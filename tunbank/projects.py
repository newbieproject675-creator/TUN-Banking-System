"""The project catalogue and the /grant project calculation.

The catalogue is DATA, not code, so it can be corrected when the game changes without a new deployment:
  * tunbank/catalogue/projects.json is the built-in seed. It has the project names, identifiers and the prerequisites known for
    certain, but NO COSTS (they could not be verified when it was written), so every project starts as "cost not loaded".
  * An Admin imports the real table with /grantset projectimport (CSV or JSON). Each import is stored as a new immutable version
    (who, when, a sha256 fingerprint of the file) and the newest version is the one in force. /grantset projects exports the
    current catalogue as a CSV, ready to edit and import again.
  * Domestic-policy discounts live in the same catalogue ("policy_discounts"): which policy discounts which projects, by how
    much, on cash and/or resources. Until that table is imported and marked verified, every preview says so.

Nothing is guessed: a project with no cost, a nation that already owns it, one missing a prerequisite, or one whose
ownership/policy PnW did not report is flagged for review and nothing is sent for it.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import ledger as L
from . import money as M
from .pnw_rules import Calc, D, clean, stock_units
from .util import now_iso

SEED = Path(__file__).parent / "catalogue" / "projects.json"
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
APPLIES = ("cash", "resources", "both")
CSV_COLUMNS = ["key", "name", "category", *M.RESOURCES, "requires", "min_cities", "notes"]
MAX_IMPORT_BYTES = 300_000


class CatalogueError(L.LedgerError):
    pass


@dataclass
class Catalogue:
    projects: dict = field(default_factory=dict)         # key -> entry
    policy_rules: list = field(default_factory=list)
    policy_verified: bool = False
    source: str = "built-in seed"
    sha: str = ""
    version_id: int | None = None
    imported_by: str = ""
    imported_at: str = ""

    def costed(self) -> int:
        return sum(1 for e in self.projects.values() if e.get("cost"))

    def verified(self) -> int:
        return sum(1 for e in self.projects.values() if e.get("verified") and e.get("cost"))


# ------------------------------------------------------------------------------------------------ loading
def _clean_policy(s) -> str:
    return str(s).strip().upper().replace(" ", "_")


def normalise(data: dict) -> dict:
    """Validate a catalogue document and return it in canonical form. Raises CatalogueError listing EVERY problem."""
    errors: list = []
    out_projects, seen = [], set()
    for i, e in enumerate((data or {}).get("projects") or [], start=1):
        key = str(e.get("key", "")).strip().lower()
        if not KEY_RE.match(key):
            errors.append(f"project #{i}: the key '{e.get('key')}' is not a valid identifier (lower case letters, digits, underscore)")
            continue
        if key in seen:
            errors.append(f"project '{key}' appears twice")
        seen.add(key)
        cost = e.get("cost")
        if cost is not None:
            clean_cost = {}
            for r, v in cost.items():
                if r not in M.RESOURCES:
                    errors.append(f"{key}: '{r}' is not a resource")
                    continue
                try:
                    amount = D(str(v))
                except Exception:  # noqa: BLE001
                    errors.append(f"{key}: the {r} cost '{v}' is not a number")
                    continue
                if amount < 0:
                    errors.append(f"{key}: the {r} cost is negative")
                elif amount:
                    clean_cost[r] = str(amount.normalize()) if amount != amount.to_integral() else str(int(amount))
            cost = clean_cost or None
        out_projects.append({"key": key, "name": str(e.get("name") or key.replace("_", " ").title()).strip(), "category": str(e.get("category") or "").strip(),
                             "cost": cost, "requires": [str(x).strip().lower() for x in (e.get("requires") or []) if str(x).strip()],
                             "min_cities": int(e["min_cities"]) if e.get("min_cities") not in (None, "") else None,
                             "verified": bool(e.get("verified")) and cost is not None, "notes": str(e.get("notes") or "")[:300]})
    keys = {p["key"] for p in out_projects}
    for p in out_projects:
        for r in p["requires"]:
            if r not in keys:
                errors.append(f"{p['key']}: the prerequisite '{r}' is not a project in this catalogue")
    pol = (data or {}).get("policy_discounts") or {"verified": False, "rules": []}
    rules = []
    for i, r in enumerate(pol.get("rules") or [], start=1):
        try:
            d = D(str(r["discount"]))
        except Exception:  # noqa: BLE001
            errors.append(f"policy rule #{i}: the discount is not a number")
            continue
        projects = [str(x).strip().lower() for x in (r.get("projects") or [])]
        if not (0 < d < 1):
            errors.append(f"policy rule #{i}: the discount must be a fraction between 0 and 1 (0.05 = 5%)")
        if not str(r.get("policy", "")).strip() or not projects:
            errors.append(f"policy rule #{i}: needs a policy and a list of projects (or '*')")
        for k in projects:
            if k != "*" and k not in keys:
                errors.append(f"policy rule #{i}: '{k}' is not a project in this catalogue")
        if r.get("applies_to", "both") not in APPLIES:
            errors.append(f"policy rule #{i}: applies_to must be one of {', '.join(APPLIES)}")
        rules.append({"policy": _clean_policy(r.get("policy", "")), "projects": projects, "discount": str(d.normalize()), "applies_to": r.get("applies_to", "both")})
    if errors:
        raise CatalogueError("The catalogue has problems, nothing was imported:\n- " + "\n- ".join(errors[:25]) + (f"\n…and {len(errors) - 25} more" if len(errors) > 25 else ""))
    return {"format": 1, "source": str((data or {}).get("source") or ""), "projects": out_projects,
            "policy_discounts": {"verified": bool(pol.get("verified")), "rules": rules}}


def _from_doc(doc: dict, *, source: str, sha: str, version_id=None, by="", at="") -> Catalogue:
    return Catalogue(projects={p["key"]: p for p in doc["projects"]}, policy_rules=doc["policy_discounts"]["rules"],
                     policy_verified=doc["policy_discounts"]["verified"], source=source, sha=sha, version_id=version_id, imported_by=by, imported_at=at)


def load_seed() -> Catalogue:
    text = SEED.read_text()
    return _from_doc(normalise(json.loads(text)), source="built-in seed (no costs)", sha=hashlib.sha256(text.encode()).hexdigest())


def active(conn) -> Catalogue:
    """The catalogue in force: the newest imported version, else the built-in seed."""
    row = conn.execute("SELECT * FROM project_catalogue_versions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return load_seed()
    return _from_doc(normalise(json.loads(row["json_text"])), source=row["source_name"], sha=row["sha256"], version_id=row["id"],
                     by=row["imported_by"], at=row["imported_at"])


# ------------------------------------------------------------------------------------------------ import / export
def parse_csv(text: str) -> dict:
    """A CSV table of projects -> a partial catalogue document. A row with no cost cells leaves that project's cost as it is."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows or "key" not in (rows[0] or {}):
        raise CatalogueError("The CSV needs a header row with at least a `key` column. Export the current catalogue with /grantset projects to get the right layout.")
    projects = []
    for r in rows:
        key = (r.get("key") or "").strip()
        if not key:
            continue
        cells = {res: (r.get(res) or "").strip().replace(",", "").replace("$", "") for res in M.RESOURCES}
        entry = {"key": key, "name": (r.get("name") or "").strip() or None, "category": (r.get("category") or "").strip(),
                 "requires": [x for x in re.split(r"[|;, ]+", r.get("requires") or "") if x],
                 "min_cities": (r.get("min_cities") or "").strip() or None, "notes": (r.get("notes") or "").strip()}
        entry["cost"] = {res: v for res, v in cells.items() if v != ""} if any(v != "" for v in cells.values()) else "UNCHANGED"
        projects.append(entry)
    return {"projects": projects}


def merge(base: Catalogue, incoming: dict, *, attested: bool) -> dict:
    """The current catalogue with the incoming rows applied (rows are matched by key; everything else is kept). Imported costs are marked verified."""
    doc = {"format": 1, "source": "", "projects": [dict(p) for p in base.projects.values()],
           "policy_discounts": {"verified": base.policy_verified, "rules": list(base.policy_rules)}}
    index = {p["key"]: p for p in doc["projects"]}
    for e in incoming.get("projects") or []:
        key = str(e.get("key", "")).strip().lower()
        row = index.get(key)
        if row is None:
            row = {"key": key, "name": None, "category": "", "cost": None, "requires": [], "min_cities": None, "verified": False, "notes": ""}
            doc["projects"].append(row)
            index[key] = row
        for f in ("name", "category", "notes"):
            if e.get(f):
                row[f] = e[f]
        if e.get("requires"):
            row["requires"] = e["requires"]
        if e.get("min_cities") not in (None, ""):
            row["min_cities"] = e["min_cities"]
        if e.get("cost") != "UNCHANGED" and "cost" in e:
            row["cost"] = e["cost"]
            row["verified"] = bool(attested and e["cost"])
    if "policy_discounts" in incoming:
        doc["policy_discounts"] = incoming["policy_discounts"]
    return doc


def prepare(base: Catalogue, text: str, filename: str):
    """Validate an uploaded CSV/JSON and merge it into `base` WITHOUT storing anything. Returns (document, keys whose cost changes)."""
    if len(text.encode()) > MAX_IMPORT_BYTES:
        raise CatalogueError("That file is too large to be a project catalogue.")
    is_json = filename.lower().endswith(".json") or text.lstrip().startswith("{")
    try:
        incoming = json.loads(text) if is_json else parse_csv(text)
    except ValueError as exc:
        raise CatalogueError(f"I couldn't read that file as JSON: {exc}") from exc
    doc = normalise(merge(base, incoming, attested=True))
    doc["source"] = f"imported from {filename}"
    changed = [p["key"] for p in doc["projects"] if p.get("cost") != (base.projects.get(p["key"]) or {}).get("cost")]
    return doc, changed


def import_catalogue(conn, *, text: str, filename: str, actor: str) -> dict:
    """Validate an uploaded CSV/JSON, merge it into the catalogue in force, store it as a NEW immutable version. Caller owns the transaction."""
    base = active(conn)
    doc, _ = prepare(base, text, filename)
    body = json.dumps(doc, sort_keys=True)
    sha = hashlib.sha256(body.encode()).hexdigest()
    cur = conn.execute("INSERT INTO project_catalogue_versions(imported_at,imported_by,source_name,sha256,project_count,json_text) VALUES(?,?,?,?,?,?)",
                       (now_iso(), actor, filename, sha, len(doc["projects"]), body))
    new = _from_doc(doc, source=filename, sha=sha, version_id=cur.lastrowid, by=actor, at=now_iso())
    changed = [k for k, e in new.projects.items() if e.get("cost") != (base.projects.get(k) or {}).get("cost")]
    L.audit(conn, actor, "PROJECT_CATALOGUE_IMPORTED", f"catalogue:{cur.lastrowid}", {
        "file": filename, "sha256": sha, "projects": len(doc["projects"]), "costs_changed": changed, "policy_rules": len(new.policy_rules),
        "policy_verified": new.policy_verified})
    return {"version_id": cur.lastrowid, "sha": sha, "projects": len(doc["projects"]), "costs_changed": changed, "costed": new.costed(),
            "policy_rules": len(new.policy_rules), "policy_verified": new.policy_verified}


def export_csv(cat: Catalogue) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for e in cat.projects.values():
        cost = e.get("cost") or {}
        w.writerow([e["key"], e["name"], e.get("category", ""), *[cost.get(r, "") for r in M.RESOURCES], "|".join(e.get("requires") or []),
                    e.get("min_cities") or "", e.get("notes", "")])
    return out.getvalue()


# ------------------------------------------------------------------------------------------------ finding a project
def find(cat: Catalogue, text: str):
    """By key or by official name (any case); otherwise a part of a name if it picks exactly one project."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for e in cat.projects.values():
        if t in (e["key"], e["name"].lower()):
            return e
    part = [e for e in cat.projects.values() if t in e["name"].lower() or t.replace(" ", "_") in e["key"]]
    return part[0] if len(part) == 1 else None


def project_field_keys(cat: Catalogue) -> list:
    return sorted(cat.projects)


# ------------------------------------------------------------------------------------------------ the calculation
def _money_text(amounts: dict) -> str:
    return ", ".join(f"{M.LABELS[r]} {M.fmt_units(r, a)}" for r, a in amounts.items()) or "nothing"


def compute_project(nation: dict, entry: dict, cat: Catalogue, *, only_missing: bool = False) -> Calc:
    """Standard cost -> domestic-policy discount -> final cost, plus every eligibility check. Anything doubtful is flagged, not guessed."""
    c = Calc(nation_id=nation["id"])
    key, name = entry["key"], entry["name"]
    c.assumptions.append(f"Catalogue: {cat.source} (fingerprint {cat.sha[:12]})")
    if not entry.get("cost"):
        c.review.append(f"the cost of {name} is not loaded in the project catalogue (import it with /grantset projectimport)")
        return c
    if not entry.get("verified"):
        c.review.append(f"the cost of {name} has not been verified (import the official table with /grantset projectimport)")
        return c
    # ---- eligibility
    projects = nation.get("projects") or {}
    owned = projects.get(key)
    if owned is True:
        c.review.append(f"the nation already owns {name}")
    elif owned is None:
        c.review.append(f"PnW did not report whether the nation owns {name}, so I cannot confirm it can be bought")
    for req in entry.get("requires") or []:
        have = projects.get(req)
        label = cat.projects.get(req, {}).get("name", req)
        if have is False:
            c.review.append(f"it requires {label}, which the nation does not have")
        elif have is None:
            c.review.append(f"it requires {label}, and PnW did not report whether the nation has it")
    if entry.get("min_cities") and (nation.get("num_cities") or 0) < entry["min_cities"]:
        c.review.append(f"it needs at least {entry['min_cities']} cities and the nation has {nation.get('num_cities')}")
    timer = nation.get("project_timer")
    if timer:
        c.review.append(f"the nation's project timer is still running ({timer} turn(s) left), so it cannot buy a project yet")
    elif timer is None:
        c.assumptions.append("NOT VERIFIED: the project cooldown and the number of project slots cannot be read from PnW; check in game")
        c.estimate = True
    if entry.get("notes") and entry["notes"] != "cost and any other requirements not loaded":
        c.assumptions.append(f"Catalogue note: {entry['notes']}")
    elif not entry.get("min_cities") and not entry.get("requires"):
        c.assumptions.append("Only the eligibility conditions in the catalogue are checked; others the catalogue does not list are not")
    # ---- cost
    standard = {r: M.to_units(Decimal(str(v))) for r, v in entry["cost"].items()}
    discount, how = D(0), "none"
    matching = [r for r in cat.policy_rules if "*" in r["projects"] or key in r["projects"]]
    pol_raw = nation.get("policy")
    pol = _clean_policy(pol_raw) if pol_raw is not None else None
    if matching:
        if pol is None:
            c.review.append("the nation's domestic policy could not be read, and a policy discount may apply to this project, so the standard price is NOT assumed")
        else:
            hit = [r for r in matching if r["policy"] == pol]
            if hit:
                best = max(hit, key=lambda r: D(r["discount"]))              # one policy at a time: discounts are never stacked
                discount, how = D(best["discount"]), f"{pol} -{(discount * 100).normalize():f}% on {best['applies_to']}"
            else:
                how = f"{pol}: no discount for this project"
    else:
        how = f"{pol or 'unknown policy'}: no policy discount is listed for this project"
    if not cat.policy_verified:
        c.estimate = True
        c.assumptions.append("ESTIMATE: the domestic-policy discount table has NOT been verified, so any discount that applies may be missing")
    final = {}
    for r, units in standard.items():
        applies = discount and (best["applies_to"] in ("both", "cash" if r == "money" else "resources"))
        amount = (D(units) / M.SCALE) * (1 - discount) if applies else D(units) / M.SCALE
        final[r] = int(amount.to_integral_value(rounding="ROUND_CEILING")) * M.SCALE          # always rounded UP to a whole unit/dollar
    c.requirement = final
    c.stock = stock_units(nation)
    c.amounts = dict(final)
    if only_missing:
        if c.stock is None:
            c.review.append("the current stockpile is not available, so only the missing amount cannot be verified")
        else:
            c.amounts = {r: max(0, a - c.stock.get(r, 0)) for r, a in final.items()}
            c.amounts = {r: a for r, a in c.amounts.items() if a > 0}
    # ---- the preview text
    stock_txt = ("not available" if c.stock is None else _money_text({r: c.stock.get(r, 0) for r in final}))
    c.lines += [f"Project: {name} ({key})", f"Standard cost: {_money_text(standard)}", f"Domestic policy: {how}",
                f"Final cost: {_money_text(final)}", f"Nation now has: {stock_txt}", f"To send: {_money_text(c.amounts) if not c.review else 'nothing (see issues)'}"]
    c.inputs = {"project": key, "project_name": name, "standard_cost": {r: str(v) for r, v in entry["cost"].items()}, "policy": pol, "discount": str(discount),
                "policy_table_verified": cat.policy_verified, "catalogue_sha256": cat.sha, "catalogue_version": cat.version_id, "only_missing": only_missing,
                "preview": "\n".join(c.lines)[:900]}
    return c
