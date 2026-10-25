# 23. First live test: an ordered checklist

Work down the list. Each step says what you should see. If something differs, stop and send me: **the command you ran, the exact text of the card the bot
showed, and the matching Railway log lines. Never paste keys, tokens or your `.env`.**

## A. Before you deploy
1. `/bankset backup` and save the file. `python scripts/rehearse_upgrade.py <that file>` → must print `RESULT: PASS`.
2. `python scripts/check_startup.py` → four `OK` lines. (Run `pip install -r requirements.txt` first: discord.py must be 2.5 or newer.)
3. Replace the code (keep `.env`, `data/`, `.venv/`), commit, push.

## B. After the deploy
4. Railway log shows `Applied migration …` for any new ones (014 to 017 if you have not had them yet), then `Logged in as …`. No tracebacks.
5. `/bank unlock` if withdrawals are still paused from the earlier update. `/ledger dashboard` → state **NORMAL**, both chains intact.
6. `/bank holdings` (also the Vault button in `/help`) → shows, possibly across several messages. It must never error.
7. `/help` → click through every category and page. No button may fail. (Help messages sent before a restart go dead: run `/help` again.)
8. `python scripts/check_pnw_schema.py --nation <one of your members' ids>` → every required field `OK`; note whether the **stockpile is visible** and which projects PnW reports.
   Send me the output if it reports anything missing.

## C. Settings (nothing here moves money)
9. `/bankset config` → readable pages with labels, not codes. Change one setting by name and check the before/after card.
10. First tests of automatic deduction should be cautious: `/bankset config setting:Deduct external withdrawals value:review` (switch to automatic only once you trust it).
11. Reconciliation gate: `/bankset gate show`. Leave it ON unless you have a reason; if deposits from Discord are refused, `/ledger dashboard` says which finding blocks them.

## D. Money movement, smallest amounts first
12. **Deposit from Discord** (a member with a saved key): a small deposit. Expected: the PnW record appears, the balance rises, the member gets a DM.
13. **External withdrawal**: send a small amount from the offshore to a linked member in game. With mode `review` expect a card in the ECON channel and the record in `/bank review`;
    press **Charge to member**: the balance drops by exactly that amount and the member gets a DM saying it was *detected externally*. Scanning again must not charge it twice.
14. **Funded credit**: `/bank credit` from alliance funds for a small amount. Expect an approval request; a *different* Minister approves with `/bank approve`; run the same command again; check both sides in the result.
15. **Shared offshore**: if you used `/offshore wipe`, `/bank holdings` will show alliance-owned funds as $0 until you assign TUN's share again (`/offshore assign` or `/offshore adjustownership`).
    That is correct, not a fault.

## E. Transfers and grants (all start as preview-only)
16. `/grantset rules` → compare the rules with the game / Locutus. Correct any difference with `/grantset rule key value`.
17. Run a **preview** of each command for one real nation and compare with Locutus or the game: `/transfer raws`, `/grant warchest`, `/grant city` (try both `number_of_cities` and `up_to`),
    `/grant infrastructure`, `/grant land`. A preview never sends anything; the card ends with "PREVIEW ONLY".
18. **Project catalogue**: `/grantset projects` → fill the attached CSV with the real costs from the game → `/grantset projectimport`. Then `/grant project` previews should show the costs, the policy and the final cost.
    Import your domestic-policy discount table too (JSON), or previews keep saying it is unverified.
19. Switch on ONE kind: `/grantset live` → start with `WARCHEST_GRANT`. Send it to a single test nation with a small `ratio` (for example 0.1).
    Check: the PnW transfer exists, its note has `TUN-TX…` (and `#ignore` for alliance-funded), `/grantset batch batch_id:1` shows the calculation and the PnW record,
    and the same command run again within 10 minutes is refused.
20. Member-funded: with an ordinary ECON user (no `disburse_member_available`) → refused with a clear message. Give the permission with `/bankset setaccess` → works, and the member's balance drops once.
21. Repeat step 19 for the other kinds, one at a time, only after their previews matched the game.

## F. Optional
22. Icons: save the PnW PNGs into `icons/`, run `python scripts/install_pnw_icons.py`, then `/bankset iconpack`.
23. Moving to another Railway account later: follow guide 18.

## Things that are expected, not faults
* "PREVIEW ONLY": sending is off for that kind until an Admin switches it on.
* "Cost not loaded": the project catalogue has no costs until you import them.
* "ESTIMATE": an input PnW did not report was assumed; the card says which.
* A nation "left out for review": the card gives the reason; nothing was sent for it and the batch shows as *Partial*.
* "Withdrawals are paused by ECON": `/bank unlock`.
