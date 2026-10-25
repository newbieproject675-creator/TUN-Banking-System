"""Politics & War rules used to calculate transfers and grants. PURE FUNCTIONS: no database, no network, no Discord.

!!  RULES_VERSION is "unverified": these numbers are the author's best knowledge of the game and Locutus, and they could not be
!!  checked against the live game. EVERY number below is a key in DEFAULT_RULES and can be corrected from Discord with
!!  `/grantset rule` (no new deployment): see `/grantset rules` for the values in force. Every preview prints the version, the
!!  discounts/bonuses it applied, and a per-resource breakdown (uses / produces / net) to compare with Locutus.

RAWS follow Locutus: what a nation needs is its consumption MINUS what its own buildings produce (mines, farms, refineries,
with the project bonuses), over the number of days. Nothing is needed for a resource the nation produces enough of.
CASH is the nation's gross building + military upkeep: its tax income cannot be reconstructed from the API, so it is NOT subtracted
(use `no_cash`, or `only_missing` to net the cash it holds).
Not modelled (the preview says so): seasons and radiation (food), wartime effects, the nation's tax income.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from . import money as M

RULES_VERSION = "pnw-rules-2026-10-UNVERIFIED-2"
D = Decimal


def _build_defaults() -> dict:
    r: dict = {}
    # ---- power: capacity = infrastructure one plant can power; use = resource per infrastructure point per day
    r.update({"power.coal.capacity": 500, "power.coal.use_per_100_infra": "1.2", "power.oil.capacity": 500, "power.oil.use_per_100_infra": "1.2",
              "power.nuclear.capacity": 2000, "power.nuclear.use_per_1000_infra": "2.4", "power.wind.capacity": 250})
    # ---- factories (per building per day)
    r.update({"factory.oil_refinery.oil": 3, "factory.oil_refinery.gasoline": 6, "factory.steel_mill.coal": 3, "factory.steel_mill.iron": 3,
              "factory.steel_mill.steel": 9, "factory.aluminum_refinery.bauxite": 3, "factory.aluminum_refinery.aluminum": 9,
              "factory.munitions_factory.lead": 6, "factory.munitions_factory.munitions": 18})
    # ---- mines and farms (per building per day)
    for res in ("coal", "oil", "uranium", "iron", "bauxite", "lead"):
        r[f"mine.{res}"] = 3
    r.update({"farm.land_per_unit": 500, "farm.food_per_unit": 12})
    # ---- project bonuses to production (added to 1.0)
    r.update({"bonus.uranium_enrichment_program": 1, "bonus.iron_works": "0.36", "bonus.bauxite_works": "0.36", "bonus.arms_stockpile": "0.34",
              "bonus.emergency_gasoline_reserve": "0.36", "bonus.mass_irrigation": "0.25"})
    # ---- food
    r.update({"food.per_person": "0.001", "food.per_soldier": "0.002"})
    # ---- cash upkeep per building per day, and per military unit per day
    for b, v in {"coal_power": 1200, "oil_power": 1800, "nuclear_power": 10500, "wind_power": 500, "coal_mine": 400, "oil_well": 600,
                 "uranium_mine": 5000, "iron_mine": 1600, "bauxite_mine": 1600, "lead_mine": 1500, "farm": 300, "police_station": 750,
                 "hospital": 1000, "recycling_center": 2500, "subway": 3250, "supermarket": 600, "bank": 1800, "shopping_mall": 5400,
                 "stadium": 12150, "oil_refinery": 4000, "aluminum_refinery": 2500, "steel_mill": 4000, "munitions_factory": 3500,
                 "barracks": 1500, "factory": 3000, "hangar": 4000, "drydock": 5000}.items():
        r[f"upkeep.{b}"] = v
    for u, v in {"soldiers": "1.25", "tanks": 50, "aircraft": 500, "ships": 3375, "missiles": 21000, "nukes": 31500, "spies": 2400}.items():
        r[f"upkeep.military.{u}"] = v
    # ---- purchase prices
    r.update({"city.cubic": 50000, "city.linear": 150000, "city.constant": 75000,
              "infra.divisor": 710, "infra.exponent": "2.2", "infra.base": 300, "infra.offset": 10,
              "land.coefficient": "0.002", "land.offset": 20, "land.base": 50})
    # ---- discounts (a fraction taken off the price; the ones that apply are ADDED together)
    r.update({"discount.city.urban_planning": "0.05", "discount.city.advanced_urban_planning": "0.05", "discount.city.metropolitan_planning": "0.05",
              "discount.city.government_support_agency": "0.025", "discount.city.policy.MANIFEST_DESTINY": "0.05",
              "discount.infra.center_for_civil_engineering": "0.05", "discount.infra.advanced_engineering_corps": "0.05",
              "discount.land.center_for_civil_engineering": "0.05", "discount.land.advanced_engineering_corps": "0.05"})
    return {k: D(str(v)) for k, v in r.items()}


DEFAULT_RULES = _build_defaults()
_OVERRIDES: dict = {}


def load_overrides(text: str | None) -> None:
    """Install the admin's corrections (a JSON object of {rule key: number}). Unknown keys and bad values are ignored, never trusted."""
    _OVERRIDES.clear()
    try:
        raw = json.loads(text or "{}")
    except ValueError:
        return
    for k, v in (raw or {}).items():
        if k in DEFAULT_RULES:
            try:
                _OVERRIDES[k] = D(str(v))
            except Exception:  # noqa: BLE001
                pass


def overrides() -> dict:
    return dict(_OVERRIDES)


def rule(key: str) -> D:
    return _OVERRIDES.get(key, DEFAULT_RULES[key])


# ----------------------------------------------------------------------------------------------- derived structures
POWER_ORDER = ("wind_power", "nuclear_power", "coal_power", "oil_power")      # assumption when plant types are mixed in one city
ALL_BUILDINGS = tuple(k[len("upkeep."):] for k in DEFAULT_RULES if k.startswith("upkeep.") and not k.startswith("upkeep.military."))
MILITARY_CASH_UPKEEP = {k[len("upkeep.military."):]: v for k, v in DEFAULT_RULES.items() if k.startswith("upkeep.military.")}
PRODUCTION_PROJECTS = ("uranium_enrichment_program", "iron_works", "bauxite_works", "arms_stockpile", "emergency_gasoline_reserve", "mass_irrigation")
CITY_DISCOUNT_PROJECTS = tuple(k.split(".")[-1] for k in DEFAULT_RULES if k.startswith("discount.city.") and ".policy." not in k)
INFRA_DISCOUNT_PROJECTS = tuple(k.split(".")[-1] for k in DEFAULT_RULES if k.startswith("discount.infra."))
LAND_DISCOUNT_PROJECTS = tuple(k.split(".")[-1] for k in DEFAULT_RULES if k.startswith("discount.land."))
ALL_PROJECTS = tuple(dict.fromkeys(PRODUCTION_PROJECTS + CITY_DISCOUNT_PROJECTS + INFRA_DISCOUNT_PROJECTS + LAND_DISCOUNT_PROJECTS))


def city_price(current_cities: int) -> D:
    """Price of the NEXT city when the nation owns `current_cities` (before discounts)."""
    c = D(current_cities)
    return rule("city.cubic") * (c - 1) ** 3 + rule("city.linear") * c + rule("city.constant")


def infra_unit_price(level) -> D:
    """Price of one infrastructure point bought while at `level` (before discounts)."""
    return D(str(abs(float(level) - float(rule("infra.offset"))) ** float(rule("infra.exponent")) / float(rule("infra.divisor"))
                 + float(rule("infra.base"))))


def land_unit_price(level) -> D:
    """Price of one acre bought while at `level` (before discounts)."""
    return D(str(float(rule("land.coefficient")) * (float(level) - float(rule("land.offset"))) ** 2 + float(rule("land.base"))))


def sheet() -> str:
    """Every rule in force, for ECON to compare with the game and correct with /grantset rule."""
    out = [f"Rules version: {RULES_VERSION}", "Format: key = value   (* = changed from the built-in value; change with /grantset rule)"]
    group = None
    for k in sorted(DEFAULT_RULES):
        if k.split(".")[0] != group:
            group = k.split(".")[0]
            out += ["", f"[{group}]"]
        mark = "*" if k in _OVERRIDES else " "
        out.append(f" {mark} {k} = {format(rule(k).normalize(), 'f')}")
    out += ["", "How they are used:",
            "  RAWS  need per day = consumption - own production, per resource; x days; rounded UP. Production needs the city to be powered.",
            "        farm food = farms x land/land_per_unit x food_per_unit x (1 + mass_irrigation); mine output x (1 + project bonus).",
            "        food use = people x food.per_person + soldiers x food.per_soldier. Cash = gross building + military upkeep.",
            "  CITY  next city = cubic*(c-1)^3 + linear*c + constant (c = cities owned); discounts are added together and taken off.",
            "  INFRA per point = base + |level-offset|^exponent / divisor, point by point from each city's own level.",
            "  LAND  per acre = base + coefficient*(level-offset)^2, acre by acre from each city's own land.",
            "Not modelled: seasons/radiation, wartime effects, tax income."]
    return "\n".join(out)


# ============================================================================================== results
@dataclass
class Calc:
    """The outcome for ONE nation. `review` non-empty = cannot be trusted -> must not be sent, ECON decides."""
    nation_id: int
    amounts: dict = field(default_factory=dict)          # what to send, in units (hundredths)
    requirement: dict = field(default_factory=dict)      # what the rules say is needed, in units
    stock: dict | None = None                            # verified current stockpile in units, None = not available
    lines: list = field(default_factory=list)            # human-readable calculation, one line each
    assumptions: list = field(default_factory=list)
    review: list = field(default_factory=list)
    estimate: bool = False                               # True = an input was assumed; not presented as exact
    inputs: dict = field(default_factory=dict)           # raw numbers used, stored with the transaction

    @property
    def sendable(self) -> bool:
        return not self.review


def clean(d) -> str:
    """A Decimal as plain text without trailing zeros: 1.0 -> "1", 10 -> "10", 1.50 -> "1.5"."""
    return format(D(str(d)).normalize(), "f")


def _units(x: Decimal, *, up: bool = True) -> int:
    """Decimal PnW amount -> units (hundredths); rounded UP to a whole in-game unit so a nation is never short."""
    if up:
        return int((x.to_integral_value(rounding="ROUND_CEILING")) * M.SCALE)
    return int((x * M.SCALE).quantize(D(1), rounding=ROUND_HALF_UP))


def stock_units(nation: dict) -> dict | None:
    s = nation.get("stock")
    if not s or any(s.get(r) is None for r in M.RESOURCES):
        return None
    return {r: M.to_units(s[r]) for r in M.RESOURCES}


_stock_units = stock_units


def _cities_ok(nation: dict, calc: Calc) -> list | None:
    cities = nation.get("cities")
    n = nation.get("num_cities")
    if not n:
        calc.review.append("the number of cities is unknown")
        return None
    if not cities or len(cities) != n:
        calc.review.append(f"city data is incomplete ({len(cities or [])} of {n} cities returned by PnW)")
        return None
    return cities


def _project(nation: dict, name: str, calc: Calc, *, note: bool = True):
    """True/False for a project, or None (unknown). Unknown is treated as 'does not have it' and the result becomes an ESTIMATE."""
    v = (nation.get("projects") or {}).get(name)
    if v is None and note:
        calc.estimate = True
        msg = f"ESTIMATE: PnW did not report the project {name}; treated as NOT owned"
        if msg not in calc.assumptions:
            calc.assumptions.append(msg)
    return bool(v)


def _policy(nation: dict, calc: Calc) -> str:
    p = nation.get("policy")
    if p is None:
        calc.estimate = True
        calc.assumptions.append("ESTIMATE: PnW did not report the domestic policy; no policy discount applied")
        return ""
    return str(p).strip().upper().replace(" ", "_")


# ============================================================================================== raws
def compute_raws(nation: dict, days, *, include_cash: bool = True, only_missing: bool = False) -> Calc:
    """What a nation needs to keep operating for `days` days: its consumption minus its own production, from its ACTUAL buildings."""
    days = D(str(days))
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "NET of the nation's own production (mines, farms, refineries, with project bonuses), like Locutus",
                      "Seasons, radiation and wartime effects are not applied" + ("; cash is GROSS upkeep (tax income is not subtracted)" if include_cash else "")]
    uses = {r: D(0) for r in M.RESOURCES}
    makes = {r: D(0) for r in M.RESOURCES}
    mine_of = {"coal": "coal_mine", "oil": "oil_well", "uranium": "uranium_mine", "iron": "iron_mine", "bauxite": "bauxite_mine", "lead": "lead_mine"}
    cities = _cities_ok(nation, c)
    if cities is not None:
        bonus = {p: (rule(f"bonus.{p}") if _project(nation, p, c) else D(0)) for p in PRODUCTION_PROJECTS}
        if any(bonus.values()):
            c.lines.append("Production bonuses: " + ", ".join(f"{p} +{int(v * 100)}%" for p, v in bonus.items() if v))
        plants = {"wind_power": (None, rule("power.wind.capacity"), D(0)),
                  "nuclear_power": ("uranium", rule("power.nuclear.capacity"), rule("power.nuclear.use_per_1000_infra") / 1000),
                  "coal_power": ("coal", rule("power.coal.capacity"), rule("power.coal.use_per_100_infra") / 100),
                  "oil_power": ("oil", rule("power.oil.capacity"), rule("power.oil.use_per_100_infra") / 100)}
        for city in cities:
            infra, land, b = city.get("infrastructure"), city.get("land"), city.get("buildings")
            if infra is None or land is None or b is None or any(k not in b for k in ALL_BUILDINGS):
                c.review.append(f"city #{city.get('id')} has incomplete building data")
                continue
            remaining = D(str(infra))
            notes = []
            capacity = sum(plants[p][1] * b[p] for p in plants)
            powered = capacity >= infra
            for plant in POWER_ORDER:
                res, cap, rate = plants[plant]
                carried = min(remaining, cap * b[plant])
                if carried > 0:
                    remaining -= carried
                    if res:
                        uses[res] += carried * rate
                        notes.append(f"{plant} x{b[plant]} burns {carried * rate:.2f} {res}")
            if len([p for p in plants if b[p]]) > 1:
                c.assumptions.append(f"city #{city.get('id')}: mixed power plants, filled in the order {', '.join(POWER_ORDER)}")
            if not powered:
                c.assumptions.append(f"city #{city.get('id')} is not fully powered (infra {infra} > capacity {capacity}); its buildings are idle: "
                                     "no production and no inputs counted")
            else:
                for res, bld in mine_of.items():
                    if b[bld]:
                        makes[res] += b[bld] * rule(f"mine.{res}") * (1 + (bonus["uranium_enrichment_program"] if res == "uranium" else 0))
                if b["farm"]:
                    makes["food"] += b["farm"] * (D(str(land)) / rule("farm.land_per_unit")) * rule("farm.food_per_unit") * (1 + bonus["mass_irrigation"])
                for fac, out_res, proj in (("oil_refinery", "gasoline", "emergency_gasoline_reserve"), ("steel_mill", "steel", "iron_works"),
                                           ("aluminum_refinery", "aluminum", "bauxite_works"), ("munitions_factory", "munitions", "arms_stockpile")):
                    if b[fac]:
                        for k in DEFAULT_RULES:
                            if k.startswith(f"factory.{fac}.") and k.split(".")[-1] != out_res:
                                uses[k.split(".")[-1]] += b[fac] * rule(k)
                        makes[out_res] += b[fac] * rule(f"factory.{fac}.{out_res}") * (1 + bonus[proj])
            c.lines.append(f"City #{city.get('id')} (infra {infra}, land {land}): " + ("; ".join(notes) if notes else "no fuel burned"))
            if include_cash:
                uses["money"] += sum(rule(f"upkeep.{k}") * b[k] for k in ALL_BUILDINGS)
    pop, mil = nation.get("population"), nation.get("military")
    if pop is None:
        c.review.append("the population is unknown, so food cannot be calculated")
    elif mil is None or mil.get("soldiers") is None:
        c.review.append("the military numbers are unknown, so food and cash upkeep cannot be calculated")
    else:
        uses["food"] += D(str(pop)) * rule("food.per_person") + D(str(mil["soldiers"])) * rule("food.per_soldier")
    if include_cash:
        if mil is None or any(mil.get(u) is None for u in MILITARY_CASH_UPKEEP):
            if not any("military numbers" in r for r in c.review):
                c.review.append("the military numbers are unknown, so military cash upkeep cannot be calculated")
        else:
            uses["money"] += sum(rule(f"upkeep.military.{u}") * D(str(mil[u])) for u in MILITARY_CASH_UPKEEP)
    need_day = {r: max(D(0), uses[r] - makes[r]) for r in M.RESOURCES}
    for r in M.RESOURCES:
        if uses[r] or makes[r]:
            net = makes[r] - uses[r]
            c.lines.append(f"{M.LABELS[r]}: uses {uses[r]:.2f}/day, produces {makes[r]:.2f}/day -> "
                           + (f"needs {-net:.2f}/day" if net < 0 else f"surplus {net:.2f}/day (nothing to send)"))
    c.requirement = {r: _units(need_day[r] * days) for r in M.RESOURCES if need_day[r] * days > 0}
    c.inputs = {"days": clean(days), "uses_per_day": {r: str(v) for r, v in uses.items() if v}, "produces_per_day": {r: str(v) for r, v in makes.items() if v},
                "rules": RULES_VERSION, "overrides": {k: str(v) for k, v in _OVERRIDES.items()}, "cities": nation.get("num_cities"), "include_cash": include_cash}
    c.stock = stock_units(nation)
    c.amounts = dict(c.requirement)
    if only_missing:
        if c.stock is None:
            c.review.append("the current stockpile is not available, so only the missing amount cannot be verified")
        else:
            c.amounts = {r: max(0, a - c.stock.get(r, 0)) for r, a in c.requirement.items()}
            c.amounts = {r: a for r, a in c.amounts.items() if a > 0}
    if not c.review and not c.requirement:
        c.lines.append("Nothing is needed.")
    return c


def apply_daily_cash_rule(calc: Calc, nation: dict, threshold_dollars: int) -> None:
    """`no_daily_cash`: never send cash beyond what keeps the nation at or below the daily-bonus threshold. Only ever REDUCES cash.

    Interpretation (documented, please confirm): cash is sent only up to (threshold - current cash), none if already at/above it.
    """
    if "money" not in calc.amounts:
        return
    if threshold_dollars <= 0:
        calc.review.append("no_daily_cash was requested but the daily-bonus threshold is not configured (/grantset dailycash)")
        return
    cash = (nation.get("stock") or {}).get("money")
    if cash is None:
        calc.review.append("the nation's current cash is not available, so the daily-bonus rule cannot be applied")
        return
    room = max(0, M.to_units(threshold_dollars) - M.to_units(cash))
    before = calc.amounts["money"]
    allowed = min(before, room)
    if allowed <= 0:
        calc.amounts.pop("money")
    else:
        calc.amounts["money"] = allowed
    calc.lines.append(f"Daily-bonus rule: cash capped at ${M.units_to_float(allowed):,.0f} "
                      f"(threshold ${threshold_dollars:,}, nation has ${float(cash):,.0f}); was ${M.units_to_float(before):,.0f}")


# ============================================================================================== warchest
MAX_RATIO = D(10)


def compute_warchest(nation: dict, per_city: dict, ratio, *, only_missing: bool = False) -> Calc:
    """per_city: {resource: units per city} from the ECON-configured setting. Example: food 1000, steel 100 at ratio 2, 12 cities
    -> food 24,000 and steel 2,400."""
    c = Calc(nation_id=nation["id"])
    ratio = D(str(ratio))
    if ratio <= 0 or ratio > MAX_RATIO:
        c.review.append(f"the ratio must be above 0 and at most {MAX_RATIO}")
        return c
    n = nation.get("num_cities")
    if not n:
        c.review.append("the number of cities is unknown")
        return c
    if not per_city:
        c.review.append("no warchest amounts are configured (/grantset warchest)")
        return c
    for r, u in per_city.items():
        c.requirement[r] = int((D(u) * n * ratio).quantize(D(1), rounding=ROUND_HALF_UP))
        c.lines.append(f"{M.LABELS[r]}: {M.fmt_units(r, u)} per city x {n} cities x ratio {ratio.normalize():f} = {M.fmt_units(r, c.requirement[r])}")
    c.stock = stock_units(nation)
    c.amounts = dict(c.requirement)
    if only_missing:
        if c.stock is None:
            c.review.append("the current stockpile is not available, so only the missing amount cannot be verified")
        else:
            c.amounts = {r: max(0, a - c.stock.get(r, 0)) for r, a in c.requirement.items()}
            c.amounts = {r: a for r, a in c.amounts.items() if a > 0}
            c.lines.append("Deducted the verified stockpile: " + ", ".join(
                f"{M.LABELS[r]} {M.fmt_units(r, c.stock.get(r, 0))}" for r in c.requirement))
    c.inputs = {"cities": n, "ratio": clean(ratio), "per_city": per_city, "only_missing": only_missing, "rules": "config"}
    return c


# ============================================================================================== city / infrastructure / land
def _discount(nation: dict, kind: str, projects: tuple, calc: Calc, *, policies: bool = False) -> D:
    total = D(0)
    for p in projects:
        if _project(nation, p, calc):
            pct = rule(f"discount.{kind}.{p}")
            if pct:
                total += pct
                calc.lines.append(f"Discount: {p} -{(pct * 100).normalize():f}%")
    if policies:
        pol = _policy(nation, calc)
        key = f"discount.{kind}.policy.{pol}"
        if pol and key in DEFAULT_RULES and rule(key):
            total += rule(key)
            calc.lines.append(f"Discount: domestic policy {pol} -{(rule(key) * 100).normalize():f}%")
    return total


MAX_CITIES_PER_GRANT = 50          # a typo guard only; the real limit on money is ECON's per-nation cap (/grantset citycap)


def compute_city(nation: dict, how_many: int | None = None, up_to: int | None = None) -> Calc:
    """The money to buy cities. `how_many` = that many NEW cities; `up_to` = buy until the nation owns that many (up_to=25 for a
    24-city nation is just the 25th city; for a 20-city nation it is cities 21 to 25)."""
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Discounts are added together, then taken off the price"]
    n = nation.get("num_cities")
    if not n:
        c.review.append("the number of cities is unknown")
        return c
    if up_to is not None:
        if up_to <= n:
            c.review.append(f"the nation already has {n} cities, so there is no city to buy up to {up_to}")
            return c
        how_many = up_to - n
        c.lines.append(f"Buying up to city #{up_to}: the nation has {n}, so {how_many} new city(ies) (#{n + 1} to #{up_to})")
    how_many = 1 if how_many is None else how_many
    if how_many < 1 or how_many > MAX_CITIES_PER_GRANT:
        c.review.append(f"the number of cities must be between 1 and {MAX_CITIES_PER_GRANT}")
        return c
    disc = _discount(nation, "city", CITY_DISCOUNT_PROJECTS, c, policies=True)
    total = D(0)
    for k in range(how_many):
        base = city_price(n + k)
        price = base * (1 - disc)
        total += price
        c.lines.append(f"City {n + k + 1}: base ${base:,.0f}" + (f" - {(disc * 100).normalize():f}% = ${price:,.0f}" if disc else f" = ${price:,.0f}"))
    c.requirement = c.amounts = {"money": _units(total)}
    c.inputs = {"cities_now": n, "buying": how_many, "up_to": up_to, "discount": str(disc), "rules": RULES_VERSION,
                "overrides": {k: str(v) for k, v in _OVERRIDES.items()}}
    return c


def _price_range(unit_price, start: int, end: int) -> D:
    return sum((unit_price(x) for x in range(int(start), int(end))), D(0))


def compute_infra(nation: dict, target: int) -> Calc:
    """Money to raise every city's infrastructure to `target` (cities already at or above it cost nothing)."""
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Priced point by point from each city's own infrastructure level"]
    cities = _cities_ok(nation, c)
    if cities is None:
        return c
    disc = _discount(nation, "infra", INFRA_DISCOUNT_PROJECTS, c)
    total = D(0)
    for city in cities:
        cur = city.get("infrastructure")
        if cur is None:
            c.review.append(f"city #{city.get('id')} has no infrastructure level")
            continue
        if cur >= target:
            c.lines.append(f"City #{city.get('id')}: {cur} (already at or above {target})")
            continue
        cost = _price_range(infra_unit_price, cur, target) * (1 - disc)
        total += cost
        c.lines.append(f"City #{city.get('id')}: {cur} -> {target} = ${cost:,.0f}")
    c.requirement = c.amounts = {"money": _units(total)} if total > 0 else {}
    c.inputs = {"target": target, "cities": len(cities), "discount": str(disc), "rules": RULES_VERSION, "overrides": {k: str(v) for k, v in _OVERRIDES.items()}}
    if not c.review and total <= 0:
        c.lines.append("Every city is already at or above the target: nothing to grant.")
    return c


def compute_land(nation: dict, target: int) -> Calc:
    c = Calc(nation_id=nation["id"])
    c.assumptions += [f"Rules {RULES_VERSION}", "Priced acre by acre from each city's own land"]
    cities = _cities_ok(nation, c)
    if cities is None:
        return c
    disc = _discount(nation, "land", LAND_DISCOUNT_PROJECTS, c)
    total = D(0)
    for city in cities:
        cur = city.get("land")
        if cur is None:
            c.review.append(f"city #{city.get('id')} has no land amount")
            continue
        if cur >= target:
            c.lines.append(f"City #{city.get('id')}: {cur} (already at or above {target})")
            continue
        cost = _price_range(land_unit_price, math.floor(cur), math.ceil(target)) * (1 - disc)
        total += cost
        c.lines.append(f"City #{city.get('id')}: {cur} -> {target} acres = ${cost:,.0f}")
    c.requirement = c.amounts = {"money": _units(total)} if total > 0 else {}
    c.inputs = {"target": target, "cities": len(cities), "discount": str(disc), "rules": RULES_VERSION, "overrides": {k: str(v) for k, v in _OVERRIDES.items()}}
    if not c.review and total <= 0:
        c.lines.append("Every city is already at or above the target: nothing to grant.")
    return c
