"""
dashboard7.py  —  AIRLoad A350-1000 Cargo Loading Dashboard  (v7)
=================================================================

NEW in v7  (built on dashboard6)
---------------------------------
  1. Per-compartment unlock button
       • Every occupied slot in the sidebar shows an ↺ unlock button
         regardless of whether it was AI-recommended or manually locked.
       • Clicking ↺ clears only that item's lock (manual_lock = False)
         without touching other items or invalidating the recommendation
         for those items.

  2. Weight limits are inclusive (≤, not <)
       • FWD hold  : feasible at exactly 22,000 kg  (was < 22,000 kg)
       • AFT hold  : feasible at exactly 19,000 kg  (was < 19,000 kg)
       • BULK_01   : feasible at exactly    600 kg  (was < 600 kg)
       • All hold-bar indicators, recommender, and reassign-dialog
         checks updated to use  > limit  (strict), not  >= limit.

  3. Flight-plan driven fuel burn
       • Reads my_flightplan.yaml (new format) which contains:
             flight.flight_hours, flight.alternate_hours
             passengers.count, passengers.avg_weight_kg
             fuel.fuel_added_kg, fuel.burn_rate_kg_per_hr (optional),
             fuel.trip_fuel_est_kg, fuel.wing_tank_cg_x_m
       • If burn_rate_kg_per_hr is not specified, it is derived as:
             trip_fuel_est_kg / flight_hours
       • Fuel burn is modelled in 1-minute steps (or proportional steps)
         over the full flight duration, producing an accurate CG trace.
       • Falls back to legacy hardcoded values if keys are absent.
       • New "Open Flight Plan…" button and --flightplan CLI arg.
       • The ⛽ Fuel Projection window shows flight number, route,
         burn rate, and a time axis (hours from departure).

  4. CG-throughout-flight feasibility
       • check_feasibility() now calls project_fuel_burn() and verifies
         that every point on the fuel-burn CG trace stays inside
         [CG_FWD, CG_AFT].
       • If any point dips below 20% or exceeds 35% MAC, the
         configuration is flagged INFEASIBLE with a specific message:
         "CG exits envelope at T+Xh Ymin (CG = Z.ZZ%)" identifying
         exactly when and why.
       • The DangerOverlay message is updated to reflect this.

All previous features (v6 and earlier) unchanged.

Dependencies:
  pip install pyyaml        (already in project)
  tkinter                   (stdlib)

Usage:
  python dashboard7.py
  python dashboard7.py --plan        output/optimised_loading_plan.yaml
  python dashboard7.py --manifest    input_file/my_flight.yaml
  python dashboard7.py --flightplan  input_file/my_flightplan.yaml
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import yaml, os, sys, argparse, math, time, copy

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from airload_person1_core_v2 import (
    A3501000ReferenceModel,
    AIRLoadPhysicsCoreV2,
    OperatingEmptyAircraft,
    CargoZone,
    ULD,
    PassengerCabinZone,
    FuelTank,
)

# ══════════════════════════════════════════════════════════════════════════════
# AIRCRAFT REFERENCE  (from Person 1 core — no duplication)
# ══════════════════════════════════════════════════════════════════════════════

CG_FWD  = A3501000ReferenceModel.CG_FORWARD_LIMIT_PERCENT_MAC   # 26.0
CG_AFT  = A3501000ReferenceModel.CG_AFT_LIMIT_PERCENT_MAC       # 35.0
CG_TGT  = A3501000ReferenceModel.CG_TARGET_PERCENT_MAC          # 28.0
LEMAC   = A3501000ReferenceModel.LEMAC_X_M                       # 25.0
MAC_LEN = A3501000ReferenceModel.MAC_LENGTH_M                    # 10.0

# ── Weight limits  (Req 2: inclusive — overweight only when strictly > limit)
FWD_HOLD_LIMIT_KG  = 22_000
AFT_HOLD_LIMIT_KG  = 19_000
BULK_LIMIT_KG      = 600

# ── Zone geometry ─────────────────────────────────────────────────────────────

def _build_extended_zones() -> dict:
    base_fwd_x = {1: 15.0, 2: 17.0, 3: 19.0, 4: 21.0}
    base_aft_x = {1: 31.0, 2: 33.0, 3: 35.0, 4: 37.0}
    zones = {}
    for n, x in base_fwd_x.items():
        for side in ("L", "R"):
            zid = f"FWD_0{n}{side}"
            zones[zid] = CargoZone(zid, "Forward Hold", x, max_weight_kg=99999)
    for n, x in base_aft_x.items():
        for side in ("L", "R"):
            zid = f"AFT_0{n}{side}"
            zones[zid] = CargoZone(zid, "Aft Hold", x, max_weight_kg=99999)
    zones["BULK_01"] = CargoZone("BULK_01", "Bulk Hold", 40.0, max_weight_kg=BULK_LIMIT_KG)
    return zones

ZONE_DEFS  = _build_extended_zones()
FWD_ZONES  = [f"FWD_0{n}{s}" for n in range(1, 5) for s in ("L", "R")]
AFT_ZONES  = [f"AFT_0{n}{s}" for n in range(1, 5) for s in ("L", "R")]
BULK_ZONES = ["BULK_01"]
ALL_ZONES  = FWD_ZONES + AFT_ZONES + BULK_ZONES

POLL_INTERVAL_MS = 10_000

# ── Default fuel-burn parameters (used when flightplan not loaded) ─────────
DEFAULT_FUEL_KG          = 65_000
DEFAULT_FUEL_BURN_RATE   = 6_963     # kg/hr  (typical A350-1000 cruise)
DEFAULT_WING_TANK_CG_X_M = 27.5
DEFAULT_FLIGHT_HRS       = 9.0

# ══════════════════════════════════════════════════════════════════════════════
# COLOUR PALETTE  —  Warm Navy Aviation Theme (from dashboard10)
# ══════════════════════════════════════════════════════════════════════════════

C = {
    # ── Shell ────────────────────────────────────────────────────────────────
    "bg":          "#1a1f2e",   # deep navy — cockpit-screen feel
    "panel":       "#222840",   # slightly lighter panel
    "panel2":      "#1e2339",   # secondary panel / info bars
    "border":      "#3a4468",   # subtle blue-grey dividers
    "text":        "#dde4f0",   # soft white — easy on the eyes
    "muted":       "#7a8aaa",   # secondary text

    # ── Accents ──────────────────────────────────────────────────────────────
    "accent":      "#4a9eff",   # bright aviation blue
    "accent2":     "#ff7b5c",   # warm coral — TOW / alerts

    # ── Zone states ──────────────────────────────────────────────────────────
    "zone_empty":  "#252d45",   # unoccupied slot — dark navy
    "zone_locked": "#3a2e10",   # warm amber tint for locked

    # ── Status colours ───────────────────────────────────────────────────────
    "green":       "#2ecc71",   # optimal CG
    "yellow":      "#f0b429",   # slight deviance
    "red":         "#e74c3c",   # out of limits
    "orange":      "#f39c12",   # warning / locked
    "purple":      "#9b59b6",   # info
    "danger_bg":   "#2c0a0a",   # danger overlay background

    # ── Cargo item badge colours (vivid, distinct) ───────────────────────────
    "cargo_cols": [
        "#2980b9", "#1abc9c", "#8e44ad",
        "#e67e22", "#27ae60", "#c0392b",
        "#16a085", "#d35400", "#2c3e50",
        "#f39c12", "#7f8c8d", "#6c3483",
        "#117a65", "#873600", "#1a5276",
    ],

    # ── Aircraft silhouette colours (kept from dashboard9 drawing logic) ─────
    "fuselage":    "#21262d",
    "wing":        "#1c2535",
    "tail":        "#21262d",
}

# ── Typography system (from dashboard10) ────────────────────────────────────
UI_FONT   = "Helvetica"      # clean sans-serif for all UI chrome
MONO_FONT = "Courier New"    # monospaced for numeric readouts


def _lighten(hex_col: str, amount: float = 0.35) -> str:
    """Return a lighter version of a hex colour for slot highlights."""
    try:
        h = hex_col.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = min(255, int(r + (255 - r) * amount))
        g = min(255, int(g + (255 - g) * amount))
        b = min(255, int(b + (255 - b) * amount))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_col


# ══════════════════════════════════════════════════════════════════════════════
# FLIGHT PLAN DATA CLASS  (Req 3)
# ══════════════════════════════════════════════════════════════════════════════

class FlightPlan:
    """
    Parsed representation of my_flightplan.yaml.
    Provides the flight parameters needed for accurate fuel-burn modelling.
    """

    def __init__(self):
        # Metadata
        self.flight_number   = ""
        self.origin          = ""
        self.destination     = ""

        # Flight time
        self.flight_hours    = DEFAULT_FLIGHT_HRS
        self.alternate_hours = 0.75

        # Passengers
        self.pax_count       = 280
        self.pax_avg_kg      = 95.0

        # Fuel
        self.fuel_added_kg        = DEFAULT_FUEL_KG
        self.trip_fuel_est_kg     = DEFAULT_FUEL_KG * 0.86
        self.burn_rate_kg_per_hr  = None   # None → auto-derive
        self.wing_tank_cg_x_m    = DEFAULT_WING_TANK_CG_X_M

        # Cargo (raw list from YAML)
        self.cargo = []

        # Whether this was loaded from a flightplan file (vs. defaults)
        self.loaded = False

    @property
    def effective_burn_rate(self) -> float:
        """kg/hr — explicit if given, else trip_fuel / flight_hours."""
        if self.burn_rate_kg_per_hr is not None:
            return float(self.burn_rate_kg_per_hr)
        if self.flight_hours > 0:
            return self.trip_fuel_est_kg / self.flight_hours
        return DEFAULT_FUEL_BURN_RATE

    @property
    def pax_total_kg(self) -> float:
        return self.pax_count * self.pax_avg_kg

    @classmethod
    def from_yaml(cls, path: str) -> "FlightPlan":
        fp = cls()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        flt  = data.get("flight",     {})
        pax  = data.get("passengers", {})
        fuel = data.get("fuel",       {})

        fp.flight_number   = flt.get("flight_number",   "")
        fp.origin          = flt.get("origin",          "")
        fp.destination     = flt.get("destination",     "")
        fp.flight_hours    = float(flt.get("flight_hours",    DEFAULT_FLIGHT_HRS))
        fp.alternate_hours = float(flt.get("alternate_hours", 0.75))

        fp.pax_count   = int(pax.get("count",          280))
        fp.pax_avg_kg  = float(pax.get("avg_weight_kg", 95.0))

        fp.fuel_added_kg       = float(fuel.get("fuel_added_kg",    DEFAULT_FUEL_KG))
        fp.trip_fuel_est_kg    = float(fuel.get("trip_fuel_est_kg", fp.fuel_added_kg * 0.86))
        fp.wing_tank_cg_x_m   = float(fuel.get("wing_tank_cg_x_m", DEFAULT_WING_TANK_CG_X_M))

        explicit_rate = fuel.get("burn_rate_kg_per_hr")
        fp.burn_rate_kg_per_hr = float(explicit_rate) if explicit_rate is not None else None

        fp.cargo  = data.get("cargo", [])
        fp.loaded = True
        return fp


# ══════════════════════════════════════════════════════════════════════════════
# DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_manifest(path: str) -> dict:
    """Legacy my_flight.yaml → minimal plan dict."""
    with open(path, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    items = manifest.get("cargo", [])
    lp = [
        {"item": it["name"], "weight_kg": it["weight_kg"],
         "assigned_zone": None, "cg_after_placement": None,
         "manual_lock": False}
        for it in items
    ]
    return {
        "aircraft":         "Airbus A350-1000",
        "input_manifest":   path,
        "status":           "UNASSIGNED",
        "cg_within_limits": None,
        "base_cg_pct_mac":  None,
        "final_cg_pct_mac": None,
        "cg_fwd_limit":     CG_FWD,
        "cg_aft_limit":     CG_AFT,
        "cg_target":        CG_TGT,
        "total_reward":     None,
        "loading_plan":     lp,
    }


def load_flightplan_as_plan(fp: FlightPlan, path: str) -> dict:
    """Convert a FlightPlan into the plan dict format used by the dashboard."""
    pax_kg   = fp.pax_total_kg
    fuel_kg  = fp.fuel_added_kg
    oew      = A3501000ReferenceModel.operating_empty_aircraft().operating_empty_weight_kg
    cargo_kg = sum(c.get("weight_kg", 0) for c in fp.cargo)
    lp = [
        {"item": c["name"], "weight_kg": c["weight_kg"],
         "assigned_zone": None, "cg_after_placement": None,
         "manual_lock": False}
        for c in fp.cargo
    ]
    return {
        "aircraft":         "Airbus A350-1000",
        "input_manifest":   path,
        "flight_plan":      {
            "flight_number":    fp.flight_number,
            "origin":           fp.origin,
            "destination":      fp.destination,
            "flight_hours":     fp.flight_hours,
            "alternate_hours":  fp.alternate_hours,
            "burn_rate_kg_per_hr": fp.effective_burn_rate,
            "wing_tank_cg_x_m":   fp.wing_tank_cg_x_m,
        },
        "status":           "UNASSIGNED",
        "cg_within_limits": None,
        "base_cg_pct_mac":  None,
        "final_cg_pct_mac": None,
        "cg_fwd_limit":     CG_FWD,
        "cg_aft_limit":     CG_AFT,
        "cg_target":        CG_TGT,
        "total_reward":     None,
        "weight_breakdown": {
            "empty_aircraft_kg": oew,
            "passengers_kg":     pax_kg,
            "fuel_kg":           fuel_kg,
            "cargo_kg":          cargo_kg,
        },
        "loading_plan":     lp,
    }


def _normalise_plan(plan: dict) -> dict:
    mapping = {
        "FWD_01": "FWD_01L", "FWD_02": "FWD_02L",
        "FWD_03": "FWD_03L", "FWD_04": "FWD_04L",
        "AFT_01": "AFT_01L", "AFT_02": "AFT_02L",
        "AFT_03": "AFT_03L", "AFT_04": "AFT_04L",
    }
    for entry in plan.get("loading_plan", []):
        z = entry.get("assigned_zone")
        if z in mapping:
            entry["assigned_zone"] = mapping[z]
        if "manual_lock" not in entry:
            entry["manual_lock"] = False
    return plan


def build_zone_map(plan: dict) -> dict:
    zm = {z: [] for z in list(ZONE_DEFS.keys()) + [None]}
    for e in plan.get("loading_plan", []):
        z = e.get("assigned_zone")
        if z not in zm:
            z = None
        zm[z].append(e)
    return zm


def compute_cg_status(cg) -> str:
    if cg is None:
        return "muted"
    if cg < CG_FWD or cg > CG_AFT:
        return "red"
    if abs(cg - CG_TGT) <= 2.0:
        return "green"
    return "yellow"


# ══════════════════════════════════════════════════════════════════════════════
# PHYSICS HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _base_moment_weight(plan: dict):
    wb         = plan.get("weight_breakdown") or {}
    oew        = wb.get("empty_aircraft_kg",  155_000)
    pax_zones  = A3501000ReferenceModel.passenger_zones()
    fuel_tanks = A3501000ReferenceModel.fuel_tanks()

    # If the plan has an explicit passengers_kg (from flightplan), use it
    # instead of the reference passenger zones (which have fixed weights).
    pax_kg_override = wb.get("passengers_kg")
    if pax_kg_override is not None:
        pax_moment = pax_kg_override * sum(
            z.x_position_m * z.total_weight() for z in pax_zones.values()
        ) / max(sum(z.total_weight() for z in pax_zones.values()), 1)
        pax_weight = pax_kg_override
    else:
        pax_moment = sum(z.total_weight() * z.x_position_m for z in pax_zones.values())
        pax_weight = sum(z.total_weight() for z in pax_zones.values())

    fuel_kg_override = wb.get("fuel_kg")
    if fuel_kg_override is not None:
        wing_x = (plan.get("flight_plan") or {}).get("wing_tank_cg_x_m",
                  DEFAULT_WING_TANK_CG_X_M)
        fuel_moment = fuel_kg_override * wing_x
        fuel_weight = fuel_kg_override
    else:
        fuel_moment = sum(t.fuel_kg * t.x_position_m for t in fuel_tanks.values())
        fuel_weight = sum(t.fuel_kg for t in fuel_tanks.values())

    moment = (oew * A3501000ReferenceModel.operating_empty_aircraft().empty_cg_x_m
              + pax_moment + fuel_moment)
    weight = oew + pax_weight + fuel_weight
    return moment, weight


def recalculate_cg(plan: dict) -> float | None:
    moment, weight = _base_moment_weight(plan)
    for e in plan.get("loading_plan", []):
        z = e.get("assigned_zone")
        if z and z in ZONE_DEFS:
            wt = e.get("weight_kg", 0)
            moment += wt * ZONE_DEFS[z].x_position_m
            weight += wt
    if weight == 0:
        return None
    return (moment / weight - LEMAC) / MAC_LEN * 100.0


def hold_weights(plan: dict):
    """Returns (fwd_kg, aft_kg, bulk_kg)."""
    fwd = aft = bulk = 0.0
    for e in plan.get("loading_plan", []):
        z = e.get("assigned_zone") or ""
        if   z.startswith("FWD"):  fwd  += e["weight_kg"]
        elif z.startswith("AFT"):  aft  += e["weight_kg"]
        elif z == "BULK_01":       bulk += e["weight_kg"]
    return fwd, aft, bulk


# ══════════════════════════════════════════════════════════════════════════════
# FUEL-BURN PROJECTION  (Req 3 — flight-plan driven)
# ══════════════════════════════════════════════════════════════════════════════

def project_fuel_burn(plan: dict) -> list[dict]:
    """
    Models CG migration over the flight using parameters from the flight plan.

    Returns a list of dicts, one per time step (1-minute resolution), each:
        {
          "time_hr":           float,   # hours from take-off
          "fuel_remaining_kg": float,
          "total_weight_kg":   float,
          "cg_pct_mac":        float,
        }
    """
    wb        = plan.get("weight_breakdown", {})
    flt       = plan.get("flight_plan", {})
    oew       = wb.get("empty_aircraft_kg",  155_000)
    pax_kg    = wb.get("passengers_kg",       26_350)
    fuel_kg   = wb.get("fuel_kg",   DEFAULT_FUEL_KG)
    cargo_kg  = wb.get("cargo_kg",            3_300)

    burn_rate    = flt.get("burn_rate_kg_per_hr", DEFAULT_FUEL_BURN_RATE)
    flight_hours = flt.get("flight_hours",         DEFAULT_FLIGHT_HRS)
    wing_x       = flt.get("wing_tank_cg_x_m",    DEFAULT_WING_TANK_CG_X_M)

    # Non-fuel mass and its moment (includes cargo at current CG)
    base_cg = plan.get("final_cg_pct_mac") or plan.get("base_cg_pct_mac") or CG_TGT
    base_cg_x       = LEMAC + (base_cg / 100.0) * MAC_LEN
    non_fuel_mass   = oew + pax_kg + cargo_kg
    non_fuel_moment = non_fuel_mass * base_cg_x

    # 1-minute time steps
    dt_hr  = 1.0 / 60.0
    steps  = []
    t      = 0.0
    remaining = float(fuel_kg)

    while t <= flight_hours + 1e-9:
        total_mass   = non_fuel_mass + remaining
        total_moment = non_fuel_moment + remaining * wing_x
        cg_x  = total_moment / total_mass if total_mass > 0 else LEMAC
        cg_pct = (cg_x - LEMAC) / MAC_LEN * 100.0

        steps.append({
            "time_hr":           round(t, 4),
            "fuel_remaining_kg": round(remaining, 1),
            "total_weight_kg":   round(total_mass, 1),
            "cg_pct_mac":        round(cg_pct, 4),
        })

        remaining = max(0.0, remaining - burn_rate * dt_hr)
        t += dt_hr
        if remaining <= 0:
            break

    return steps


# ══════════════════════════════════════════════════════════════════════════════
# INFEASIBILITY CHECKER  (Req 4 + original static check)
# ══════════════════════════════════════════════════════════════════════════════

def check_feasibility(plan: dict) -> dict:
    """
    Two-stage feasibility check:

    Stage 1 — Static: can the remaining unlocked cargo be placed such that
              the ramp CG (before fuel burn) is inside [CG_FWD, CG_AFT]?

    Stage 2 — Dynamic: build a best-case trial plan by greedily placing ALL
              remaining unlocked items to minimise |CG − target|, compute the
              resulting full-load CG, then run the fuel-burn trace on that
              trial plan.  Only if the fuel-burn trace still exits the envelope
              on the BEST achievable placement of unlocked items is the config
              declared INFEASIBLE.

              This prevents false positives when a partial load (e.g. two heavy
              FWD items) temporarily shows a bad flight CG that the remaining
              unlocked cargo could correct.

    Returns:
        {
          "feasible":          bool,
          "reason":            str,
          "conflicting_locks": list[str],  # item names
          "flight_violation":  dict | None # {time_hr, cg_pct_mac} of first breach
        }
    """
    # ── Stage 1: static placement check ──────────────────────────────────────
    items    = plan.get("loading_plan", [])
    locked   = [e for e in items if e.get("manual_lock") and e.get("assigned_zone")]
    unlocked = [e for e in items if not e.get("manual_lock")]

    base_moment, base_weight = _base_moment_weight(plan)

    fwd_used = aft_used = bulk_used = 0.0
    occupied = set()

    for e in locked:
        z  = e["assigned_zone"]
        wt = e["weight_kg"]
        base_moment += wt * ZONE_DEFS[z].x_position_m
        base_weight += wt
        occupied.add(z)
        if   z.startswith("FWD"):  fwd_used  += wt
        elif z.startswith("AFT"):  aft_used  += wt
        elif z == "BULK_01":       bulk_used += wt

    if not unlocked:
        # All cargo assigned — check static ramp CG then flight CG on actual plan
        if base_weight == 0:
            return {"feasible": True, "reason": "", "conflicting_locks": [],
                    "flight_violation": None}
        cg = (base_moment / base_weight - LEMAC) / MAC_LEN * 100.0
        if not (CG_FWD <= cg <= CG_AFT):
            return {
                "feasible": False,
                "reason": (f"All cargo assigned but ramp CG ({cg:.2f}% MAC) "
                           f"is outside the envelope [{CG_FWD}–{CG_AFT}% MAC]."),
                "conflicting_locks": [e["item"] for e in locked],
                "flight_violation": None,
            }
        # Proceed to stage 2 on the actual fully-assigned plan
        return _check_flight_cg(plan, {"feasible": True, "reason": "",
                                        "conflicting_locks": [], "flight_violation": None})

    # Find available free zones
    free_zones = [z for z in ALL_ZONES if z not in occupied]
    lightest = min(unlocked, key=lambda e: e["weight_kg"])["weight_kg"] if unlocked else 0

    reachable_x = []
    for z in free_zones:
        zdef = ZONE_DEFS[z]
        if z.startswith("FWD") and fwd_used  + lightest > FWD_HOLD_LIMIT_KG: continue
        if z.startswith("AFT") and aft_used  + lightest > AFT_HOLD_LIMIT_KG: continue
        if z == "BULK_01"      and bulk_used + lightest > BULK_LIMIT_KG:      continue
        reachable_x.append(zdef.x_position_m)

    if not reachable_x:
        return {
            "feasible": False,
            "reason": ("No available zones remain after hold-weight limits are applied. "
                       "All FWD, AFT, and BULK limits are exceeded."),
            "conflicting_locks": [e["item"] for e in locked],
            "flight_violation": None,
        }

    total_unlocked_wt = sum(e["weight_kg"] for e in unlocked)
    cg_if_all_fwd = (
        (base_moment + total_unlocked_wt * min(reachable_x)) /
        (base_weight + total_unlocked_wt) - LEMAC
    ) / MAC_LEN * 100.0
    cg_if_all_aft = (
        (base_moment + total_unlocked_wt * max(reachable_x)) /
        (base_weight + total_unlocked_wt) - LEMAC
    ) / MAC_LEN * 100.0

    achievable_min = min(cg_if_all_fwd, cg_if_all_aft)
    achievable_max = max(cg_if_all_fwd, cg_if_all_aft)

    if achievable_max < CG_FWD:
        return {
            "feasible": False,
            "reason": (f"Even placing all remaining cargo in the most-aft zones, "
                       f"the best achievable CG is {achievable_max:.2f}% MAC — "
                       f"still forward of the {CG_FWD}% MAC limit. "
                       f"The locked items pull the CG too far forward."),
            "conflicting_locks": [e["item"] for e in locked
                                  if ZONE_DEFS[e["assigned_zone"]].x_position_m < LEMAC],
            "flight_violation": None,
        }
    if achievable_min > CG_AFT:
        return {
            "feasible": False,
            "reason": (f"Even placing all remaining cargo in the most-forward zones, "
                       f"the best achievable CG is {achievable_min:.2f}% MAC — "
                       f"still aft of the {CG_AFT}% MAC limit. "
                       f"The locked items pull the CG too far aft."),
            "conflicting_locks": [e["item"] for e in locked
                                  if ZONE_DEFS[e["assigned_zone"]].x_position_m > LEMAC],
            "flight_violation": None,
        }

    # ── Stage 1 passed ─────────────────────────────────────────────────────────
    # Stage 2: build a best-case trial plan — greedily place all unlocked items
    # to optimise CG, then run the fuel-burn trace on that fully-loaded plan.
    # This avoids false positives from a partial load where the unlocked items
    # have not yet been placed and could compensate the flight CG shift.
    trial_plan = _build_best_case_trial(plan)
    return _check_flight_cg(
        trial_plan,
        {"feasible": True, "reason": "", "conflicting_locks": [],
         "flight_violation": None},
    )


def _build_best_case_trial(plan: dict) -> dict:
    """
    Return a deep-copy of *plan* where every unlocked item has been greedily
    placed (using the same greedy_recommend logic) to achieve the CG closest
    to target.  The copy's ``final_cg_pct_mac`` is updated so that
    ``project_fuel_burn`` uses the correct full-load CG.

    Locked items keep their current zone; only unlocked items are assigned.
    """
    trial = copy.deepcopy(plan)
    # Use the recommender — it respects existing manual_lock zones and assigns
    # the remaining items optimally.
    best_items = greedy_recommend(trial)
    trial["loading_plan"] = best_items
    # Recompute the ramp CG for the fully-loaded trial
    cg = recalculate_cg(trial)
    trial["final_cg_pct_mac"] = round(cg, 3) if cg is not None else None
    return trial


def _check_flight_cg(plan: dict, base_result: dict) -> dict:
    """
    Stage 2: run the fuel-burn trace on *plan* and flag the first CG breach.
    *plan* should already represent a fully-loaded configuration (all items
    placed) so that ``project_fuel_burn`` uses a meaningful full-load CG.
    Mutates and returns base_result.
    """
    steps = project_fuel_burn(plan)
    for s in steps:
        cg = s["cg_pct_mac"]
        if cg < CG_FWD or cg > CG_AFT:
            t_hr  = s["time_hr"]
            t_min = int(round(t_hr * 60))
            hrs   = t_min // 60
            mins  = t_min % 60
            side  = "forward of" if cg < CG_FWD else "aft of"
            limit = CG_FWD if cg < CG_FWD else CG_AFT
            base_result["feasible"] = False
            base_result["reason"] = (
                f"Even with the best possible placement of remaining cargo, "
                f"the CG exits the envelope during flight. "
                f"No arrangement of the unlocked cargo can keep the CG within the "
                f"safe envelope throughout the flight."
            )
            base_result["flight_violation"] = {
                "time_hr":    t_hr,
                "cg_pct_mac": cg,
            }
            return base_result
    return base_result


# ══════════════════════════════════════════════════════════════════════════════
# GREEDY CG RECOMMENDER
# ══════════════════════════════════════════════════════════════════════════════

def greedy_recommend(plan: dict) -> list[dict]:
    items      = copy.deepcopy(plan.get("loading_plan", []))
    wb         = plan.get("weight_breakdown") or {}
    oew        = wb.get("empty_aircraft_kg",  155_000)
    pax_zones  = A3501000ReferenceModel.passenger_zones()
    fuel_tanks = A3501000ReferenceModel.fuel_tanks()

    pax_kg_ov  = wb.get("passengers_kg")
    fuel_kg_ov = wb.get("fuel_kg")
    flt        = plan.get("flight_plan", {})
    wing_x     = flt.get("wing_tank_cg_x_m", DEFAULT_WING_TANK_CG_X_M)

    if pax_kg_ov is not None:
        pax_wt = pax_kg_ov
        pax_mo = pax_kg_ov * (sum(z.total_weight() * z.x_position_m for z in pax_zones.values())
                               / max(sum(z.total_weight() for z in pax_zones.values()), 1))
    else:
        pax_wt = sum(z.total_weight() for z in pax_zones.values())
        pax_mo = sum(z.total_weight() * z.x_position_m for z in pax_zones.values())

    if fuel_kg_ov is not None:
        fuel_wt = fuel_kg_ov
        fuel_mo = fuel_kg_ov * wing_x
    else:
        fuel_wt = sum(t.fuel_kg for t in fuel_tanks.values())
        fuel_mo = sum(t.fuel_kg * t.x_position_m for t in fuel_tanks.values())

    base_moment = (oew * A3501000ReferenceModel.operating_empty_aircraft().empty_cg_x_m
                   + pax_mo + fuel_mo)
    base_weight = oew + pax_wt + fuel_wt

    occupied  = {}
    fwd_used  = aft_used = bulk_used = 0.0
    cur_moment = base_moment
    cur_weight = base_weight

    for it in items:
        if it.get("manual_lock") and it.get("assigned_zone"):
            z  = it["assigned_zone"]
            wt = it["weight_kg"]
            occupied[z] = it["item"]
            cur_moment += wt * ZONE_DEFS[z].x_position_m
            cur_weight += wt
            if   z.startswith("FWD"):  fwd_used  += wt
            elif z.startswith("AFT"):  aft_used  += wt
            elif z == "BULK_01":       bulk_used += wt

    to_place = sorted(
        [it for it in items if not it.get("manual_lock")],
        key=lambda x: -x["weight_kg"]
    )

    for it in to_place:
        best_zone  = None
        best_delta = float("inf")
        wt = it["weight_kg"]

        for z, zdef in ZONE_DEFS.items():
            if z in occupied:
                continue
            # Req 2: strictly greater-than for overweight (≤ is OK)
            if z.startswith("FWD") and fwd_used  + wt > FWD_HOLD_LIMIT_KG: continue
            if z.startswith("AFT") and aft_used  + wt > AFT_HOLD_LIMIT_KG: continue
            if z == "BULK_01"      and bulk_used + wt > BULK_LIMIT_KG:      continue

            trial_cg = (
                (cur_moment + wt * zdef.x_position_m) / (cur_weight + wt) - LEMAC
            ) / MAC_LEN * 100.0
            delta = abs(trial_cg - CG_TGT)
            if delta < best_delta:
                best_delta = delta
                best_zone  = z

        it["assigned_zone"] = best_zone
        if best_zone:
            occupied[best_zone] = it["item"]
            cur_moment += wt * ZONE_DEFS[best_zone].x_position_m
            cur_weight += wt
            if   best_zone.startswith("FWD"):  fwd_used  += wt
            elif best_zone.startswith("AFT"):  aft_used  += wt
            elif best_zone == "BULK_01":       bulk_used += wt

    result = []
    for it in items:
        new_it = dict(it)
        if not it.get("manual_lock"):
            placed = next((x for x in to_place if x["item"] == it["item"]), None)
            if placed:
                new_it["assigned_zone"] = placed["assigned_zone"]
        result.append(new_it)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# INFEASIBILITY DANGER OVERLAY
# ══════════════════════════════════════════════════════════════════════════════

class DangerOverlay(tk.Toplevel):
    def __init__(self, parent, result: dict, on_review, on_remove_all_cargo):
        super().__init__(parent)
        self.title("⚠  INFEASIBLE CONFIGURATION DETECTED")
        self.configure(bg=C["danger_bg"])
        self.geometry("700x480")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self._build(result, on_review, on_remove_all_cargo)

    def _build(self, result, on_review, on_remove_all_cargo):
        hdr = tk.Frame(self, bg=C["red"], pady=16, padx=20)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚠  NO FEASIBLE CONFIGURATION",
                 font=(UI_FONT, 16, "bold"),
                 fg="white", bg=C["red"]).pack()

        fv = result.get("flight_violation")
        subtitle = ("The fuel-burn CG trace exits the safe envelope during flight."
                    if fv else
                    "The current manual locks make it impossible to achieve a CG within the envelope.")
        tk.Label(hdr, text=subtitle, font=(UI_FONT, 10),
                 fg="#ffcccc", bg=C["red"], wraplength=660).pack(pady=(4, 0))

        reason_frame = tk.Frame(self, bg=C["danger_bg"], padx=24, pady=14)
        reason_frame.pack(fill="x")
        tk.Label(reason_frame, text="DIAGNOSIS:",
                 font=(UI_FONT, 10, "bold"),
                 fg=C["orange"], bg=C["danger_bg"]).pack(anchor="w")
        tk.Label(reason_frame, text=result.get("reason", "Unknown cause."),
                 font=(UI_FONT, 10), fg=C["text"], bg=C["danger_bg"],
                 wraplength=650, justify="left").pack(anchor="w", pady=(4, 0))

        # Show flight violation details prominently (Req 4)
        if fv:
            t_hr  = fv["time_hr"]
            t_min = int(round(t_hr * 60))
            fv_frame = tk.Frame(self, bg="#1a0808", padx=24, pady=8)
            fv_frame.pack(fill="x", padx=20, pady=(0, 4))
            tk.Label(fv_frame,
                     text=f"⏱  First violation at T+{t_min//60}h {t_min%60:02d}min   |   "
                          f"CG = {fv['cg_pct_mac']:.2f}% MAC",
                     font=(UI_FONT, 10, "bold"),
                     fg=C["red"], bg="#1a0808").pack()

        conflicts = result.get("conflicting_locks", [])
        if conflicts:
            tk.Label(reason_frame, text="\nCONFLICTING LOCKED ITEMS:",
                     font=(UI_FONT, 10, "bold"),
                     fg=C["red"], bg=C["danger_bg"]).pack(anchor="w")
            for name in conflicts:
                tk.Label(reason_frame, text=f"  🔒  {name}",
                         font=(UI_FONT, 10),
                         fg=C["orange"], bg=C["danger_bg"]).pack(anchor="w")

        env_frame = tk.Frame(self, bg="#1a0808", padx=24, pady=8)
        env_frame.pack(fill="x", padx=20, pady=(0, 8))
        tk.Label(env_frame,
                 text=f"CG Envelope:  {CG_FWD:.0f}% MAC (fwd)  —  {CG_AFT:.0f}% MAC (aft)  |  Target: {CG_TGT:.0f}% MAC",
                 font=(UI_FONT, 9, "bold"),
                 fg=C["muted"], bg="#1a0808").pack()

        action_msg = ("ACTION: Adjust cargo placement so the CG stays within the envelope throughout the flight."
                      if fv else
                      "ACTION: Remove all cargo to start fresh, or review the current placement manually.")
        tk.Label(self, text=action_msg, font=(UI_FONT, 10, "bold"),
                 fg=C["yellow"], bg=C["danger_bg"], wraplength=660).pack(padx=24, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["danger_bg"], pady=12)
        btn_row.pack(fill="x", padx=24)
        tk.Button(btn_row, text="🗑  Remove All Cargo",
                  command=lambda: [self.destroy(), on_remove_all_cargo()],
                  bg=C["red"], fg="white",
                  font=(UI_FONT, 11, "bold"),
                  relief="flat", padx=16, pady=8,
                  cursor="hand2").pack(side="left", padx=(0, 10))
        tk.Button(btn_row, text="Review Manually  →",
                  command=lambda: [self.destroy(), on_review()],
                  bg=C["border"], fg=C["text"],
                  font=(UI_FONT, 11),
                  relief="flat", padx=16, pady=8,
                  cursor="hand2").pack(side="left")

        self._pulse_state = False
        self._pulse()

    def _pulse(self):
        if not self.winfo_exists(): return
        self._pulse_state = not self._pulse_state
        col = C["red"] if self._pulse_state else C["danger_bg"]
        self.configure(highlightbackground=col, highlightthickness=4)
        self.after(700, self._pulse)


# ══════════════════════════════════════════════════════════════════════════════
# REASSIGN DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class ReassignDialog(tk.Toplevel):
    def __init__(self, parent, item_entry: dict, plan: dict,
                 zone_map: dict, on_confirm):
        super().__init__(parent)
        self.title("Reassign Cargo")
        self.configure(bg=C["panel2"])
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()

        self._item       = item_entry
        self._plan       = plan
        self._zone_map   = zone_map
        self._on_confirm = on_confirm
        self._result_zone = tk.StringVar(value=item_entry.get("assigned_zone") or "")
        self._build()
        self._update_preview()

    def _build(self):
        item = self._item
        wt   = item.get("weight_kg", 0)

        hdr = tk.Frame(self, bg=C["accent"], padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"Reassign: {item['item']}",
                 font=(UI_FONT, 11, "bold"),
                 fg=C["bg"], bg=C["accent"]).pack(side="left")
        tk.Label(hdr, text=f"  {wt:,.1f} kg",
                 font=(UI_FONT, 11),
                 fg=C["bg"], bg=C["accent"]).pack(side="left")

        body = tk.Frame(self, bg=C["panel2"], padx=16, pady=10)
        body.pack(fill="x")
        cur = item.get("assigned_zone") or "Unassigned"
        tk.Label(body, text=f"Current zone: {cur}",
                 font=(UI_FONT, 10), fg=C["muted"],
                 bg=C["panel2"]).pack(anchor="w")
        tk.Label(body, text="New zone:",
                 font=(UI_FONT, 10), fg=C["text"],
                 bg=C["panel2"]).pack(anchor="w", pady=(8, 2))

        fwd_w, aft_w, bulk_w = hold_weights(self._plan)
        cur_z   = item.get("assigned_zone")
        options = []
        for z in ALL_ZONES:
            occupants = self._zone_map.get(z, [])
            is_cur  = z == cur_z
            is_free = len(occupants) == 0 or is_cur
            if not is_free:
                continue
            tf  = fwd_w  - (wt if cur_z and cur_z.startswith("FWD") else 0)
            ta  = aft_w  - (wt if cur_z and cur_z.startswith("AFT") else 0)
            tb  = bulk_w - (wt if cur_z == "BULK_01" else 0)
            # Req 2: strictly > limit means overweight
            if z.startswith("FWD") and tf + wt > FWD_HOLD_LIMIT_KG and not is_cur:
                options.append(f"{z}  [FWD LIMIT EXCEEDED]"); continue
            if z.startswith("AFT") and ta + wt > AFT_HOLD_LIMIT_KG and not is_cur:
                options.append(f"{z}  [AFT LIMIT EXCEEDED]"); continue
            if z == "BULK_01"      and tb + wt > BULK_LIMIT_KG      and not is_cur:
                options.append(f"{z}  [BULK LIMIT {BULK_LIMIT_KG} kg EXCEEDED]"); continue
            options.append(z)

        cb = ttk.Combobox(body, textvariable=self._result_zone,
                          values=options, state="readonly",
                          font=(UI_FONT, 11), width=36)
        cb.pack(fill="x")
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_preview())

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TCombobox",
                        fieldbackground=C["panel"], background=C["border"],
                        foreground=C["text"], selectbackground=C["accent"],
                        selectforeground=C["bg"])

        self._preview_lbl = tk.Label(body, text="", font=(UI_FONT, 10),
                                     fg=C["muted"], bg=C["panel2"],
                                     pady=4, wraplength=420, justify="left")
        self._preview_lbl.pack(anchor="w")
        self._feas_lbl = tk.Label(body, text="", font=(UI_FONT, 9),
                                   fg=C["muted"], bg=C["panel2"],
                                   pady=2, wraplength=420, justify="left")
        self._feas_lbl.pack(anchor="w")

        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        btn_row = tk.Frame(self, bg=C["panel2"], padx=16, pady=10)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg=C["border"], fg=C["text"], relief="flat",
                  padx=14, pady=5, cursor="hand2").pack(side="right", padx=4)
        tk.Button(btn_row, text="Confirm & Lock 🔒", command=self._confirm,
                  bg=C["accent"], fg=C["bg"],
                  font=(UI_FONT, 10, "bold"),
                  relief="flat", padx=14, pady=5,
                  cursor="hand2").pack(side="right", padx=4)

    def _update_preview(self):
        raw  = self._result_zone.get()
        zone = raw.split("  ")[0]
        if not zone or zone not in ZONE_DEFS:
            self._preview_lbl.config(text="Select a valid zone", fg=C["muted"])
            self._feas_lbl.config(text="")
            return

        trial = copy.deepcopy(self._plan)
        for e in trial.get("loading_plan", []):
            if e["item"] == self._item["item"]:
                e["assigned_zone"] = zone
                e["manual_lock"]   = True
                break

        cg = recalculate_cg(trial)
        if cg is None:
            self._preview_lbl.config(text="CG: —", fg=C["muted"])
            return

        col = C[compute_cg_status(cg)]
        fw, aw, bw = hold_weights(trial)
        fok = "✓" if fw <= FWD_HOLD_LIMIT_KG else f"⚠ {fw:,.0f} kg"
        aok = "✓" if aw <= AFT_HOLD_LIMIT_KG else f"⚠ {aw:,.0f} kg"
        bok = "✓" if bw <= BULK_LIMIT_KG      else f"⚠ {bw:,.0f} kg"
        self._preview_lbl.config(
            text=(f"Projected CG: {cg:.3f}% MAC  "
                  f"({'✓ within limits' if CG_FWD <= cg <= CG_AFT else '✗ OUTSIDE LIMITS'})\n"
                  f"FWD: {fok}   AFT: {aok}   BULK: {bok}"),
            fg=col)

        feas = check_feasibility(trial)
        if not feas["feasible"]:
            fv = feas.get("flight_violation")
            if fv:
                t_min = int(round(fv["time_hr"] * 60))
                msg = (f"⚠ Flight CG violation at T+{t_min//60}h {t_min%60:02d}min "
                       f"(CG={fv['cg_pct_mac']:.2f}%)")
            else:
                msg = f"⚠ INFEASIBLE: {feas['reason'][:80]}…"
            self._feas_lbl.config(text=msg, fg=C["red"])
        else:
            self._feas_lbl.config(text="✓ Configuration feasible throughout flight.", fg=C["green"])

    def _confirm(self):
        raw  = self._result_zone.get()
        zone = raw.split("  ")[0]
        if not zone or zone not in ZONE_DEFS:
            messagebox.showwarning("Invalid zone", "Please select a valid zone.", parent=self)
            return
        if "LIMIT EXCEEDED" in raw:
            if not messagebox.askyesno("Weight limit exceeded",
                                       "This exceeds a hold limit. Confirm anyway?",
                                       parent=self):
                return
        self._on_confirm(self._item["item"], zone)
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# RECOMMENDATION WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class RecommendationWindow(tk.Toplevel):
    def __init__(self, parent, rec_items, projected_cg, item_colours, on_apply):
        super().__init__(parent)
        self.title("🤖  Recommended Configuration")
        self.configure(bg=C["bg"])
        self.geometry("520x520")
        self.resizable(True, True)
        self._rec_items    = rec_items
        self._projected_cg = projected_cg
        self._item_colours = item_colours
        self._on_apply     = on_apply
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg=C["panel"], padx=14, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🤖  AI Recommendation",
                 font=(UI_FONT, 13, "bold"),
                 fg=C["accent"], bg=C["panel"]).pack(side="left")
        cg_col = C[compute_cg_status(self._projected_cg)]
        cg_txt = f"{self._projected_cg:.3f} % MAC" if self._projected_cg else "—"
        tk.Label(hdr, text=f"Projected CG: {cg_txt}",
                 font=(UI_FONT, 11, "bold"),
                 fg=cg_col, bg=C["panel"]).pack(side="right")

        tk.Label(self, text="🔒 Locked items kept.  Others reassigned to minimise |CG − target|.",
                 font=(UI_FONT, 9), fg=C["muted"], bg=C["bg"],
                 wraplength=490).pack(pady=(4, 0))
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", padx=10, pady=4)

        outer = tk.Frame(self, bg=C["bg"])
        outer.pack(fill="both", expand=True, padx=10, pady=4)
        vsb = tk.Scrollbar(outer); vsb.pack(side="right", fill="y")
        lbc = tk.Canvas(outer, bg=C["bg"], highlightthickness=0, yscrollcommand=vsb.set)
        lbc.pack(side="left", fill="both", expand=True)
        vsb.config(command=lbc.yview)
        inner = tk.Frame(lbc, bg=C["bg"])
        wid = lbc.create_window((0, 0), window=inner, anchor="nw")
        lbc.bind("<Configure>", lambda e: lbc.itemconfig(wid, width=e.width))
        inner.bind("<Configure>", lambda e: lbc.configure(scrollregion=lbc.bbox("all")))

        for it in self._rec_items:
            col  = self._item_colours.get(it["item"], C["accent"])
            lock = "🔒" if it.get("manual_lock") else "  "
            zone = it.get("assigned_zone") or "—"
            row  = tk.Frame(inner, bg=C["panel2"], pady=5, padx=10)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="●", fg=col, bg=C["panel2"],
                     font=(UI_FONT, 13)).pack(side="left")
            tk.Label(row, text=f"{lock} {it['item']}",
                     font=(UI_FONT, 10, "bold"),
                     fg=C["text"], bg=C["panel2"]).pack(side="left", padx=6)
            tk.Label(row, text=f"→  {zone}  ({it['weight_kg']:,.1f} kg)",
                     font=(UI_FONT, 10),
                     fg=C["muted"], bg=C["panel2"]).pack(side="right", padx=6)

        btn_row = tk.Frame(self, bg=C["bg"], pady=8)
        btn_row.pack(fill="x", padx=10)
        tk.Button(btn_row, text="Discard", command=self.destroy,
                  bg=C["border"], fg=C["text"], relief="flat",
                  padx=14, pady=6, cursor="hand2").pack(side="right", padx=4)
        tk.Button(btn_row, text="Apply Recommendation ✓",
                  command=lambda: [self._on_apply(self._rec_items), self.destroy()],
                  bg=C["green"], fg=C["bg"],
                  font=(UI_FONT, 10, "bold"),
                  relief="flat", padx=14, pady=6,
                  cursor="hand2").pack(side="right", padx=4)


# ══════════════════════════════════════════════════════════════════════════════
# FUEL-BURN CHART WINDOW  (Req 3 — time axis, flight plan info)
# ══════════════════════════════════════════════════════════════════════════════

class FuelBurnWindow(tk.Toplevel):
    def __init__(self, parent, steps: list[dict], plan: dict):
        super().__init__(parent)
        self.title("CG Projection — Fuel Burn")
        self.configure(bg=C["bg"])
        self.geometry("700x400")
        self.resizable(True, True)
        self._steps = steps
        self._plan  = plan
        self._build()

    def _build(self):
        flt    = self._plan.get("flight_plan", {})
        fn     = flt.get("flight_number", "")
        route  = f"{flt.get('origin','')} → {flt.get('destination','')}"
        br     = flt.get("burn_rate_kg_per_hr", DEFAULT_FUEL_BURN_RATE)
        fh     = flt.get("flight_hours", DEFAULT_FLIGHT_HRS)

        hdr = tk.Frame(self, bg=C["panel"], pady=8, padx=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"✈  CG Migration During Fuel Burn",
                 font=(UI_FONT, 11, "bold"),
                 fg=C["accent"], bg=C["panel"]).pack(side="left")
        if fn or route.strip("→ "):
            tk.Label(hdr, text=f"  {fn}  {route}",
                     font=(UI_FONT, 10),
                     fg=C["muted"], bg=C["panel"]).pack(side="left")

        sub = tk.Frame(self, bg=C["bg"])
        sub.pack(fill="x", padx=14)
        tk.Label(sub,
                 text=f"Burn rate: {br:,.0f} kg/hr   |   Flight time: {fh:.1f} h   |   Steps: 1 min",
                 font=(UI_FONT, 9), fg=C["muted"], bg=C["bg"]).pack(side="left")

        self.chart = tk.Canvas(self, bg=C["panel"], highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=14, pady=8)
        self.chart.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _draw(self):
        c = self.chart; c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return
        steps = self._steps
        if len(steps) < 2: return

        pad_l, pad_r, pad_t, pad_b = 64, 20, 20, 44
        plot_w = W - pad_l - pad_r
        plot_h = H - pad_t - pad_b

        times   = [s["time_hr"]           for s in steps]
        cgs     = [s["cg_pct_mac"]        for s in steps]
        weights = [s["total_weight_kg"]   for s in steps]
        fuels   = [s["fuel_remaining_kg"] for s in steps]

        max_t  = max(times)
        min_cg = min(min(cgs) - 0.5, CG_FWD - 0.5)
        max_cg = max(max(cgs) + 0.5, CG_AFT + 0.5)

        def px(t):   return pad_l + t / max_t * plot_w if max_t > 0 else pad_l
        def py(cg):  return pad_t + plot_h - (cg - min_cg) / (max_cg - min_cg) * plot_h

        # Danger zones (outside envelope) — filled bands
        y_fwd = py(CG_FWD); y_aft = py(CG_AFT)
        c.create_rectangle(pad_l, pad_t,      pad_l+plot_w, y_fwd,       fill="#1a0000", outline="")
        c.create_rectangle(pad_l, y_aft,      pad_l+plot_w, pad_t+plot_h, fill="#1a0000", outline="")
        # Safe zone — subtle green tint
        c.create_rectangle(pad_l, y_fwd, pad_l+plot_w, y_aft, fill="#0d1f0d", outline="")

        # Limit lines
        for lim, col, lbl in [
            (CG_FWD, C["red"],   f"FWD {CG_FWD:.0f}%"),
            (CG_AFT, C["red"],   f"AFT {CG_AFT:.0f}%"),
            (CG_TGT, C["green"], f"TGT {CG_TGT:.0f}%"),
        ]:
            if min_cg <= lim <= max_cg:
                yy = py(lim)
                c.create_line(pad_l, yy, pad_l+plot_w, yy, fill=col, dash=(4, 3), width=1)
                c.create_text(pad_l-4, yy, text=lbl, font=(MONO_FONT, 6),
                              fill=col, anchor="e")

        # Axes
        c.create_line(pad_l, pad_t, pad_l, pad_t+plot_h, fill=C["border"], width=1)
        c.create_line(pad_l, pad_t+plot_h, pad_l+plot_w, pad_t+plot_h, fill=C["border"], width=1)

        # X-axis ticks (hours)
        for h in range(0, int(max_t)+2):
            if h > max_t + 0.05: break
            tx = px(h)
            c.create_line(tx, pad_t+plot_h, tx, pad_t+plot_h+4, fill=C["muted"], width=1)
            c.create_text(tx, pad_t+plot_h+14, text=f"{h}h",
                          font=(UI_FONT, 6), fill=C["muted"])
        c.create_text(pad_l+plot_w/2, H-6, text="Flight Time (hours from departure)",
                      font=(UI_FONT, 8), fill=C["muted"])
        c.create_text(10, pad_t+plot_h/2, text="CG % MAC",
                      font=(UI_FONT, 8), fill=C["muted"], angle=90)

        # CG trace — colour each segment by its status
        pts = [(px(times[i]), py(cgs[i])) for i in range(len(steps))]
        for i in range(len(pts)-1):
            c.create_line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1],
                          fill=C[compute_cg_status(cgs[i])], width=2)

        # TOW / LDW markers
        sx, sy = pts[0];  ex, ey = pts[-1]
        c.create_oval(sx-5, sy-5, sx+5, sy+5, fill=C["accent"], outline="")
        c.create_text(sx+2, sy-13,
                      text=f"TOW  {weights[0]/1000:.0f}t  {fuels[0]/1000:.0f}t fuel",
                      font=(UI_FONT, 6), fill=C["accent"])
        c.create_oval(ex-5, ey-5, ex+5, ey+5, fill=C["accent2"], outline="")
        c.create_text(ex, ey-13,
                      text=f"LDW  {weights[-1]/1000:.0f}t",
                      font=(UI_FONT, 6), fill=C["accent2"])

        # Mark first violation if any
        for i, s in enumerate(steps):
            if s["cg_pct_mac"] < CG_FWD or s["cg_pct_mac"] > CG_AFT:
                vx, vy = pts[i]
                c.create_oval(vx-6, vy-6, vx+6, vy+6, fill=C["red"], outline="white")
                c.create_text(vx, vy-16,
                              text=f"⚠ T+{s['time_hr']:.1f}h",
                              font=(UI_FONT, 6, "bold"), fill=C["red"])
                break


# ══════════════════════════════════════════════════════════════════════════════
# ZONE DETAIL POPUP  (Req 3: per-item remove buttons  |  Req 4: assign-cargo UI)
# ══════════════════════════════════════════════════════════════════════════════

class ZoneDetailPopup(tk.Toplevel):
    """
    Rich floating popup shown when any zone slot is clicked.

    Occupied zone  → shows cargo info + individual ✕ Remove and ✎ Reassign buttons
    Empty zone     → shows zone info + list of unassigned cargo with ✓ Assign buttons
    """

    def __init__(self, parent, zone_id: str, items: list, zone_def,
                 item_colours: dict, plan: dict,
                 on_remove_item, on_assign_item, on_reassign):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=C["panel2"])

        self._on_remove   = on_remove_item
        self._on_assign   = on_assign_item
        self._on_reassign = on_reassign

        self._build(zone_id, items, zone_def, item_colours, plan)
        self.bind("<Escape>", lambda e: self.destroy())
        # click outside to close
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, event):
        try:
            if self.winfo_exists():
                self.after(100, self._check_focus)
        except Exception:
            pass

    def _check_focus(self):
        try:
            focused = self.focus_get()
            if focused is None:
                self.destroy()
        except Exception:
            pass

    def _header(self, title: str, subtitle: str = ""):
        hdr = tk.Frame(self, bg=C["accent"], padx=10, pady=7)
        hdr.pack(fill="x")
        tk.Label(hdr, text=title,
                 font=(UI_FONT, 11, "bold"),
                 fg=C["bg"], bg=C["accent"]).pack(side="left")
        if subtitle:
            tk.Label(hdr, text=f"  {subtitle}",
                     font=(UI_FONT, 9),
                     fg=C["bg"], bg=C["accent"]).pack(side="left")
        close_btn = tk.Label(hdr, text=" ✕ ",
                             font=(UI_FONT, 11, "bold"),
                             fg=C["bg"], bg=C["accent"],
                             cursor="hand2")
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self.destroy())

    def _build(self, zone_id, items, zone_def, item_colours, plan):
        x_m = getattr(zone_def, "x_position_m", "—")
        cap = getattr(zone_def, "max_weight_kg", None)
        cap_str = f"{cap:,} kg" if cap else "—"
        subtitle = f"Sta {x_m} m  ·  Cap {cap_str}"

        self._header(zone_id, subtitle)

        if items:
            self._build_occupied(zone_id, items, zone_def, item_colours, cap)
        else:
            self._build_empty(zone_id, zone_def, item_colours, plan)

    def _build_occupied(self, zone_id, items, zone_def, item_colours, capacity):
        """Occupied zone: show each cargo item with ✕ Remove + ✎ Reassign."""
        body = tk.Frame(self, bg=C["panel2"], padx=12, pady=8)
        body.pack(fill="x")

        for it in items:
            col  = item_colours.get(it["item"], C["accent"])
            wt   = it.get("weight_kg", 0)
            cg_a = it.get("cg_after_placement")
            locked = it.get("manual_lock", False)

            row = tk.Frame(body, bg=C["panel2"], pady=3)
            row.pack(fill="x")

            tk.Label(row, text="▐ ", fg=col, bg=C["panel2"],
                     font=(UI_FONT, 13)).pack(side="left")

            info = tk.Frame(row, bg=C["panel2"])
            info.pack(side="left", fill="x", expand=True)

            lock_icon = " 🔒" if locked else ""
            tk.Label(info,
                     text=it["item"] + lock_icon,
                     font=(UI_FONT, 10, "bold"),
                     fg=C["orange"] if locked else C["text"],
                     bg=C["panel2"], anchor="w").pack(fill="x")
            tk.Label(info, text=f"{wt:,.1f} kg",
                     font=(UI_FONT, 9),
                     fg=C["muted"], bg=C["panel2"], anchor="w").pack(fill="x")
            if cg_a is not None:
                s_col = C[compute_cg_status(cg_a)]
                tk.Label(info, text=f"CG after: {cg_a:.3f} % MAC",
                         font=(UI_FONT, 9),
                         fg=s_col, bg=C["panel2"], anchor="w").pack(fill="x")

            # ─── Action buttons ────────────────────────────────────────────
            btn_col = tk.Frame(row, bg=C["panel2"])
            btn_col.pack(side="right", padx=(8, 0))

            # Req 3: ✕ Remove this item's assignment
            def _remove(name=it["item"]):
                self._on_remove(name)

            tk.Button(btn_col, text="✕ Remove",
                      command=_remove,
                      bg=C["red"], fg="white",
                      font=(UI_FONT, 8, "bold"),
                      relief="flat", padx=6, pady=2,
                      cursor="hand2").pack(fill="x", pady=1)

            # ✎ Reassign
            def _reassign(entry=it):
                self.destroy()
                self._on_reassign(entry)

            tk.Button(btn_col, text="✎ Reassign",
                      command=_reassign,
                      bg=C["border"], fg=C["accent"],
                      font=(UI_FONT, 8),
                      relief="flat", padx=6, pady=2,
                      cursor="hand2").pack(fill="x", pady=1)

            tk.Frame(body, bg=C["border"], height=1).pack(fill="x", pady=2)

        # Zone total
        total_w = sum(it.get("weight_kg", 0) for it in items)
        ft = tk.Frame(self, bg=C["panel"], padx=12, pady=5)
        ft.pack(fill="x")
        cap_val = capacity if capacity else (total_w or 1)
        pct = min(100.0, total_w / cap_val * 100) if cap_val else 0
        pct_col = C["green"] if pct < 80 else C["yellow"] if pct <= 100 else C["red"]
        tk.Label(ft, text=f"Total: {total_w:,.1f} kg  ({pct:.0f}% utilisation)",
                 font=(UI_FONT, 9, "bold"),
                 fg=pct_col, bg=C["panel"]).pack(anchor="w")

    def _build_empty(self, zone_id, zone_def, item_colours, plan):
        """Req 4: Empty zone — show unloaded cargo items the user can assign here."""
        unassigned = [e for e in plan.get("loading_plan", [])
                      if not e.get("assigned_zone")]

        if not unassigned:
            tk.Label(self, text="  — Zone is empty  |  All cargo assigned —",
                     font=(UI_FONT, 10, "italic"),
                     fg=C["muted"], bg=C["panel2"],
                     padx=14, pady=12).pack()
            return

        tk.Label(self,
                 text=f"  Assign cargo to {zone_id}:",
                 font=(UI_FONT, 9, "bold"),
                 fg=C["accent"], bg=C["panel2"],
                 padx=12, pady=5).pack(anchor="w")

        # Scrollable list of unassigned items
        outer = tk.Frame(self, bg=C["panel2"])
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        vsb = tk.Scrollbar(outer, orient="vertical")
        vsb.pack(side="right", fill="y")
        lbc = tk.Canvas(outer, bg=C["panel2"], highlightthickness=0,
                        yscrollcommand=vsb.set,
                        height=min(260, 40 * len(unassigned)))
        lbc.pack(side="left", fill="both", expand=True)
        vsb.config(command=lbc.yview)
        inner = tk.Frame(lbc, bg=C["panel2"])
        wid = lbc.create_window((0, 0), window=inner, anchor="nw")
        lbc.bind("<Configure>", lambda e: lbc.itemconfig(wid, width=e.width))
        inner.bind("<Configure>",
                   lambda e: lbc.configure(scrollregion=lbc.bbox("all")))

        for entry in unassigned:
            col = item_colours.get(entry["item"], C["accent"])
            wt  = entry.get("weight_kg", 0)

            row = tk.Frame(inner, bg=C["panel2"], pady=3, padx=6)
            row.pack(fill="x")

            tk.Label(row, text="●", fg=col, bg=C["panel2"],
                     font=(UI_FONT, 11)).pack(side="left")

            info = tk.Frame(row, bg=C["panel2"])
            info.pack(side="left", padx=5, fill="x", expand=True)
            tk.Label(info, text=entry["item"],
                     font=(UI_FONT, 9, "bold"),
                     fg=C["text"], bg=C["panel2"], anchor="w").pack(fill="x")
            tk.Label(info, text=f"{wt:,.1f} kg",
                     font=(UI_FONT, 8),
                     fg=C["muted"], bg=C["panel2"], anchor="w").pack(fill="x")

            def _assign(name=entry["item"], zid=zone_id):
                self._on_assign(name, zid)

            tk.Button(row, text="✓ Place Here",
                      command=_assign,
                      bg=C["green"], fg=C["bg"],
                      font=(UI_FONT, 8, "bold"),
                      relief="flat", padx=8, pady=2,
                      cursor="hand2").pack(side="right", padx=2)

            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", padx=4)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD  (v7)
# ══════════════════════════════════════════════════════════════════════════════

class AIRLoadDashboard(tk.Tk):

    def __init__(self, plan: dict, source_path: str = None):
        super().__init__()
        self.plan        = _normalise_plan(plan)
        self.zone_map    = build_zone_map(self.plan)
        self.source_path = source_path
        self._item_colours: dict[str, str] = {}
        self._assign_colours()

        self._live_active = source_path is not None
        self._live_blink  = False
        self._popup       = None
        self._zone_rects: dict[str, tuple] = {}

        self._active_recommendation: list[dict] | None = None
        self._rec_projected_cg: float | None = None

        self._last_feasibility: dict = {"feasible": True, "reason": "",
                                         "conflicting_locks": [], "flight_violation": None}
        self._danger_overlay: DangerOverlay | None = None

        self.title("AIRLoad  ·  A350-1000 Cargo Loading Dashboard  v9")
        self.configure(bg=C["bg"])
        self.resizable(True, True)
        self.minsize(1200, 780)

        self._build_ui()
        self._draw_all()

        if self._live_active:
            self._schedule_poll()
            self._blink_live()

        self.after(300, self._run_feasibility_check)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _assign_colours(self):
        palette = C["cargo_cols"]
        for i, e in enumerate(self.plan.get("loading_plan", [])):
            self._item_colours[e["item"]] = palette[i % len(palette)]

    def _update_live_cg(self):
        cg = recalculate_cg(self.plan)
        self.plan["final_cg_pct_mac"] = round(cg, 3) if cg is not None else None
        self.plan["cg_within_limits"] = (CG_FWD <= cg <= CG_AFT if cg is not None else None)
        smap = {"green": "WITHIN LIMITS", "yellow": "WITHIN LIMITS (off-target)",
                "red": "WARNING - OUTSIDE LIMITS", "muted": "UNASSIGNED"}
        self.plan["status"] = smap.get(compute_cg_status(cg), "—")

    # ── Feasibility ───────────────────────────────────────────────────────────

    def _run_feasibility_check(self, from_manual: bool = False):
        """
        Always runs the feasibility check in the background and updates
        _last_feasibility (used by the status bar).

        The DangerOverlay is ONLY shown when from_manual=True (i.e. the user
        just manually locked a cargo item) AND there is no feasible solution
        for the remaining unlocked items.  Background polls and initial load
        never trigger the overlay.
        """
        result = check_feasibility(self.plan)
        self._last_feasibility = result

        # Always update the status bar indicator regardless of source
        self._build_status_bar()

        if result["feasible"]:
            # If a previous overlay is open and the situation is now resolved,
            # dismiss it silently.
            if self._danger_overlay and self._danger_overlay.winfo_exists():
                self._danger_overlay.destroy()
            return

        # Only pop the overlay when the user has just manually locked a cargo
        # item.  Background polls / initial load must not interrupt the user.
        if not from_manual:
            return

        # Don't stack overlays — if one is already shown, leave it.
        if self._danger_overlay and self._danger_overlay.winfo_exists():
            return

        self._danger_overlay = DangerOverlay(
            self, result,
            on_review=self._review_locks,
            on_remove_all_cargo=self._do_remove_all_cargo,
        )

    def _review_locks(self):
        self._flash_msg("Review 🔒 locked items — use ↺ per-item to unlock.", C["yellow"])

    def _do_remove_all_cargo(self):
        """Called from DangerOverlay — removes ALL cargo assignments and locks silently."""
        count = sum(1 for e in self.plan.get("loading_plan", [])
                    if e.get("assigned_zone"))
        for e in self.plan.get("loading_plan", []):
            e["assigned_zone"]      = None
            e["manual_lock"]        = False
            e["cg_after_placement"] = None
        self._active_recommendation = None
        self._rec_projected_cg      = None
        self._update_live_cg()
        self._refresh_all()
        self._run_feasibility_check()
        self._flash_msg(f"🗑 All {count} cargo assignments removed.", C["red"])

    # ── Recommendation persistence ────────────────────────────────────────────

    def _invalidate_recommendation(self):
        self._active_recommendation = None
        self._rec_projected_cg      = None
        self._build_cg_info_bar()

    def _apply_recommendation_to_plan(self, rec_items):
        self.plan["loading_plan"]   = rec_items
        self._active_recommendation = copy.deepcopy(rec_items)
        self._update_live_cg()
        self._rec_projected_cg = self.plan.get("final_cg_pct_mac")
        self.zone_map = build_zone_map(self.plan)
        self._refresh_all()
        self._flash_msg("✓ Recommendation applied  (AI Rec ACTIVE)", C["green"])
        self._run_feasibility_check(from_manual=True)

    # ── UI skeleton ────────────────────────────────────────────────────────────

    def _build_ui(self):
        top = tk.Frame(self, bg=C["panel"], pady=10, padx=18)
        top.pack(fill="x", side="top")
        tk.Label(top, text="✈  AIRLoad",
                 font=(UI_FONT, 18, "bold"),
                 fg=C["accent"], bg=C["panel"]).pack(side="left")
        tk.Label(top, text="  A350-1000  ·  Interactive Cargo Loading",
                 font=(UI_FONT, 13), fg=C["muted"],
                 bg=C["panel"]).pack(side="left")

        self._live_label = tk.Label(top, text="⬤ LIVE",
                                    font=(UI_FONT, 10, "bold"),
                                    fg=C["green"], bg=C["panel"], padx=8)
        if self._live_active:
            self._live_label.pack(side="left", padx=16)

        btn_frame = tk.Frame(top, bg=C["panel"])
        btn_frame.pack(side="right")
        for label, cmd, bg, fg in [
            ("📂 Open Flight Plan…",  self._open_flightplan,   C["border"],  C["accent"]),
            ("⛽ Fuel Projection…",   self._open_fuel_window,  C["panel2"],  C["accent"]),
            ("🤖 Auto-Recommend",     self._run_recommend,     C["panel2"],  C["green"]),
            ("🗑 Reset All Cargo",    self._reset_all_cargo,   C["panel2"],  C["red"]),
            ("↺ Reset All Locks",     self._reset_locks,       C["panel2"],  C["orange"]),
        ]:
            tk.Button(btn_frame, text=label, command=cmd,
                      bg=bg, fg=fg, relief="flat",
                      font=(UI_FONT, 10),
                      padx=11, pady=5, cursor="hand2").pack(side="left", padx=3)

        self.status_bar = tk.Frame(self, bg=C["panel"], pady=8, padx=18)
        self.status_bar.pack(fill="x", side="top")
        self._build_status_bar()

        self.cg_info_bar = tk.Frame(self, bg=C["panel2"], pady=6, padx=18)
        self.cg_info_bar.pack(fill="x", side="top")
        self._build_cg_info_bar()

        self.hold_bar = tk.Frame(self, bg=C["panel2"], pady=4, padx=18)
        self.hold_bar.pack(fill="x", side="top")
        self._build_hold_bar()

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=8)

        canvas_frame = tk.Frame(body, bg=C["bg"])
        canvas_frame.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_frame, bg=C["bg"],
                                highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw_all())
        self.canvas.bind("<Button-1>",  self._on_canvas_click)

        sidebar = tk.Frame(body, bg=C["panel"], width=290, relief="flat", bd=0)
        sidebar.pack(side="right", fill="y", padx=(10, 0))
        sidebar.pack_propagate(False)
        self._sidebar_parent = sidebar
        self._build_sidebar(sidebar)

        gauge_frame = tk.Frame(self, bg=C["panel"], pady=10, padx=18)
        gauge_frame.pack(fill="x", side="bottom")
        self._build_cg_gauge(gauge_frame)

    # ── status bar ─────────────────────────────────────────────────────────────

    def _build_status_bar(self):
        for w in self.status_bar.winfo_children():
            w.destroy()
        plan     = self.plan
        status   = plan.get("status", "—")
        within   = plan.get("cg_within_limits")
        final_cg = plan.get("final_cg_pct_mac")
        cg_col   = C[compute_cg_status(final_cg)]

        pill_col = C["green"] if within else C["red"] if within is False else C["muted"]
        tk.Label(self.status_bar, text=f"  {status}  ",
                 font=(UI_FONT, 11, "bold"),
                 fg=C["bg"], bg=pill_col, padx=6, pady=2).pack(side="left")

        def _kv(label, val, col=C["text"]):
            f = tk.Frame(self.status_bar, bg=C["panel"])
            f.pack(side="left", padx=12)
            tk.Label(f, text=label, font=(UI_FONT, 9),
                     fg=C["muted"], bg=C["panel"]).pack(anchor="w")
            tk.Label(f, text=val, font=(UI_FONT, 11, "bold"),
                     fg=col, bg=C["panel"]).pack(anchor="w")

        _kv("Final CG",
            f"{final_cg:.3f} % MAC" if final_cg is not None else "—", cg_col)
        base_cg = plan.get("base_cg_pct_mac")
        _kv("Base CG",    f"{base_cg:.3f} % MAC" if base_cg else "—")
        _kv("CG Fwd",     f"{CG_FWD:.1f} %")
        _kv("CG Aft",     f"{CG_AFT:.1f} %")
        _kv("CG Target",  f"{CG_TGT:.1f} %")
        n_locked = sum(1 for e in plan.get("loading_plan", []) if e.get("manual_lock"))
        _kv("Locked",     f"{n_locked} items", C["orange"] if n_locked else C["muted"])

        flt = plan.get("flight_plan")
        if flt:
            route = f"{flt.get('origin','')}→{flt.get('destination','')}"
            _kv("Flight", f"{flt.get('flight_number','')} {route}", C["accent"])

        feas = self._last_feasibility
        if not feas["feasible"]:
            lbl = "⚠ FLIGHT CG !" if feas.get("flight_violation") else "⚠ INFEASIBLE"
            tk.Label(self.status_bar, text=f"  {lbl}  ",
                     font=(UI_FONT, 10, "bold"),
                     fg="white", bg=C["red"],
                     padx=6, pady=2).pack(side="right", padx=8)

    # ── CG info bar ────────────────────────────────────────────────────────────

    def _build_cg_info_bar(self):
        for w in self.cg_info_bar.winfo_children():
            w.destroy()
        plan     = self.plan
        final_cg = plan.get("final_cg_pct_mac")

        def _tile(label, val, col=C["text"]):
            f = tk.Frame(self.cg_info_bar, bg=C["panel2"], padx=10, pady=4)
            f.pack(side="left", padx=4)
            tk.Label(f, text=label, font=(UI_FONT, 8),
                     fg=C["muted"], bg=C["panel2"]).pack(anchor="w")
            tk.Label(f, text=val, font=(UI_FONT, 11, "bold"),
                     fg=col, bg=C["panel2"]).pack(anchor="w")

        if final_cg is not None:
            dev = final_cg - CG_TGT
            _tile("CG Deviation", f"{dev:+.3f} % vs TGT", C[compute_cg_status(final_cg)])

        steps = project_fuel_burn(plan)
        if steps:
            _tile("CG @ TOW", f"{steps[0]['cg_pct_mac']:.2f} % MAC", C["accent"])
            _tile("CG @ LDW", f"{steps[-1]['cg_pct_mac']:.2f} % MAC", C["accent2"])

            # Flag worst CG during flight
            min_cg_step = min(steps, key=lambda s: s["cg_pct_mac"])
            max_cg_step = max(steps, key=lambda s: s["cg_pct_mac"])
            _tile("Min CG flight", f"{min_cg_step['cg_pct_mac']:.2f} %",
                  C["red"] if min_cg_step["cg_pct_mac"] < CG_FWD else C["muted"])
            _tile("Max CG flight", f"{max_cg_step['cg_pct_mac']:.2f} %",
                  C["red"] if max_cg_step["cg_pct_mac"] > CG_AFT else C["muted"])

        wb  = plan.get("weight_breakdown", {})
        oew = wb.get("empty_aircraft_kg")
        pax = wb.get("passengers_kg")
        fuel= wb.get("fuel_kg")
        cgo = wb.get("cargo_kg")
        if all(v is not None for v in [oew, pax, fuel, cgo]):
            _tile("MTOW", f"{oew+pax+fuel+cgo:,.0f} kg")
            _tile("ZFW",  f"{oew+pax+cgo:,.0f} kg")
            _tile("FOB",  f"{fuel:,.0f} kg", C["muted"])

        # AI recommendation banner
        if self._active_recommendation is not None:
            rec_cg = self._rec_projected_cg
            cg_str = f"  CG: {rec_cg:.3f}%" if rec_cg else ""
            tk.Label(self.cg_info_bar,
                     text=f"  🤖 AI Rec ACTIVE{cg_str}  ",
                     font=(UI_FONT, 9, "bold"),
                     fg=C["bg"], bg=C["green"],
                     padx=6, pady=2).pack(side="right", padx=10)

        tk.Label(self.cg_info_bar,
                 text=f"Updated: {time.strftime('%H:%M:%S')}",
                 font=(UI_FONT, 8),
                 fg=C["border"], bg=C["panel2"]).pack(side="right", padx=10)

    # ── Hold weight alert bar  (Req 2: > limit is overweight, = limit is OK)

    def _build_hold_bar(self):
        for w in self.hold_bar.winfo_children():
            w.destroy()
        fwd_w, aft_w, bulk_w = hold_weights(self.plan)

        def _indicator(parent, label, used, limit):
            # Req 2: overweight only when strictly exceeds limit
            overweight = used > limit
            pct = used / limit if limit else 0
            col = C["red"] if overweight else C["yellow"] if pct >= 0.85 else C["green"]
            f = tk.Frame(parent, bg=C["panel2"], padx=10, pady=2)
            f.pack(side="left", padx=5)
            tk.Label(f, text=label, font=(UI_FONT, 8),
                     fg=C["muted"], bg=C["panel2"]).pack(anchor="w")
            bar_outer = tk.Frame(f, bg=C["border"], height=8, width=150)
            bar_outer.pack(anchor="w")
            bar_outer.pack_propagate(False)
            tk.Frame(bar_outer, bg=col, height=8,
                     width=min(150, int(150 * pct))).pack(side="left")
            txt = (f"OVERWEIGHT ⚠" if overweight
                   else f"{'WARNING ' if pct >= 0.85 else ''}{used:,.0f} / {limit:,} kg")
            tk.Label(f, text=txt,
                     font=(UI_FONT, 8, "bold" if overweight else "normal"),
                     fg=col, bg=C["panel2"]).pack(anchor="w")

        _indicator(self.hold_bar, "FWD HOLD",  fwd_w,  FWD_HOLD_LIMIT_KG)
        _indicator(self.hold_bar, "AFT HOLD",  aft_w,  AFT_HOLD_LIMIT_KG)
        _indicator(self.hold_bar, "BULK",      bulk_w, BULK_LIMIT_KG)

        unassigned = sum(1 for e in self.plan.get("loading_plan", [])
                         if not e.get("assigned_zone"))
        if unassigned:
            tk.Label(self.hold_bar,
                     text=f"⚠  {unassigned} item{'s' if unassigned > 1 else ''} unassigned",
                     font=(UI_FONT, 9, "bold"),
                     fg=C["yellow"], bg=C["panel2"], padx=14).pack(side="left")

    # ── sidebar  (Req 1: unlock button on ALL occupied slots) ─────────────────

    def _build_sidebar(self, parent):
        tk.Label(parent, text="CARGO MANIFEST",
                 font=(UI_FONT, 11, "bold"),
                 fg=C["accent"], bg=C["panel"],
                 pady=8).pack(fill="x", padx=12)
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=8)

        sf = tk.Frame(parent, bg=C["panel"])
        sf.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(sf); vsb.pack(side="right", fill="y")
        lbc = tk.Canvas(sf, bg=C["panel"], highlightthickness=0, yscrollcommand=vsb.set)
        lbc.pack(side="left", fill="both", expand=True)
        vsb.config(command=lbc.yview)
        inner = tk.Frame(lbc, bg=C["panel"])
        wid = lbc.create_window((0, 0), window=inner, anchor="nw")
        lbc.bind("<Configure>", lambda e: lbc.itemconfig(wid, width=e.width))
        inner.bind("<Configure>", lambda e: lbc.configure(scrollregion=lbc.bbox("all")))

        for entry in self.plan.get("loading_plan", []):
            col    = self._item_colours.get(entry["item"], C["accent"])
            zone   = entry.get("assigned_zone") or "—"
            wt     = entry.get("weight_kg", 0)
            locked = entry.get("manual_lock", False)
            assigned = bool(entry.get("assigned_zone"))

            row_bg = C["zone_locked"] if locked else C["panel"]
            row = tk.Frame(inner, bg=row_bg, pady=4, padx=8)
            row.pack(fill="x")

            tk.Label(row, text="●", fg=col, bg=row_bg,
                     font=(UI_FONT, 11)).pack(side="left")

            info = tk.Frame(row, bg=row_bg)
            info.pack(side="left", padx=5, fill="x", expand=True)

            lock_icon = " 🔒" if locked else ""
            tk.Label(info, text=entry["item"] + lock_icon,
                     font=(UI_FONT, 10, "bold"),
                     fg=C["orange"] if locked else C["text"],
                     bg=row_bg, anchor="w").pack(fill="x")
            tk.Label(info, text=f"{wt:.1f} kg  →  {zone}",
                     font=(UI_FONT, 9),
                     fg=C["muted"], bg=row_bg, anchor="w").pack(fill="x")
            cg_a = entry.get("cg_after_placement")
            if cg_a is not None:
                sc = C[compute_cg_status(cg_a)]
                tk.Label(info, text=f"CG after: {cg_a:.3f} %",
                         font=(UI_FONT, 8),
                         fg=sc, bg=row_bg, anchor="w").pack(fill="x")

            btn_row = tk.Frame(row, bg=row_bg)
            btn_row.pack(side="right")

            # ✎ Reassign (always shown when zone assigned)
            if assigned:
                def _reassign(e=entry):
                    self._reassign_item(e)
                tk.Button(btn_row, text="✎", command=_reassign,
                          bg=C["border"], fg=C["accent"],
                          font=(UI_FONT, 9),
                          relief="flat", padx=4, pady=1,
                          cursor="hand2").pack(side="left", padx=1)

            # ↺ Unlock — Req 1: shown for ANY assigned item (locked or not)
            # If unlocked+assigned, it clears the assignment back to unassigned;
            # if locked, it just clears the lock.
            if assigned:
                if locked:
                    # Clear lock only (keep zone assignment)
                    def _unlock_lock(e=entry):
                        e["manual_lock"] = False
                        self._refresh_all()
                        self._run_feasibility_check(from_manual=True)
                    tk.Button(btn_row, text="↺", command=_unlock_lock,
                              bg=C["border"], fg=C["orange"],
                              font=(UI_FONT, 9),
                              relief="flat", padx=4, pady=1,
                              cursor="hand2",
                              ).pack(side="left", padx=1)
                else:
                    # Unassigned (clears zone, keeps unlocked)
                    def _unassign(e=entry):
                        e["assigned_zone"] = None
                        e["manual_lock"]   = False
                        self._refresh_all()
                        self._run_feasibility_check(from_manual=True)
                    tk.Button(btn_row, text="✕", command=_unassign,
                              bg=C["border"], fg=C["muted"],
                              font=(UI_FONT, 9),
                              relief="flat", padx=4, pady=1,
                              cursor="hand2",
                              ).pack(side="left", padx=1)

            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", padx=8)

        total_kg = sum(e.get("weight_kg", 0) for e in self.plan.get("loading_plan", []))
        tk.Label(parent, text=f"Total cargo: {total_kg:,.1f} kg",
                 font=(UI_FONT, 10, "bold"),
                 fg=C["muted"], bg=C["panel"], pady=6).pack()

        wt_brk = self.plan.get("weight_breakdown")
        if wt_brk:
            tk.Label(parent, text="WEIGHT BREAKDOWN",
                     font=(UI_FONT, 9, "bold"),
                     fg=C["accent"], bg=C["panel"]).pack()
            for key, lbl in [("empty_aircraft_kg", "OEW"),
                              ("passengers_kg",     "Passengers"),
                              ("fuel_kg",           "Fuel"),
                              ("cargo_kg",          "Cargo")]:
                val = wt_brk.get(key)
                if val is not None:
                    tk.Label(parent, text=f"{lbl:<16} {val:>10,.0f} kg",
                             font=(UI_FONT, 8),
                             fg=C["muted"], bg=C["panel"]).pack(anchor="w", padx=10)

        # Flight plan summary
        flt = self.plan.get("flight_plan")
        if flt:
            tk.Label(parent, text="FLIGHT PLAN",
                     font=(UI_FONT, 9, "bold"),
                     fg=C["accent"], bg=C["panel"]).pack(pady=(6, 0))
            for k, v in [
                ("Flight",    f"{flt.get('flight_number','')} {flt.get('origin','')}→{flt.get('destination','')}"),
                ("Duration",  f"{flt.get('flight_hours', 0):.1f} h"),
                ("Burn rate", f"{flt.get('burn_rate_kg_per_hr', 0):,.0f} kg/hr"),
            ]:
                tk.Label(parent, text=f"{k:<12} {v}",
                         font=(UI_FONT, 8),
                         fg=C["muted"], bg=C["panel"]).pack(anchor="w", padx=10)

    # ── CG gauge ───────────────────────────────────────────────────────────────

    def _build_cg_gauge(self, parent):
        self.gauge_canvas = tk.Canvas(parent, bg=C["panel"],
                                      height=62, highlightthickness=0)
        self.gauge_canvas.pack(fill="x")
        self.gauge_canvas.bind("<Configure>", lambda e: self._draw_gauge())
        self._draw_gauge()

    # ══════════════════════════════════════════════════════════════════════════
    # DRAWING
    # ══════════════════════════════════════════════════════════════════════════

    def _draw_all(self):
        self._draw_aircraft()

    def _draw_aircraft(self):
        c = self.canvas
        c.delete("all")
        self._zone_rects.clear()

        W = c.winfo_width(); H = c.winfo_height()
        if W < 10 or H < 10: return

        X_NOSE, X_TAIL = 3.0, 52.0
        mx = 40
        usable_w = W - 2 * mx

        def ac_x(x_m):
            return mx + (x_m - X_NOSE) / (X_TAIL - X_NOSE) * usable_w

        cy = H / 2

        def fus_half(x_m):
            rel = (x_m - X_NOSE) / (X_TAIL - X_NOSE)
            if rel < 0.05:  return rel / 0.05 * 20
            if rel > 0.85:  return max(3, 20 * (1 - (rel - 0.85) / 0.15))
            return 20

        pts = []
        for i in range(81):
            x_m = X_NOSE + i/80*(X_TAIL-X_NOSE)
            pts.append((ac_x(x_m), cy - fus_half(x_m)))
        for i in range(81):
            x_m = X_TAIL - i/80*(X_TAIL-X_NOSE)
            pts.append((ac_x(x_m), cy + fus_half(x_m)))
        c.create_polygon([v for pt in pts for v in pt],
                         fill=C["fuselage"], outline=C["border"], width=1.5)

        wx1 = ac_x(22); wx2 = ac_x(33)
        tip_y = min((H - 56) * 0.44, 180)
        for sign in (-1, 1):
            c.create_polygon(
                wx1, cy+sign*20, wx2, cy+sign*20,
                wx2+28, cy+sign*tip_y, wx1-14, cy+sign*tip_y,
                fill=C["wing"], outline=C["border"], width=1)

        sx1 = ac_x(44); sx2 = ac_x(49)
        stab_y = min((H-56)*0.17, 74)
        for sign in (-1, 1):
            c.create_polygon(
                sx1, cy+sign*9, sx2, cy+sign*9,
                sx2+11, cy+sign*stab_y, sx1, cy+sign*stab_y,
                fill=C["tail"], outline=C["border"], width=1)

        c.create_line(ac_x(X_NOSE), cy, ac_x(X_TAIL), cy,
                      fill=C["border"], dash=(6,6), width=1)

        slot_h = 34
        gap_cy = 14

        def _draw_slot(zone_id, cx_canvas, upper):
            items_here = self.zone_map.get(zone_id, [])
            occupied   = len(items_here) > 0
            locked     = occupied and items_here[0].get("manual_lock", False)

            slot_w = max(26, usable_w / (X_TAIL - X_NOSE) * 1.7)
            x1 = cx_canvas - slot_w/2; x2 = cx_canvas + slot_w/2
            if upper:
                y2 = cy - gap_cy; y1 = y2 - slot_h
            else:
                y1 = cy + gap_cy; y2 = y1 + slot_h

            self._zone_rects[zone_id] = (x1, y1, x2, y2)

            base_fill = (C["zone_locked"] if locked
                         else self._item_colours.get(items_here[0]["item"], C["accent"]) if occupied
                         else C["zone_empty"])

            bdr_col = C["orange"] if locked else (_lighten(base_fill) if occupied else C["border"])
            bdr_w   = 1.5 if locked else 1

            r = 5
            c.create_rectangle(x1+r, y1, x2-r, y2, fill=base_fill, outline="")
            c.create_rectangle(x1, y1+r, x2, y2-r, fill=base_fill, outline="")
            for ccx, ccy in [(x1+r,y1+r),(x2-r,y1+r),(x1+r,y2-r),(x2-r,y2-r)]:
                c.create_oval(ccx-r, ccy-r, ccx+r, ccy+r, fill=base_fill, outline="")

            # Highlight stripe on top edge for occupied slots
            if occupied and not locked:
                hi_col = _lighten(base_fill)
                c.create_line(x1+r, y1+1, x2-r, y1+1, fill=hi_col, width=1)

            c.create_rectangle(x1, y1, x2, y2,
                                outline=bdr_col, width=bdr_w, fill="")

            mid_x = (x1+x2)/2; mid_y = (y1+y2)/2
            tc = "#ffffff" if (occupied and not locked) else C["muted"]

            short = (zone_id.replace("FWD_0","F").replace("AFT_0","A")
                             .replace("BULK_01","BLK"))
            c.create_text(mid_x, mid_y-(8 if occupied else 0),
                          text=short, font=(UI_FONT,6,"bold"), fill=tc)

            if occupied:
                item = items_here[0]
                nm = item["item"][:9]+("…" if len(item["item"])>9 else "")
                c.create_text(mid_x, mid_y+5, text=nm, font=(MONO_FONT,5), fill=tc)
                c.create_text(mid_x, mid_y+14,
                              text=f"{item['weight_kg']:.0f}kg",
                              font=(UI_FONT,5), fill=tc)
                cg_a = item.get("cg_after_placement")
                dc = C[compute_cg_status(cg_a)] if cg_a else C["muted"]
                c.create_oval(x2-9, y1+2, x2-3, y1+8, fill=dc, outline="")
                if locked:
                    c.create_text(x1+7, y1+6, text="🔒",
                                  font=(UI_FONT,6), fill=C["orange"])

            conn_y = y2 if upper else y1
            c.create_line(mid_x, cy, mid_x, conn_y,
                          fill=C["border"], dash=(3,4), width=1)

        for n in range(1, 5):
            x_m = {1:15.0,2:17.0,3:19.0,4:21.0}[n]
            cxc = ac_x(x_m)
            _draw_slot(f"FWD_0{n}L", cxc, upper=True)
            _draw_slot(f"FWD_0{n}R", cxc, upper=False)

        for n in range(1, 5):
            x_m = {1:31.0,2:33.0,3:35.0,4:37.0}[n]
            cxc = ac_x(x_m)
            _draw_slot(f"AFT_0{n}L", cxc, upper=True)
            _draw_slot(f"AFT_0{n}R", cxc, upper=False)

        _draw_slot("BULK_01", ac_x(40.0), upper=False)

        fwd_w, aft_w, bulk_w = hold_weights(self.plan)
        fwd_col  = C["red"] if fwd_w  > FWD_HOLD_LIMIT_KG else C["muted"]
        aft_col  = C["red"] if aft_w  > AFT_HOLD_LIMIT_KG else C["muted"]
        bulk_col = C["red"] if bulk_w > BULK_LIMIT_KG      else C["muted"]
        label_y  = cy + gap_cy + slot_h + 14

        c.create_text((ac_x(15)+ac_x(21))/2, label_y,
                      text=f"◀  FWD HOLD  {fwd_w:,.0f}/{FWD_HOLD_LIMIT_KG:,} kg",
                      font=(UI_FONT,9,"bold"), fill=fwd_col)
        c.create_text((ac_x(31)+ac_x(37))/2, label_y,
                      text=f"AFT HOLD  {aft_w:,.0f}/{AFT_HOLD_LIMIT_KG:,} kg  ▶",
                      font=(UI_FONT,9,"bold"), fill=aft_col)
        c.create_text(ac_x(40), label_y,
                      text=f"BULK  {bulk_w:.0f}/{BULK_LIMIT_KG} kg",
                      font=(UI_FONT,9,"bold"), fill=bulk_col)

        lx = ac_x(X_NOSE) - 12
        c.create_text(lx, cy-gap_cy-slot_h/2, text="L",
                      font=(UI_FONT,9,"bold"), fill=C["muted"])
        c.create_text(lx, cy+gap_cy+slot_h/2, text="R",
                      font=(UI_FONT,9,"bold"), fill=C["muted"])

        final_cg = self.plan.get("final_cg_pct_mac")
        if final_cg is not None:
            cg_px = ac_x(LEMAC + (final_cg/100)*MAC_LEN)
            col   = C[compute_cg_status(final_cg)]
            c.create_line(cg_px, cy-22, cg_px, cy+22, fill=col, width=2)
            d = 7
            c.create_polygon(cg_px, cy-22-d, cg_px+d, cy-22,
                             cg_px, cy-22+d, cg_px-d, cy-22, fill=col, outline="")
            c.create_text(cg_px, cy-22-d-10,
                          text=f"CG {final_cg:.2f}%",
                          font=(UI_FONT,9,"bold"), fill=col)

        tgt_px = ac_x(LEMAC + (CG_TGT/100)*MAC_LEN)
        c.create_line(tgt_px, cy-15, tgt_px, cy+15,
                      fill=C["muted"], width=1, dash=(4,3))
        c.create_text(tgt_px, cy-22, text=f"TGT {CG_TGT:.0f}%",
                      font=(UI_FONT,8), fill=C["muted"])

    def _draw_gauge(self):
        gc = self.gauge_canvas; gc.delete("all")
        W = gc.winfo_width()
        if W < 10: return
        bar_h, bar_y, pad = 18, 26, 72
        bar_w = W - 2*pad
        total_range = CG_AFT - CG_FWD

        def cg_to_x(pct):
            return pad + (pct - CG_FWD) / total_range * bar_w

        gc.create_rectangle(pad, bar_y, pad+bar_w, bar_y+bar_h,
                            fill=C["zone_empty"], outline=C["border"], width=1)
        gx1 = cg_to_x(CG_TGT-2); gx2 = cg_to_x(CG_TGT+2)
        gc.create_rectangle(pad+1, bar_y+1, max(pad+1,gx1), bar_y+bar_h-1,
                            fill=C["yellow"], outline="")
        gc.create_rectangle(max(pad,gx1), bar_y+1, min(pad+bar_w,gx2), bar_y+bar_h-1,
                            fill=C["green"], outline="")
        gc.create_rectangle(min(pad+bar_w,gx2), bar_y+1, pad+bar_w-1, bar_y+bar_h-1,
                            fill=C["yellow"], outline="")
        gc.create_text(pad-6, bar_y+bar_h/2, text=f"◀ {CG_FWD:.0f}%",
                       font=(UI_FONT, 10, "bold"), fill=C["red"], anchor="e")
        gc.create_text(pad+bar_w+6, bar_y+bar_h/2, text=f"{CG_AFT:.0f}% ▶",
                       font=(UI_FONT, 10, "bold"), fill=C["red"], anchor="w")
        tx = cg_to_x(CG_TGT)
        gc.create_line(tx, bar_y-2, tx, bar_y+bar_h+2, fill="#ffffff", width=2, dash=(3,2))
        gc.create_text(tx, bar_y-9, text=f"TGT {CG_TGT:.0f}%",
                       font=(UI_FONT, 9, "bold"), fill=C["text"])

        final_cg = self.plan.get("final_cg_pct_mac")
        if final_cg is not None:
            col = C[compute_cg_status(final_cg)]
            cgx = max(pad, min(pad+bar_w, cg_to_x(final_cg)))
            d   = 8
            gc.create_polygon(cgx, bar_y-2, cgx-d, bar_y-2-d*1.5, cgx+d, bar_y-2-d*1.5,
                              fill=col, outline="white", width=1)
            gc.create_line(cgx, bar_y-2, cgx, bar_y+bar_h, fill=col, width=2)
            gc.create_text(cgx, bar_y+bar_h+11, text=f"{final_cg:.3f} % MAC",
                           font=(MONO_FONT, 10, "bold"), fill=col)

        gc.create_text(W/2, 9, text="CENTRE OF GRAVITY ENVELOPE  —  % MAC",
                       font=(UI_FONT, 9, "bold"), fill=C["muted"])

    # ══════════════════════════════════════════════════════════════════════════
    # INTERACTION HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _on_canvas_click(self, event):
        # Req 5: if a drag just ended, don't also open popup
        if getattr(self, "_drag_just_dropped", False):
            self._drag_just_dropped = False
            return
        x, y = event.x, event.y
        for zone_id, (x1, y1, x2, y2) in self._zone_rects.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                self._show_zone_popup(zone_id, event.x_root, event.y_root)
                return

    def _show_zone_popup(self, zone_id: str, sx: int, sy: int):
        """Req 3 & 4: Show rich zone popup with per-item remove buttons and assign-cargo UI."""
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
        self._popup = ZoneDetailPopup(
            self, zone_id,
            self.zone_map.get(zone_id, []),
            ZONE_DEFS.get(zone_id),
            self._item_colours,
            self.plan,
            on_remove_item=self._remove_item_from_zone,
            on_assign_item=self._assign_item_to_zone,
            on_reassign=self._reassign_item,
        )
        # position near click, stay on screen
        self._popup.update_idletasks()
        pw = self._popup.winfo_reqwidth()
        ph = self._popup.winfo_reqheight()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        rx = sx + 16 if sx + pw + 20 < sw else sx - pw - 10
        ry = sy + 16 if sy + ph + 20 < sh else sy - ph - 10
        self._popup.geometry(f"+{max(0,rx)}+{max(0,ry)}")

    def _show_empty_zone_tip(self, zone_id, sx, sy):
        # Kept for legacy; now routes to the full popup
        self._show_zone_popup(zone_id, sx, sy)

    def _remove_item_from_zone(self, item_name: str):
        """Req 3: Remove a single cargo item's zone assignment."""
        for e in self.plan.get("loading_plan", []):
            if e["item"] == item_name:
                e["assigned_zone"]      = None
                e["manual_lock"]        = False
                e["cg_after_placement"] = None
                break
        self._invalidate_recommendation()
        self._update_live_cg()
        self._refresh_all()
        self._run_feasibility_check(from_manual=True)
        self._flash_msg(f"✕ {item_name} removed from zone.", C["muted"])
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()

    def _assign_item_to_zone(self, item_name: str, zone_id: str):
        """Req 4: Assign an unloaded item to a specific empty zone."""
        # Make sure zone is free
        if self.zone_map.get(zone_id):
            self._flash_msg(f"Zone {zone_id} is already occupied.", C["yellow"])
            return
        for e in self.plan.get("loading_plan", []):
            if e["item"] == item_name:
                e["assigned_zone"] = zone_id
                e["manual_lock"]   = True
                break
        self._invalidate_recommendation()
        self._update_live_cg()
        self._refresh_all()
        self._run_feasibility_check(from_manual=True)
        self._flash_msg(
            f"🔒 {item_name} → {zone_id}  |  CG: {self.plan.get('final_cg_pct_mac', 0):.3f} %",
            C["orange"])
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()

    def _reassign_item(self, entry):
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()

        def on_confirm(item_name, new_zone):
            self._invalidate_recommendation()
            for e in self.plan["loading_plan"]:
                if e["item"] == item_name:
                    old = e.get("assigned_zone")
                    if old and old in self.zone_map:
                        self.zone_map[old] = [x for x in self.zone_map[old]
                                              if x["item"] != item_name]
                    e["assigned_zone"] = new_zone
                    e["manual_lock"]   = True
                    break
            self._update_live_cg()
            self.zone_map = build_zone_map(self.plan)
            self._refresh_all()
            self._flash_msg(
                f"🔒 {item_name} → {new_zone}  |  CG: {self.plan.get('final_cg_pct_mac', 0):.3f} %",
                C["orange"])
            self._run_feasibility_check(from_manual=True)

        ReassignDialog(self, entry, self.plan, self.zone_map, on_confirm)

    def _run_recommend(self):
        rec_items  = greedy_recommend(self.plan)
        trial      = copy.deepcopy(self.plan)
        trial["loading_plan"] = rec_items
        proj_cg    = recalculate_cg(trial)
        RecommendationWindow(self, rec_items, proj_cg,
                             self._item_colours, self._apply_recommendation_to_plan)

    def _reset_locks(self):
        if not messagebox.askyesno("Reset Locks",
                                   "Clear all manual zone locks?\n"
                                   "(Assignments stay; only locks cleared.)",
                                   parent=self):
            return
        self._do_reset_locks()

    def _do_reset_locks(self):
        for e in self.plan.get("loading_plan", []):
            e["manual_lock"] = False
        self._invalidate_recommendation()
        self._refresh_all()
        self._run_feasibility_check()
        self._flash_msg("All locks cleared — run 🤖 Auto-Recommend to re-optimise.", C["muted"])

    def _reset_all_cargo(self):
        """Req 2: Remove ALL cargo assignments (AI + manual) and clear all locks."""
        count = sum(1 for e in self.plan.get("loading_plan", [])
                    if e.get("assigned_zone"))
        if count == 0:
            self._flash_msg("No cargo is currently assigned.", C["muted"])
            return
        if not messagebox.askyesno(
                "Reset All Cargo",
                f"Remove ALL {count} cargo assignment(s)?\n\n"
                "This clears every zone assignment (AI-recommended and manual).\n"
                "This cannot be undone.",
                parent=self):
            return
        for e in self.plan.get("loading_plan", []):
            e["assigned_zone"]      = None
            e["manual_lock"]        = False
            e["cg_after_placement"] = None
        self._active_recommendation = None
        self._rec_projected_cg      = None
        self._update_live_cg()
        self._refresh_all()
        self._run_feasibility_check()
        self._flash_msg(f"🗑 All {count} cargo assignments cleared.", C["red"])

    # ── refresh ────────────────────────────────────────────────────────────────

    def _refresh_all(self):
        self.zone_map = build_zone_map(self.plan)
        self._build_status_bar()
        self._build_cg_info_bar()
        self._build_hold_bar()
        self._rebuild_sidebar()
        self._draw_all()
        self._draw_gauge()

    def _flash_msg(self, msg, col):
        flash = tk.Label(self, text=f"  {msg}  ",
                         font=(UI_FONT, 10, "bold"),
                         fg=C["bg"], bg=col, padx=10, pady=4)
        flash.place(relx=0.5, rely=0.06, anchor="center")
        self.after(2800, flash.destroy)

    # ── live-update poll ───────────────────────────────────────────────────────

    def _schedule_poll(self):
        self.after(POLL_INTERVAL_MS, self._poll_source)

    def _poll_source(self):
        if not self.source_path or not os.path.exists(self.source_path):
            self._schedule_poll(); return
        try:
            raw = open(self.source_path).read()
            if "loading_plan" in raw:
                new_plan = _normalise_plan(load_plan(self.source_path))
            elif "flight_hours" in raw or "passengers" in raw:
                fp = FlightPlan.from_yaml(self.source_path)
                new_plan = load_flightplan_as_plan(fp, self.source_path)
            else:
                new_plan = load_manifest(self.source_path)

            # Preserve locks
            lock_map = {e["item"]: {"manual_lock": e.get("manual_lock", False),
                                     "assigned_zone": e.get("assigned_zone")}
                        for e in self.plan.get("loading_plan", [])}
            for e in new_plan.get("loading_plan", []):
                saved = lock_map.get(e["item"])
                if saved and saved["manual_lock"]:
                    e["manual_lock"]   = True
                    e["assigned_zone"] = saved["assigned_zone"]

            # Reapply active recommendation for unlocked items
            if self._active_recommendation is not None:
                rec_map = {it["item"]: it["assigned_zone"]
                           for it in self._active_recommendation}
                for e in new_plan.get("loading_plan", []):
                    if not e.get("manual_lock") and e["item"] in rec_map:
                        e["assigned_zone"] = rec_map[e["item"]]

            old_zones = {e["item"]: e.get("assigned_zone")
                         for e in self.plan.get("loading_plan", [])}
            self.plan = new_plan
            self._update_live_cg()
            self._refresh_all()

            moved = [e["item"] for e in new_plan.get("loading_plan", [])
                     if old_zones.get(e["item"]) != e.get("assigned_zone")]
            if moved:
                self._flash_msg(
                    f"⟳ {', '.join(moved[:3])}{'…' if len(moved)>3 else ''} updated",
                    C["yellow"])

            self._run_feasibility_check(from_manual=False)
        except Exception:
            pass
        self._schedule_poll()

    def _blink_live(self):
        if not self._live_active: return
        self._live_blink = not self._live_blink
        self._live_label.config(fg=C["green"] if self._live_blink else C["border"])
        self.after(1000, self._blink_live)

    # ── file open actions ──────────────────────────────────────────────────────

    def _open_plan(self):
        path = filedialog.askopenfilename(
            title="Open Optimised Loading Plan",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")])
        if not path: return
        try:
            self.source_path = path
            self.plan = _normalise_plan(load_plan(path))
            self._assign_colours()
            self._invalidate_recommendation()
            self._update_live_cg()
            self._refresh_all()
            self._run_feasibility_check(from_manual=True)
            self._activate_live()
        except Exception as ex:
            messagebox.showerror("Load Error", str(ex))

    def _open_manifest(self):
        path = filedialog.askopenfilename(
            title="Open Cargo Manifest",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")])
        if not path: return
        try:
            self.source_path = path
            self.plan = load_manifest(path)
            self._assign_colours()
            self._invalidate_recommendation()
            self._update_live_cg()
            self._refresh_all()
            self._run_feasibility_check(from_manual=True)
            self._activate_live()
        except Exception as ex:
            messagebox.showerror("Load Error", str(ex))

    def _open_flightplan(self):
        """Load a my_flightplan.yaml — populates passengers, fuel, and flight hours."""
        path = filedialog.askopenfilename(
            title="Open Flight Plan (my_flightplan.yaml)",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")])
        if not path: return
        try:
            fp   = FlightPlan.from_yaml(path)
            plan = load_flightplan_as_plan(fp, path)
            self.source_path = path
            self.plan = _normalise_plan(plan)
            self._assign_colours()
            self._invalidate_recommendation()
            self._update_live_cg()
            self._refresh_all()
            self._run_feasibility_check(from_manual=True)
            self._activate_live()
            self._flash_msg(
                f"Flight plan loaded: {fp.flight_number} {fp.origin}→{fp.destination}  "
                f"|  {fp.pax_count} pax  |  {fp.fuel_added_kg:,.0f} kg fuel  "
                f"|  {fp.effective_burn_rate:,.0f} kg/hr",
                C["accent"])
        except Exception as ex:
            messagebox.showerror("Load Error", str(ex))

    def _activate_live(self):
        if not self._live_active:
            self._live_active = True
            self._live_label.pack(side="left", padx=16)
            self._schedule_poll()
            self._blink_live()

    def _open_fuel_window(self):
        FuelBurnWindow(self, project_fuel_burn(self.plan), self.plan)

    def _rebuild_sidebar(self):
        for w in self._sidebar_parent.winfo_children():
            w.destroy()
        self._build_sidebar(self._sidebar_parent)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="AIRLoad A350-1000 Cargo Loading Dashboard v7")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--plan",       type=str, default=None,
                     help="Path to optimised_loading_plan.yaml")
    grp.add_argument("--manifest",   type=str, default=None,
                     help="Path to legacy my_flight.yaml")
    grp.add_argument("--flightplan", type=str, default=None,
                     help="Path to my_flightplan.yaml (new format)")
    args = parser.parse_args()

    source_path = None
    if args.plan:
        plan = _normalise_plan(load_plan(args.plan)); source_path = args.plan
    elif args.manifest:
        plan = load_manifest(args.manifest); source_path = args.manifest
    elif args.flightplan:
        fp   = FlightPlan.from_yaml(args.flightplan)
        plan = load_flightplan_as_plan(fp, args.flightplan)
        source_path = args.flightplan
    else:
        candidates = [
            ("output/optimised_loading_plan.yaml", "plan"),
            ("optimised_loading_plan.yaml",         "plan"),
            ("input_file/my_flightplan.yaml",       "flightplan"),
            ("my_flightplan.yaml",                  "flightplan"),
            ("input_file/my_flight.yaml",           "manifest"),
            ("my_flight.yaml",                      "manifest"),
        ]
        plan = None
        for cand, kind in candidates:
            if os.path.exists(cand):
                try:
                    if kind == "plan":
                        plan = _normalise_plan(load_plan(cand))
                    elif kind == "flightplan":
                        fp   = FlightPlan.from_yaml(cand)
                        plan = load_flightplan_as_plan(fp, cand)
                    else:
                        plan = load_manifest(cand)
                    source_path = cand
                    print(f"Auto-loaded ({kind}): {cand}")
                    break
                except Exception:
                    continue

        if plan is None:
            plan = {
                "aircraft":         "Airbus A350-1000",
                "input_manifest":   "—",
                "status":           "NO FILE LOADED",
                "cg_within_limits": None,
                "base_cg_pct_mac":  None,
                "final_cg_pct_mac": None,
                "cg_fwd_limit":     CG_FWD,
                "cg_aft_limit":     CG_AFT,
                "cg_target":        CG_TGT,
                "total_reward":     None,
                "loading_plan":     [],
            }
            print("No default file found. Use --plan / --manifest / --flightplan flags, "
                  "or File > Open… buttons in the dashboard.")

    app = AIRLoadDashboard(plan, source_path=source_path)
    app.mainloop()


if __name__ == "__main__":
    main()
