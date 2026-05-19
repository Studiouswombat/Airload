"""
main.py
=======
FastAPI backend for AIRLoad.
Connects the React frontend to the Python AI models.

How to run:
-----------
    cd src/backend
    uvicorn main:app --reload --port 8000

Endpoints:
----------
    GET  /                  health check
    GET  /zones             get all zone definitions
    POST /optimise          optimise a cargo manifest
    POST /recalculate       recalculate after RFID scan
    GET  /state             get current loading state
    POST /reset             reset loading state
"""

import sys
import os
import yaml
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

# ── point to AI modules ────────────────────────────────────────────────────────
sys.path.append(os.path.join(os.path.dirname(__file__), "../AI_FOR_ACTUAL_LOADING/main"))
sys.path.append(os.path.join(os.path.dirname(__file__), "../AI_FOR_LOADING_ZONES/main"))

from airload_person1_core_v2 import A3501000ReferenceModel
from stable_baselines3 import PPO
from env.cargo_env import CargoEnv

# ── app setup ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AIRLoad API",
    description="AI-powered aircraft cargo loading optimisation",
    version="1.0.0",
)

# ── CORS — allows React (port 3000) to talk to FastAPI (port 8000) ─────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── load reference model ───────────────────────────────────────────────────────
ref            = A3501000ReferenceModel
empty_aircraft = ref.operating_empty_aircraft()
zones_template = ref.cargo_zones()
passenger_zones= ref.passenger_zones()
fuel_tanks     = ref.fuel_tanks()

# ── load trained RL model ──────────────────────────────────────────────────────
MODEL_PATH = os.path.join(
    os.path.dirname(__file__),
    "../AI_FOR_ACTUAL_LOADING/main/models/best_model.zip"
)

try:
    model = PPO.load(MODEL_PATH)
    print(f"✓ RL model loaded from {MODEL_PATH}")
except Exception as e:
    model = None
    print(f"✗ Could not load RL model: {e}")

# ── running state (in memory) ──────────────────────────────────────────────────
# stores current loading state between RFID scan events
running_state = {
    "locked":    [],
    "remaining": [],
    "scan_count": 0,
}


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────────────────────────────────────

class CargoItem(BaseModel):
    name:      str
    weight_kg: float

class Manifest(BaseModel):
    cargo: List[CargoItem]

class RFIDScan(BaseModel):
    uld_id:  str
    zone:    str
    weight:  float

class ZoneAssignment(BaseModel):
    name:          str
    weight_kg:     float
    assigned_zone: str
    cg_after_placement: float


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def compute_cg(zone_weights: dict) -> float:
    """
    Computes total aircraft CG in % MAC.
    Includes empty aircraft + passengers + fuel + cargo.
    """
    total_moment = (
        empty_aircraft.operating_empty_weight_kg
        * empty_aircraft.empty_cg_x_m
    )
    total_weight = empty_aircraft.operating_empty_weight_kg

    # fuel
    for tank in fuel_tanks.values():
        total_moment += tank.fuel_kg * tank.x_position_m
        total_weight  += tank.fuel_kg

    # passengers
    for pax_zone in passenger_zones.values():
        pax_weight    = pax_zone.total_weight()
        total_moment += pax_weight * pax_zone.x_position_m
        total_weight  += pax_weight

    # cargo
    for zone_id, cargo_kg in zone_weights.items():
        if cargo_kg > 0:
            zone          = zones_template[zone_id]
            total_moment += cargo_kg * zone.x_position_m
            total_weight  += cargo_kg

    cg_arm = total_moment / total_weight
    return round(ref.x_to_percent_mac(cg_arm), 3)


def run_rl_agent(cargo_items: list, prefilled_zones: dict = None) -> dict:
    """
    Runs the RL agent on a list of cargo items.
    prefilled_zones: zones already occupied (for recalculation)
    Returns assignments and final CG.
    """
    if model is None:
        raise HTTPException(status_code=500, detail="RL model not loaded")

    env = CargoEnv(
        manifest_dir=os.path.join(
            os.path.dirname(__file__),
            "../AI_FOR_ACTUAL_LOADING/main/data/manifests"
        )
    )
    obs, _ = env.reset()

    # inject cargo
    env.cargo_items      = [{"name": i["name"], "weight_kg": i["weight_kg"]}
                             if isinstance(i, dict) else
                             {"name": i.name, "weight_kg": i.weight_kg}
                             for i in cargo_items]
    env.current_item_idx = 0
    env.zone_weights     = {z: 0.0 for z in env.zone_ids}

    # prefill locked zones
    if prefilled_zones:
        for zone_id, weight in prefilled_zones.items():
            env.zone_weights[zone_id] = weight

    env.current_cg = env.base_cg
    obs = env._get_observation()

    done        = False
    assignments = []

    while not done:
        action, _ = model.predict(obs, deterministic=True)
        zone_id   = env.zone_ids[action]
        item      = env.cargo_items[env.current_item_idx]

        obs, _, done, _, info = env.step(action)

        assignments.append({
            "name":               item["name"],
            "weight_kg":          item["weight_kg"],
            "assigned_zone":      zone_id,
            "cg_after_placement": round(info["cg_pct_mac"], 3),
        })

    return {
        "assignments":    assignments,
        "final_cg":       round(info["cg_pct_mac"], 3),
        "within_limits":  info["cg_within_limits"],
        "status":         "WITHIN LIMITS" if info["cg_within_limits"] else "WARNING - OUTSIDE LIMITS",
    }


def check_feasibility(locked_ulds, remaining_ulds, occupied_zones):
    """Checks if CG can still be balanced with remaining ULDs."""
    available_zones = [
        z for z in zones_template.keys()
        if z not in occupied_zones
    ]

    if not available_zones:
        return False, "No available zones remaining"

    zones_by_arm = sorted(
        available_zones,
        key=lambda z: zones_template[z].x_position_m
    )

    total_remaining = sum(u["weight_kg"] for u in remaining_ulds)

    # base moment
    base_moment = (
        empty_aircraft.operating_empty_weight_kg * empty_aircraft.empty_cg_x_m
    )
    base_weight = empty_aircraft.operating_empty_weight_kg

    for tank in fuel_tanks.values():
        base_moment += tank.fuel_kg * tank.x_position_m
        base_weight  += tank.fuel_kg

    for pax in passenger_zones.values():
        pw = pax.total_weight()
        base_moment += pw * pax.x_position_m
        base_weight  += pw

    for uld in locked_ulds:
        zone         = zones_template[uld["actual_zone"]]
        base_moment += uld["weight_kg"] * zone.x_position_m
        base_weight  += uld["weight_kg"]

    # forward and aft extremes
    fwd_cg = ref.x_to_percent_mac(
        (base_moment + total_remaining * zones_template[zones_by_arm[0]].x_position_m)
        / (base_weight + total_remaining)
    )
    aft_cg = ref.x_to_percent_mac(
        (base_moment + total_remaining * zones_template[zones_by_arm[-1]].x_position_m)
        / (base_weight + total_remaining)
    )

    fwd_limit = ref.CG_FORWARD_LIMIT_PERCENT_MAC
    aft_limit = ref.CG_AFT_LIMIT_PERCENT_MAC

    if fwd_cg > aft_limit:
        return False, f"CG {fwd_cg:.2f}% MAC even at most forward — aft of limit ({aft_limit}%)"
    if aft_cg < fwd_limit:
        return False, f"CG {aft_cg:.2f}% MAC even at most aft — forward of limit ({fwd_limit}%)"

    return True, f"Achievable range: {fwd_cg:.2f}% — {aft_cg:.2f}% MAC"


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def health_check():
    """Health check — confirms API is running."""
    return {
        "status":     "AIRLoad API running",
        "model":      "loaded" if model else "not loaded",
        "zones":      len(zones_template),
        "cg_limits":  f"{ref.CG_FORWARD_LIMIT_PERCENT_MAC}% — {ref.CG_AFT_LIMIT_PERCENT_MAC}% MAC",
    }


@app.get("/zones")
def get_zones():
    """Returns all zone definitions for the frontend to render."""
    return {
        "zones": [
            {
                "zone_id":      zone_id,
                "hold":         zone.hold,
                "x_position_m": zone.x_position_m,
                "max_weight_kg":zone.max_weight_kg,
            }
            for zone_id, zone in zones_template.items()
        ],
        "cg_fwd_limit":  ref.CG_FORWARD_LIMIT_PERCENT_MAC,
        "cg_aft_limit":  ref.CG_AFT_LIMIT_PERCENT_MAC,
        "cg_target":     ref.CG_TARGET_PERCENT_MAC,
        "aircraft": {
            "empty_weight_kg": empty_aircraft.operating_empty_weight_kg,
            "passengers_kg":   sum(p.total_weight() for p in passenger_zones.values()),
            "fuel_kg":         sum(t.fuel_kg for t in fuel_tanks.values()),
        }
    }


@app.post("/optimise")
def optimise(manifest: Manifest):
    """
    Takes a cargo manifest and returns the AI optimised loading plan.

    Request body:
        { "cargo": [{"name": "AKE2045", "weight_kg": 850.0}, ...] }

    Response:
        { "assignments": [...], "final_cg": 22.73, "status": "WITHIN LIMITS" }
    """
    if not manifest.cargo:
        raise HTTPException(status_code=400, detail="No cargo items provided")

    result = run_rl_agent(manifest.cargo)

    return {
        "status":       result["status"],
        "final_cg":     result["final_cg"],
        "within_limits":result["within_limits"],
        "cg_fwd_limit": ref.CG_FORWARD_LIMIT_PERCENT_MAC,
        "cg_aft_limit": ref.CG_AFT_LIMIT_PERCENT_MAC,
        "cg_target":    ref.CG_TARGET_PERCENT_MAC,
        "assignments":  result["assignments"],
    }


@app.post("/reset")
def reset_state(manifest: Manifest):
    """
    Resets the loading state for a new flight.
    Call this at the start of each new loading session.
    """
    global running_state
    running_state = {
        "locked":     [],
        "remaining":  [{"name": i.name, "weight_kg": i.weight_kg}
                       for i in manifest.cargo],
        "scan_count": 0,
        "flight_plan":[{"name": i.name, "weight_kg": i.weight_kg}
                       for i in manifest.cargo],
    }
    base_cg = compute_cg({})
    return {
        "message":    "State reset for new flight",
        "n_items":    len(manifest.cargo),
        "base_cg":    base_cg,
        "remaining":  running_state["remaining"],
    }


@app.post("/recalculate")
def recalculate(scan: RFIDScan):
    """
    Called every time an RFID scanner detects a ULD placement.
    Updates the locked state and recalculates optimal plan for remaining ULDs.

    Request body:
        { "uld_id": "AKE2045", "zone": "AFT_01", "weight": 850.0 }
    """
    global running_state

    if not running_state["remaining"] and not running_state["locked"]:
        raise HTTPException(
            status_code=400,
            detail="No active flight. Call /reset first."
        )

    # remove from remaining
    running_state["remaining"] = [
        item for item in running_state["remaining"]
        if item["name"] != scan.uld_id
    ]

    # add to locked
    running_state["locked"].append({
        "name":        scan.uld_id,
        "weight_kg":   scan.weight,
        "actual_zone": scan.zone,
    })
    running_state["scan_count"] += 1

    locked_ulds    = running_state["locked"]
    remaining_ulds = running_state["remaining"]
    occupied_zones = {u["actual_zone"] for u in locked_ulds}

    # current CG from locked ULDs
    zone_weights = {u["actual_zone"]: u["weight_kg"] for u in locked_ulds}
    current_cg   = compute_cg(zone_weights)

    # check if all loaded
    if not remaining_ulds:
        within = ref.CG_FORWARD_LIMIT_PERCENT_MAC <= current_cg <= ref.CG_AFT_LIMIT_PERCENT_MAC
        return {
            "status":      "LOADING COMPLETE",
            "feasible":    True,
            "current_cg":  current_cg,
            "within_limits": within,
            "locked_ulds": locked_ulds,
            "remaining":   [],
            "new_plan":    [],
        }

    # feasibility check
    feasible, feasibility_msg = check_feasibility(
        locked_ulds, remaining_ulds, occupied_zones
    )

    if not feasible:
        return {
            "status":      "WARNING - CG CANNOT BE BALANCED",
            "feasible":    False,
            "current_cg":  current_cg,
            "within_limits": False,
            "reason":      feasibility_msg,
            "locked_ulds": locked_ulds,
            "remaining":   remaining_ulds,
            "new_plan":    [],
            "recommendation": "Reposition a locked ULD before continuing",
        }

    # run RL agent on remaining
    prefilled = {u["actual_zone"]: u["weight_kg"] for u in locked_ulds}
    result    = run_rl_agent(remaining_ulds, prefilled_zones=prefilled)

    return {
        "status":      result["status"],
        "feasible":    True,
        "current_cg":  current_cg,
        "projected_cg":result["final_cg"],
        "within_limits":result["within_limits"],
        "scan_count":  running_state["scan_count"],
        "locked_ulds": locked_ulds,
        "remaining":   remaining_ulds,
        "new_plan":    result["assignments"],
    }


@app.get("/state")
def get_state():
    """Returns the current loading state."""
    zone_weights = {
        u["actual_zone"]: u["weight_kg"]
        for u in running_state["locked"]
    }
    current_cg = compute_cg(zone_weights) if running_state["locked"] else compute_cg({})

    return {
        "scan_count":  running_state["scan_count"],
        "locked_ulds": running_state["locked"],
        "remaining":   running_state["remaining"],
        "current_cg":  current_cg,
        "cg_fwd_limit":ref.CG_FORWARD_LIMIT_PERCENT_MAC,
        "cg_aft_limit": ref.CG_AFT_LIMIT_PERCENT_MAC,
        "cg_target":   ref.CG_TARGET_PERCENT_MAC,
    }