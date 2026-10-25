"""/transfer raws, /grant warchest|city|infrastructure|land and the /grantset configuration group.

All five disbursement commands share ONE flow (`flow`) so they cannot differ in safety:
  resolve recipients -> fetch live nation/city/stockpile data -> calculate -> validate the funding source for the WHOLE
  operation -> preview -> explicit confirmation -> re-fetch and re-validate (nothing is sent if the numbers changed) ->
  create the batch -> send each recipient through the normal safe pipeline -> report every recipient's outcome.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import config_view as CV
from . import disburse as DB
from . import fmt
from . import icons
from . import identity
from . import ledger as L
from . import money as M
from . import pnw_rules as PR
from . import projects as PJ
from .config import cfg_bool, cfg_get, cfg_int, cfg_set
from .pnw import PnWRejected, PnWUncertain
from .ui import Services, actor_label, confirm, has_flag, nation_arg, need, reply, role_ids, thinking
from .valuation import value_amounts

log = logging.getLogger("tunbank.disburse")
STATUS_ICON = {"COMPLETED": "✅", "FAILED": "⛔", "UNCERTAIN": "⚠️", "PENDING": "⏳", "REVIEW": "🔎"}
MAX_DAYS = Decimal(30)
SOURCE_CHOICES = [app_commands.Choice(name="Alliance funds (TUN-owned)", value="ALLIANCE"),
                  app_commands.Choice(name="A member's AVAILABLE balance (needs the member-funds permission)", value="MEMBER_AVAILABLE"),
                  app_commands.Choice(name="A member's LOCKED funds (needs the member-funds permission + lock_id)", value="MEMBER_LOCKED")]


def calc_report(kind, calcs, source, note, params, label_of) -> str:
    """The full calculation as text (attached to the preview): per nation requirement, stockpile, amount to send, and the source."""
    out = [f"{DB.LABEL[kind]}", f"Funding source: {source.description}", f"Note: {note}", f"Inputs: {json.dumps(params, default=str)}",
           f"Rules: {PR.RULES_VERSION}", ""]
    for c in calcs:
        out.append(f"=== {label_of(c.nation_id)} ===")
        if c.review:
            out.append("  FLAGGED FOR REVIEW - NOT INCLUDED: " + "; ".join(c.review))
        out += [f"  {ln}" for ln in c.lines]
        if c.requirement:
            out.append("  Requirement: " + ", ".join(f"{M.LABELS[r]} {M.fmt_units(r, a)}" for r, a in c.requirement.items()))
        out.append("  Current stockpile: " + (", ".join(f"{M.LABELS[r]} {M.fmt_units(r, c.stock.get(r, 0))}" for r in c.requirement)
                                              if c.stock and c.requirement else ("not available" if c.stock is None else "-")))
        out.append("  To send: " + (", ".join(f"{M.LABELS[r]} {M.fmt_units(r, a)}" for r, a in c.amounts.items()) if c.amounts and c.sendable else "nothing"))
        out.append("  From: " + source.description)
        for a in c.assumptions:
            out.append(f"  * {a}")
        out.append("")
    return "\n".join(out)


def register(grant: app_commands.Group, transfer: app_commands.Group, grantset: app_commands.Group, svc: Services):
    def uid(i):
        return str(i.user.id)

    # ================================================================================== shared pieces
    async def recipients(interaction, text: str):
        with svc.db.read() as conn:
            limit = max(1, cfg_int(conn, "disburse_max_nations"))
        ids: list = []
        for tok in [t.strip() for t in text.split(",") if t.strip()]:
            if tok.lower() == "alliance":
                try:
                    ids += sorted(await svc.pnw.fetch_alliance_members())
                except (PnWRejected, PnWUncertain) as exc:
                    await reply(interaction, f"I couldn't read the alliance member list, so nothing was done: {exc}")
                    return None
                continue
            nid = await nation_arg(svc, interaction, tok)
            if nid is None:
                return None
            ids.append(nid)
        ids = list(dict.fromkeys(ids))
        if not ids:
            await reply(interaction, "Name at least one nation (comma separated), or `alliance` for every member.")
            return None
        if len(ids) > limit:
            await reply(interaction, f"That is {len(ids)} nations; one command may include at most {limit}. "
                                     f"(An Admin can change this: `/bankset config` → Most nations per command.)")
            return None
        return ids

    async def calculate(ids, calc_fn, kind):
        """Fetch live data and calculate every recipient. A nation PnW did not return, or that is not in the alliance, is flagged."""
        with svc.db.read() as conn:
            PR.load_overrides(cfg_get(conn, "pnw_rule_overrides"))                       # the admin's corrections to the built-in rules
            project_keys = PJ.project_field_keys(PJ.active(conn)) if kind == "PROJECT_GRANT" else ()
        details = await svc.pnw.fetch_nations_detail(ids, project_keys)
        try:
            roster = await svc.pnw.fetch_alliance_members()          # the proven member list (applicants excluded)
        except (PnWRejected, PnWUncertain):
            roster = None
        calcs = []
        for nid in ids:
            d = details.get(nid)
            why = None
            if d is None:
                why = "PnW did not return this nation"
            else:
                mine = svc.settings.alliance_id
                reported = d.get("alliance_id")
                if roster is not None and nid not in roster:
                    why = (f"the nation is not in this alliance's member list (PnW reports alliance id {reported}; this bot serves {mine})"
                           + (" - it may be an applicant, not a full member" if reported == mine else ""))
                elif reported != mine:
                    why = f"PnW reports this nation in alliance id {reported}, but this bot serves alliance {mine}"
            if why:
                c = PR.Calc(nation_id=nid)
                c.review.append(why)
            else:
                c = calc_fn(d)
            calcs.append(c)
        with svc.db.read() as conn:
            DB.apply_caps(conn, kind, calcs)
        return calcs

    def allowed_source(interaction, source: DB.Source) -> Optional[str]:
        """None if the operator may spend this source, otherwise the reason. Spending MEMBERS' money is a separate permission."""
        with svc.db.read() as conn:
            locked_on = cfg_bool(conn, "econ_locked_withdraw_enabled")
        if source.kind == "MEMBER_AVAILABLE" and not has_flag(svc, interaction, "disburse_member_available"):
            return ("Spending a member's available balance needs the `disburse_member_available` permission, which is separate from "
                    "ordinary ECON rights. An Admin can give it with `/bankset setaccess`.")
        if source.kind == "MEMBER_LOCKED":
            if not has_flag(svc, interaction, "disburse_member_locked"):
                return ("Spending a member's locked funds needs the `disburse_member_locked` permission. An Admin can give it with `/bankset setaccess`.")
            if not locked_on:
                return "Spending LOCKED funds is switched off. An Admin can enable it (`econ_locked_withdraw_enabled`)."
        return None

    # ================================================================================== the one flow
    async def flow(interaction, *, kind, nations, source_choice, source_member, lock_id, note, params, calc_fn, default_note):
        with svc.db.read() as conn:
            level = (cfg_get(conn, "grant_min_level") or "MINISTER").strip().upper()
        if not await need(svc, interaction, level if level in ("BANKER", "MINISTER", "ADMIN") else "MINISTER"):
            return
        await thinking(interaction)
        src_kind = source_choice.value
        if src_kind == "ALLIANCE" and (source_member.strip() or lock_id):
            return await reply(interaction, "You named a member or a lock, but the source is alliance funds. Pick the matching source.")
        if src_kind == "MEMBER_LOCKED" and not lock_id:
            return await reply(interaction, "Locked funds need the `lock_id` of the reserve to spend from.")
        member_id = None
        if source_member.strip():
            member_id = await nation_arg(svc, interaction, source_member)
            if member_id is None:
                return
        source = DB.Source(src_kind, member_id, lock_id or None)
        why = allowed_source(interaction, source)
        if why:
            return await reply(interaction, why)
        ids = await recipients(interaction, nations)
        if ids is None:
            return
        await identity.ensure(svc.pnw, ids + ([member_id] if member_id else []))
        label_of = identity.label
        with svc.db.read() as conn:
            source.description = DB.describe_source(svc, conn, source, label_of)
        note = (note or default_note).strip()[:70]
        reveal = has_flag(svc, interaction, "bank_view_alliance_holdings")

        async def build():
            calcs = await calculate(ids, calc_fn, kind)
            errors, snap, val = await DB.check_source(svc, source, calcs, actor=uid(interaction), role_ids=role_ids(interaction), reveal=reveal)
            return calcs, errors, snap, val

        try:
            calcs, errors, snap, val = await build()
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't read the nation data from PnW, so nothing was calculated or sent: {exc}")
        sendable = [c for c in calcs if c.sendable and c.amounts]
        flagged = [c for c in calcs if not c.sendable]
        report = calc_report(kind, calcs, source, note, params, label_of)
        try:
            await interaction.followup.send(content="Full calculation (per nation) is attached.", ephemeral=True,
                                            file=discord.File(io.BytesIO(report.encode()), filename=f"{kind.lower()}-calculation.txt"))
        except Exception:  # noqa: BLE001 - the preview card below still carries the essentials
            log.warning("could not attach the calculation report", exc_info=True)

        totals = DB.totals_of(calcs)
        card = A.Card(f"{icons.status('withdraw')} {DB.LABEL[kind]} · preview", "Nothing is sent until you press Confirm.", A.ORANGE)
        card.add("FUNDING SOURCE (whose money is spent)", f"**{source.description}**")
        card.add("Recipients", f"{len(sendable)} will be paid" + (f" · **{len(flagged)} left out for review**" if flagged else ""), True)
        card.add("Note", note, True)
        if totals:
            card.add("Total to send", fmt.amounts_with_value(totals, val) if val else fmt.amount_lines(totals))
        for c in sendable[:6]:
            if kind == "PROJECT_GRANT" and c.inputs.get("preview"):
                card.add(label_of(c.nation_id), c.inputs["preview"][:1000] + ("\n⚠️ ESTIMATE" if c.estimate else ""), False)
            else:
                card.add(label_of(c.nation_id), fmt.short_amounts(c.amounts) + (" ⚠️ ESTIMATE" if c.estimate else ""), False)
        if len(sendable) > 6:
            card.add("…", f"and {len(sendable) - 6} more (see the attached calculation)")
        if flagged:
            card.add("🔎 Flagged for review - NOT included",
                     "\n".join(f"**{label_of(c.nation_id)}**: {'; '.join(c.review)[:140]}" for c in flagged[:6])
                     + (f"\n…and {len(flagged) - 6} more" if len(flagged) > 6 else ""))
        assumptions = list(dict.fromkeys(a for c in sendable for a in c.assumptions))[:5]
        if assumptions:
            card.add("Assumptions", "\n".join(f"• {a}" for a in assumptions))
        if not sendable:
            card.title = f"{icons.status('wait')} {DB.LABEL[kind]} · nothing to send"
            card.description = ("Nothing was sent. " + ("Every nation was left out for review (see below); the funding source was not the problem."
                                                       if flagged else "The calculation came to nothing for these nations."))
            return await reply(interaction, card=card)
        if errors:
            card.title = f"{icons.status('bad')} {DB.LABEL[kind]} · blocked"
            card.description = "Nothing was sent. Something has to be fixed first (see Problems below)."
            card.add("Problems", "\n".join(errors[:8]) + (f"\n…and {len(errors) - 8} more" if len(errors) > 8 else ""))
            return await reply(interaction, card=card)
        with svc.db.read() as conn:
            live = DB.is_live(conn, kind)
            threshold = cfg_int(conn, "approval_threshold_value") * 100
        if not live:
            card.title = f"{icons.status('wait')} {DB.LABEL[kind]} · PREVIEW ONLY"
            card.description = ("Sending is switched OFF for this kind of transfer, so this is only a preview. Compare it with the game, then an Admin "
                                f"can switch it on: `/grantset live kind:{kind} enabled:on`.")
            return await reply(interaction, card=card)

        approval_id = approver = None
        if threshold and (val is None or val.total_cents is None or val.total_cents > threshold):
            payload = {"disburse": kind, "nations": sorted(c.nation_id for c in sendable), "totals": totals,
                       "source": [src_kind, member_id, lock_id or None], "note": note}

            def ask():
                with svc.db.tx() as conn:
                    return L.find_or_request_approval(conn, "ECON_WITHDRAW", uid(interaction), payload, f"{kind}: {note}")
            approval_id, approver = await asyncio.to_thread(ask)
            if not approver:
                c = A.Card(f"{icons.status('wait')} {DB.LABEL[kind]} needs a second approver",
                           f"Worth {fmt.dollars(val.total_cents if val else None)} to {len(sendable)} nation(s).", A.ORANGE, kind="APPROVAL")
                c.add("Request", f"#{approval_id} by {actor_label(interaction)}")
                c.add("Funding source", source.description)
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`, then you run this same command again.")
                await svc.alerts.econ(c)
                return await reply(interaction, f"Approval request #{approval_id} was posted. After a different Minister approves it, run this same command again.")
            card.add("Second approver", f"<@{approver}>", True)

        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")

        # ---- re-validate everything now (permissions, source, numbers). If anything changed since the preview, send nothing.
        why = allowed_source(interaction, source)
        if why:
            return await reply(interaction, why)
        try:
            calcs2, errors2, snap2, val2 = await build()
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I couldn't re-read PnW, so nothing was sent: {exc}")
        if errors2:
            return await reply(interaction, "Nothing was sent: the funding source no longer checks out.\n" + "\n".join(errors2[:6]))
        if DB.signature(calcs2) != DB.signature(calcs):
            return await reply(interaction, "Nothing was sent: the nations' data changed since the preview, so the amounts are different now. "
                                            "Run the command again to see the new calculation.")

        def create():
            with svc.db.tx() as conn:
                with_bank = svc.settings.payout.name
                return DB.create_batch(conn, kind=kind, calcs=calcs2, source=source, actor=uid(interaction), note=note, params=params,
                                       rules_version=PR.RULES_VERSION, paying_bank=with_bank if src_kind == "ALLIANCE" else "",
                                       val=val2, snap=snap2, approval_id=approval_id, request_key=f"{kind}:{interaction.id}")
        try:
            batch_id = await asyncio.to_thread(create)
        except L.LedgerError as exc:
            return await reply(interaction, f"Nothing was sent: {exc}")
        if batch_id is None:
            return await reply(interaction, "This request was already processed. Nothing was repeated.")
        result = await DB.run_batch(svc, batch_id, uid(interaction), role_ids(interaction), reveal)
        await show_result(interaction, batch_id, result.get("halted_why") or result.get("error"), label_of=label_of)

    async def show_result(interaction, batch_id, halted=None, label_of=None):
        label_of = label_of or identity.label

        def load():
            with svc.db.tx() as conn:
                DB.refresh_items(conn, batch_id)
                b, items = DB.load(conn, batch_id)
                return dict(b), [dict(i) for i in items], DB.summarize(conn, batch_id)
        b, items, summary = await asyncio.to_thread(load)
        counts = summary["counts"]
        color = {"COMPLETED": A.GREEN, "PARTIAL": A.ORANGE, "HALTED": A.ORANGE, "FAILED": A.RED}.get(b["status"], A.ORANGE)
        c = A.Card(f"{DB.LABEL[b['kind']]} #{batch_id} · {b['status'].title()}", f"By <@{b['actor']}> · {b['note']}", color, kind="DISBURSEMENT")
        c.add("Funding source", b["source_description"])
        c.add("Outcome", " · ".join(f"{STATUS_ICON[k]} {counts[k]} {k.lower()}" for k in ("COMPLETED", "FAILED", "UNCERTAIN", "PENDING", "REVIEW") if counts[k]) or "-")
        if summary["sent"]:
            c.add("Actually sent", fmt.amount_lines(summary["sent"]))
        lines = [f"{STATUS_ICON[i['status']]} **{label_of(i['nation_id'])}** · {fmt.short_amounts(json.loads(i['amounts_json'])) if json.loads(i['amounts_json']) else '—'}"
                 + (f"\n-# {i['message'][:110]}" if i["message"] and i["status"] != "COMPLETED" else "") for i in items[:12]]
        c.add("Each recipient", "\n".join(lines) + (f"\n…and {len(items) - 12} more: `/grantset batch batch_id:{batch_id}`" if len(items) > 12 else ""))
        if counts["REVIEW"] or counts["FAILED"] or counts["UNCERTAIN"]:
            c.add("⚠️ Not everything succeeded", "Recipients flagged for review were NOT paid. Failed ones changed no balance. Uncertain ones are being "
                                                  "checked against the PnW bank records; nothing is sent twice.")
        if halted:
            c.add("Stopped early", f"{halted}\nContinue with `/grantset resume batch_id:{batch_id}` once the cause is fixed. Sent ones are never repeated.")
        await reply(interaction, card=c)
        await svc.alerts.econ(c)
        await svc.alerts.flush_events()
        await svc.alerts.flush_notifications()

    # ================================================================================== commands
    @transfer.command(name="raws", description="ECON: send the resources nations need to keep operating for a number of days")
    @app_commands.describe(nations="Nation names or IDs, comma separated (or: alliance)", days="Days to cover, e.g. 1.5 or 7",
                           source="Whose funds pay for it", source_member="Member-funded: whose balance (leave empty = each recipient's own)",
                           lock_id="Locked-funds source: the lock to spend from", no_cash="Do not send any cash",
                           no_daily_cash="Never lift a nation's cash above the daily-bonus threshold",
                           only_missing="Deduct each nation's verified stockpile (send only what is missing)", note="Note shown in PnW")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def raws(interaction: discord.Interaction, nations: str, days: float, source: app_commands.Choice[str], source_member: str = "",
                   lock_id: int = 0, no_cash: bool = False, no_daily_cash: bool = False, only_missing: bool = False, note: str = ""):
        try:
            d = Decimal(str(days))
        except InvalidOperation:
            return await reply(interaction, "Days must be a number such as 1.5 or 7.")
        if d <= 0 or d > MAX_DAYS:
            return await reply(interaction, f"Days must be above 0 and at most {MAX_DAYS}.")
        with svc.db.read() as conn:
            threshold = cfg_int(conn, "daily_cash_threshold")

        def calc(nation):
            c = PR.compute_raws(nation, d, include_cash=not no_cash, only_missing=only_missing)
            if no_daily_cash and not no_cash and c.sendable:
                PR.apply_daily_cash_rule(c, nation, threshold)
            return c
        await flow(interaction, kind="RAWS", nations=nations, source_choice=source, source_member=source_member, lock_id=lock_id,
                   note=note, calc_fn=calc, default_note=f"Raws for {PR.clean(d)} day(s)",
                   params={"days": PR.clean(d), "no_cash": no_cash, "no_daily_cash": no_daily_cash, "only_missing": only_missing,
                           "daily_cash_threshold": threshold})

    @grant.command(name="warchest", description="ECON: give a nation its warchest (ECON's resources per city x ratio)")
    @app_commands.describe(nations="Nation names or IDs, comma separated (or: alliance)", ratio="Multiplier, e.g. 1, 1.5, 2.5 (default 1)",
                           source="Whose funds pay for it", source_member="Member-funded: whose balance (leave empty = each recipient's own)",
                           lock_id="Locked-funds source: the lock to spend from", only_missing="Deduct each nation's verified stockpile", note="Note shown in PnW")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def warchest(interaction: discord.Interaction, nations: str, source: app_commands.Choice[str], ratio: float = 1.0,
                       source_member: str = "", lock_id: int = 0, only_missing: bool = False, note: str = ""):
        with svc.db.read() as conn:
            try:
                per_city = M.parse_amounts(cfg_get(conn, "warchest_per_city") or "")
            except M.AmountError:
                per_city = {}
        if not per_city:
            return await reply(interaction, "No warchest is configured yet. ECON sets it with `/grantset warchest`.")
        r = Decimal(str(ratio))
        await flow(interaction, kind="WARCHEST_GRANT", nations=nations, source_choice=source, source_member=source_member, lock_id=lock_id,
                   note=note, calc_fn=lambda n: PR.compute_warchest(n, per_city, r, only_missing=only_missing),
                   default_note=f"Warchest x{PR.clean(r)}",
                   params={"ratio": PR.clean(r), "per_city": per_city, "only_missing": only_missing})

    @grant.command(name="city", description="ECON: give the money to buy new cities, or to buy up to a city number")
    @app_commands.describe(nation="Recipient: id, name, link or @user", source="Whose funds pay for it",
                           number_of_cities="Buy this many NEW cities (default 1)",
                           up_to="Buy up to this city number: up_to 25 for a 24-city nation is just city #25",
                           source_member="Member-funded: whose balance (empty = the recipient's own)", lock_id="Locked-funds source: the lock",
                           note="Note shown in PnW")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def city(interaction: discord.Interaction, nation: str, source: app_commands.Choice[str], number_of_cities: int = 0, up_to: int = 0,
                   source_member: str = "", lock_id: int = 0, note: str = ""):
        if "," in nation:
            return await reply(interaction, "Give ONE nation for a city grant.")
        if number_of_cities and up_to:
            return await reply(interaction, "Use EITHER `number_of_cities` (that many new cities) OR `up_to` (until the nation has that many), not both.")
        if up_to < 0 or number_of_cities < 0:
            return await reply(interaction, "Those numbers cannot be negative.")
        target = up_to or None
        count = None if up_to else (number_of_cities or 1)
        await flow(interaction, kind="CITY_GRANT", nations=nation, source_choice=source, source_member=source_member, lock_id=lock_id, note=note,
                   calc_fn=lambda n: PR.compute_city(n, count, target),
                   default_note=(f"City grant up to #{up_to}" if up_to else f"City grant x{count}"),
                   params={"number_of_cities": count, "up_to": target})

    @grant.command(name="project", description="ECON: give the money and resources a nation needs to buy a project")
    @app_commands.describe(nations="Nation names or IDs, comma separated", project="The project (start typing its name)",
                           source="Whose funds pay for it", only_missing="Deduct each nation's verified stockpile (send only what is missing)",
                           source_member="Member-funded: whose balance (empty = each recipient's own)", lock_id="Locked-funds source: the lock",
                           note="Note shown in PnW")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def project(interaction: discord.Interaction, nations: str, project: str, source: app_commands.Choice[str], only_missing: bool = False,
                      source_member: str = "", lock_id: int = 0, note: str = ""):
        with svc.db.read() as conn:
            cat = PJ.active(conn)
        entry = PJ.find(cat, project)
        if entry is None:
            return await reply(interaction, f"I don't know a project called '{project}' in the catalogue ({len(cat.projects)} projects). "
                                            "Start typing the name and pick it from the list. If a project is missing, add it with `/grantset projectimport`.")
        await flow(interaction, kind="PROJECT_GRANT", nations=nations, source_choice=source, source_member=source_member, lock_id=lock_id, note=note,
                   calc_fn=lambda n: PJ.compute_project(n, entry, cat, only_missing=only_missing), default_note=f"Project: {entry['name']}",
                   params={"project": entry["key"], "project_name": entry["name"], "only_missing": only_missing, "catalogue_sha256": cat.sha[:16]})

    @project.autocomplete("project")
    async def project_autocomplete(interaction: discord.Interaction, current: str):
        with svc.db.read() as conn:
            cat = PJ.active(conn)
        cur = (current or "").lower()
        return [app_commands.Choice(name=e["name"][:100], value=e["key"]) for e in cat.projects.values()
                if not cur or cur in e["name"].lower() or cur in e["key"]][:25]

    @grant.command(name="infrastructure", description="ECON: give the money to raise every city's infrastructure to a target")
    @app_commands.describe(nations="Nation names or IDs, comma separated", source="Whose funds pay for it",
                           target_infrastructure="Infrastructure level for every city (empty = ECON's standard target)",
                           source_member="Member-funded: whose balance (empty = each recipient's own)", lock_id="Locked-funds source: the lock",
                           note="Note shown in PnW")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def infrastructure(interaction: discord.Interaction, nations: str, source: app_commands.Choice[str], target_infrastructure: int = 0,
                             source_member: str = "", lock_id: int = 0, note: str = ""):
        with svc.db.read() as conn:
            target = target_infrastructure or cfg_int(conn, "grant_infra_target")
        if target <= 0 or target > 10000:
            return await reply(interaction, "No sensible infrastructure target: give `target_infrastructure`, or ask ECON to set the standard one "
                                            "with `/grantset infra`.")
        await flow(interaction, kind="INFRA_GRANT", nations=nations, source_choice=source, source_member=source_member, lock_id=lock_id, note=note,
                   calc_fn=lambda n: PR.compute_infra(n, target), default_note=f"Infrastructure to {target}",
                   params={"target_infrastructure": target})

    @grant.command(name="land", description="ECON: give the money to raise every city's land to a target")
    @app_commands.describe(nations="Nation names or IDs, comma separated", source="Whose funds pay for it",
                           target_land="Acres for every city (empty = ECON's standard target)",
                           source_member="Member-funded: whose balance (empty = each recipient's own)", lock_id="Locked-funds source: the lock",
                           note="Note shown in PnW")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def land(interaction: discord.Interaction, nations: str, source: app_commands.Choice[str], target_land: int = 0,
                   source_member: str = "", lock_id: int = 0, note: str = ""):
        with svc.db.read() as conn:
            target = target_land or cfg_int(conn, "grant_land_target")
        if target <= 0 or target > 10000:
            return await reply(interaction, "No sensible land target: give `target_land`, or ask ECON to set the standard one with `/grantset land`.")
        await flow(interaction, kind="LAND_GRANT", nations=nations, source_choice=source, source_member=source_member, lock_id=lock_id, note=note,
                   calc_fn=lambda n: PR.compute_land(n, target), default_note=f"Land to {target} acres", params={"target_land": target})

    # ================================================================================== /grantset
    def save_setting(interaction, key: str, text: str):
        value = CV.parse_value(key, text)
        with svc.db.tx() as conn:
            old = cfg_get(conn, key)
            cfg_set(conn, key, value, uid(interaction))
            L.audit(conn, uid(interaction), "CONFIG_CHANGED", key, {"value": value, "previous": old})
        return old, value

    async def edit(interaction, pairs):
        if not await need(svc, interaction, "MINISTER"):
            return
        try:
            def do():
                return [(k, *save_setting(interaction, k, t)) for k, t in pairs]
            done = await asyncio.to_thread(do)
        except ValueError as exc:
            return await reply(interaction, f"Not changed. {exc}")
        c = A.Card("⚙️ Grant settings changed", "", A.GREEN)
        for k, old, new in done:
            c.add(CV.SETTINGS[k].label, f"{CV.human(k, old)} → **{CV.human(k, new)}**", True)
        await reply(interaction, card=c)

    @grantset.command(name="show", description="ECON: the grant and transfer settings, and what is switched on")
    async def show(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        with svc.db.read() as conn:
            live = DB.live_kinds(conn)
            c = A.Card("⚙️ Grants & transfers", f"Rules version: `{PR.RULES_VERSION}`", A.BLUE)
            for k in ("warchest_per_city", "grant_infra_target", "grant_land_target", "grant_city_max_usd", "grant_infra_max_usd",
                      "grant_land_max_usd", "daily_cash_threshold", "disburse_ignore_tag", "disburse_max_nations"):
                c.add(CV.SETTINGS[k].label, CV.human(k, cfg_get(conn, k)), True)
            c.add("Allowed to SEND", "\n".join(f"{'✅' if k in live else '❌ preview only'} {DB.LABEL[k]}" for k in DB.KINDS))
        c.add("Funding sources offered", "Alliance funds · a member's available balance · a member's locked funds.\n"
                                       "Not offered: member tax accounts (the bank does not track a verifiable balance for them) and other alliances' "
                                       "ownership shares (their owners withdraw those themselves).")
        await reply(interaction, card=c)

    @grantset.command(name="warchest", description="ECON: set the warchest per city, e.g. food=1000 steel=100 aluminum=100")
    async def set_warchest(interaction: discord.Interaction, per_city: str):
        await edit(interaction, [("warchest_per_city", per_city)])

    @grantset.command(name="infra", description="ECON: the standard infrastructure target and the most one nation may get")
    async def set_infra(interaction: discord.Interaction, target: int, max_per_nation: str = ""):
        await edit(interaction, [("grant_infra_target", str(target))] + ([("grant_infra_max_usd", max_per_nation)] if max_per_nation else []))

    @grantset.command(name="land", description="ECON: the standard land target and the most one nation may get")
    async def set_land(interaction: discord.Interaction, target: int, max_per_nation: str = ""):
        await edit(interaction, [("grant_land_target", str(target))] + ([("grant_land_max_usd", max_per_nation)] if max_per_nation else []))

    @grantset.command(name="citycap", description="ECON: the most one nation may get from /grant city (0 = no cap)")
    async def set_citycap(interaction: discord.Interaction, max_per_nation: str):
        await edit(interaction, [("grant_city_max_usd", max_per_nation)])

    @grantset.command(name="dailycash", description="ECON: the cash level used by the no_daily_cash option of /transfer raws")
    async def set_dailycash(interaction: discord.Interaction, threshold: str):
        await edit(interaction, [("daily_cash_threshold", threshold)])

    @grantset.command(name="rules", description="ECON: the game rules the calculations use, to compare with the game")
    async def rules(interaction: discord.Interaction):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            PR.load_overrides(cfg_get(conn, "pnw_rule_overrides"))
        await interaction.followup.send(
            content="These are the rules the bot calculates with. They are **not verified** against the live game: compare previews with the game / "
                    "Locutus before switching sending on, and tell me if anything differs.", ephemeral=True,
            file=discord.File(io.BytesIO(PR.sheet().encode()), filename="pnw-rules.txt"))

    @grantset.command(name="projects", description="ECON: the project catalogue in force, and a CSV of it to edit and import")
    async def projects_status(interaction: discord.Interaction):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            cat = PJ.active(conn)
        missing = [e["name"] for e in cat.projects.values() if not e.get("cost")]
        c = A.Card("📚 Project catalogue", "Used by `/grant project`. A project with no verified cost cannot be granted.", A.BLUE)
        c.add("In force", f"{cat.source}" + (f" · version #{cat.version_id} by <@{cat.imported_by}>" if cat.version_id else " (built in)"), False)
        c.add("Fingerprint", f"`{cat.sha[:16]}`", True)
        c.add("Projects", f"{len(cat.projects)} listed · {cat.costed()} with a cost · {cat.verified()} verified", True)
        c.add("Domestic-policy discounts", (f"{len(cat.policy_rules)} rule(s) · " + ("verified" if cat.policy_verified else "NOT verified")), True)
        if missing:
            c.add("Cost not loaded", ", ".join(missing[:25]) + (f" …and {len(missing) - 25} more" if len(missing) > 25 else ""))
        c.add("Is this list complete?", "It cannot be confirmed from here. `python scripts/check_pnw_schema.py` compares it with the project fields PnW itself "
                                        "reports, and lists any that are missing.")
        c.add("To update", "Edit the attached CSV (cost columns in $ and resource units, `requires` = project keys separated by |) and import it with "
                           "`/grantset projectimport`. Each import is kept as a new version.")
        await interaction.followup.send(embed=A.to_embed(c), ephemeral=True,
                                        file=discord.File(io.BytesIO(PJ.export_csv(cat).encode()), filename="project-catalogue.csv"))

    @grantset.command(name="projectimport", description="Admin: import the project costs/requirements from a CSV or JSON file")
    @app_commands.describe(file="project-catalogue.csv (or .json). Rows are matched by `key`; projects not in the file are left as they are")
    async def projectimport(interaction: discord.Interaction, file: discord.Attachment):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        if file.size > PJ.MAX_IMPORT_BYTES:
            return await reply(interaction, "That file is too large to be a project catalogue.")
        text = (await file.read()).decode("utf-8-sig", errors="replace")
        try:
            def look():
                with svc.db.read() as conn:
                    return PJ.prepare(PJ.active(conn), text, file.filename)
            doc, changed = await asyncio.to_thread(look)
        except L.LedgerError as exc:
            return await reply(interaction, str(exc))
        c = A.Card("Confirm: import the project catalogue", "Costs you import are treated as VERIFIED by you: they must match the game.", A.ORANGE)
        c.add("File", f"{file.filename}", True)
        c.add("Projects after import", str(len(doc["projects"])), True)
        c.add("Costs added or changed", (", ".join(changed[:25]) + (f" …and {len(changed) - 25} more" if len(changed) > 25 else "")) if changed else "none")
        c.add("Domestic-policy discounts", f"{len(doc['policy_discounts']['rules'])} rule(s), " + ("marked verified" if doc["policy_discounts"]["verified"] else "NOT marked verified"))
        c.add("Kept", "The previous version stays in the history; nothing is deleted.")
        if not await confirm(svc, interaction, c):
            return await interaction.followup.send("Cancelled. Nothing was imported.", ephemeral=True)

        def do():
            with svc.db.tx() as conn:
                return PJ.import_catalogue(conn, text=text, filename=file.filename, actor=uid(interaction))
        try:
            res = await asyncio.to_thread(do)
        except L.LedgerError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await interaction.followup.send(f"Imported as catalogue version **#{res['version_id']}** ({res['projects']} projects, {res['costed']} with a cost, "
                                        f"{len(res['costs_changed'])} changed). Fingerprint `{res['sha'][:16]}`.", ephemeral=True)

    @grantset.command(name="rule", description="Admin: correct one number of the built-in game rules (or reset it)")
    @app_commands.describe(key="Which rule (start typing)", value="The new number, or `reset` to go back to the built-in value")
    async def rule_cmd(interaction: discord.Interaction, key: str, value: str):
        if not await need(svc, interaction, "ADMIN"):
            return
        if key not in PR.DEFAULT_RULES:
            return await reply(interaction, "I don't know that rule. Start typing and pick one from the list (`/grantset rules` lists them all).")
        with svc.db.read() as conn:
            current = json.loads(cfg_get(conn, "pnw_rule_overrides") or "{}")
        before = current.get(key, str(PR.DEFAULT_RULES[key]))
        if value.strip().lower() == "reset":
            current.pop(key, None)
        else:
            try:
                new = Decimal(value.strip().replace(",", ""))
            except InvalidOperation:
                return await reply(interaction, "Give a number, for example `0.05` or `1200`, or `reset`.")
            if new < 0 or new > Decimal("1e12"):
                return await reply(interaction, "That number is out of range.")
            current[key] = format(new.normalize(), "f")

        def do():
            with svc.db.tx() as conn:
                cfg_set(conn, "pnw_rule_overrides", json.dumps(current, sort_keys=True), uid(interaction))
                L.audit(conn, uid(interaction), "RULE_CHANGED", key, {"before": str(before), "after": current.get(key, "built-in")})
        await asyncio.to_thread(do)
        await reply(interaction, f"**{key}**: {before} → **{current.get(key, str(PR.DEFAULT_RULES[key]) + ' (built-in)')}**. "
                                 "It applies from the next calculation. `/grantset rules` shows every rule in force.")

    @rule_cmd.autocomplete("key")
    async def rule_autocomplete(interaction: discord.Interaction, current: str):
        cur = (current or "").lower()
        return [app_commands.Choice(name=k[:100], value=k) for k in sorted(PR.DEFAULT_RULES) if not cur or cur in k][:25]

    @grantset.command(name="live", description="Admin: allow a kind of transfer to actually SEND (off = preview only)")
    @app_commands.describe(kind="Which transfer", enabled="on = may send, off = preview only")
    @app_commands.choices(kind=[app_commands.Choice(name=DB.LABEL[k], value=k) for k in DB.KINDS],
                          enabled=[app_commands.Choice(name="on (may send)", value="on"), app_commands.Choice(name="off (preview only)", value="off")])
    async def live(interaction: discord.Interaction, kind: app_commands.Choice[str], enabled: app_commands.Choice[str]):
        if not await need(svc, interaction, "ADMIN"):
            return
        await thinking(interaction)
        on = enabled.value == "on"
        if on:
            c = A.Card(f"Allow {DB.LABEL[kind.value]} to SEND?", "Real resources and money will move when ECON confirms.", A.RED)
            c.add("Rules", f"`{PR.RULES_VERSION}`: the game rules in use are marked UNVERIFIED. Only switch this on after comparing several "
                           "previews with the game or Locutus.")
            c.add("Still protected", "Every send needs a confirmation, passes the funding-source checks, limits and the emergency lock, and goes "
                                     "through the normal at-most-once PnW pipeline.")
            if not await confirm(svc, interaction, c):
                return await interaction.followup.send("Cancelled. Nothing was changed.", ephemeral=True)

        def do():
            with svc.db.tx() as conn:
                DB.set_live(conn, kind.value, on, uid(interaction))
                L.audit(conn, uid(interaction), "DISBURSE_LIVE", kind.value, {"enabled": on})
        await asyncio.to_thread(do)
        await interaction.followup.send(f"{DB.LABEL[kind.value]} is now **{'allowed to send' if on else 'preview only'}**.", ephemeral=True)

    @grantset.command(name="batch", description="ECON: the full record of one transfer/grant batch")
    async def batch(interaction: discord.Interaction, batch_id: int):
        if not await need(svc, interaction, "AUDITOR"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            b = conn.execute("SELECT 1 FROM disbursement_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            return await reply(interaction, f"Batch #{batch_id} does not exist.")
        await identity.ensure(svc.pnw, [])
        await show_result(interaction, batch_id)

    @grantset.command(name="resume", description="ECON: continue a stopped batch (recipients already sent are never repeated)")
    async def resume(interaction: discord.Interaction, batch_id: int):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            b = conn.execute("SELECT status, funding_source, source_member_id FROM disbursement_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            return await reply(interaction, f"Batch #{batch_id} does not exist.")
        if b["status"] not in ("HALTED", "RUNNING"):
            return await reply(interaction, f"Batch #{batch_id} is {b['status'].title()}; nothing is waiting to be sent.")
        why = allowed_source(interaction, DB.Source(b["funding_source"], b["source_member_id"]))
        if why:
            return await reply(interaction, why)

        def sync():
            with svc.db.tx() as conn:
                DB.refresh_items(conn, batch_id)
        await asyncio.to_thread(sync)
        result = await DB.run_batch(svc, batch_id, uid(interaction), role_ids(interaction), has_flag(svc, interaction, "bank_view_alliance_holdings"))
        await show_result(interaction, batch_id, result.get("halted_why") or result.get("error"))
