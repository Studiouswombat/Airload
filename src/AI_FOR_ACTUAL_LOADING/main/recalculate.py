"""
recalculate.py
==============
Real-time CG recalculation engine for AIRLoad.

Every time an RFID scanner detects a ULD placement,
this script:
1. Adds that ULD to the locked state
2. Recalculates optimal placement for remaining ULDs
3. Outputs a new plan for the loading crew

How to run:
-----------
    # scan one ULD at a time
    python recalculate.py --uld_id AKE2045 --zone FWD_01 --weight 850

    # or load full state manually
    python recalculate.py --state input_file/current_state.yaml

State is saved automatically between scans in:
    input_file/running_state.yaml
"""

import yaml
import os
import argparse
import numpy as np
from stable_baselines3 import PPO
from env.cargo_env import CargoEnv
from airload_person1_core_v2 import A3501000ReferenceModel

# ── argument parser ────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--uld_id",  type=str, default=None, help="ULD ID just scanned by RFID")
parser.add_argument("--zone",    type=str, default=None, help="Zone the ULD was placed in")
parser.add_argument("--weight",  type=float, default=None, help="Weight of the ULD in kg")
parser.add_argument("--state",   type=str, default=None, help="Path to a full state YAML file")
parser.add_argument("--reset",   action="store_true", help="Reset running state (new flight)")
args = parser.parse_args()

# ── settings ──────────────────────────────────────────────────────────────────
MODEL_PATH         = "models/best_model.zip"
OUTPUT_DIR         = "output"
RUNNING_STATE_PATH = "input_file/running_state.yaml"
FLIGHT_PLAN_PATH   = "input_file/my_flight.yaml"

os.makedirs(OUTPUT_DIR, exist_ok=True)

ref = A3501000ReferenceModel


# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# Keeps track of what has been loaded so far across multiple scans
# ─────────────────────────────────────────────────────────────────────────────

def load_running_state():
    """
    Loads the current running state from file.
    If no state exists yet, starts fresh from the flight plan.
    """
    if os.path.exists(RUNNING_STATE_PATH):
        with open(RUNNING_STATE_PATH, "r") as f:
            return yaml.safe_load(f)

    # no running state yet — load from flight plan
    print("No running state found — starting fresh from flight plan...")
    with open(FLIGHT_PLAN_PATH, "r") as f:
        flight_plan = yaml.safe_load(f)

    return {
        "flight_plan":  flight_plan["cargo"],   # full original plan
        "locked":       [],                      # nothing loaded yet
        "remaining":    flight_plan["cargo"],    # everything still to load
        "scan_count":   0,
    }


def save_running_state(state):
    """Saves the current running state to file."""
    with open(RUNNING_STATE_PATH, "w") as f:
        yaml.dump(state, f, default_flow_style=False, sort_keys=False)


def reset_running_state():
    """Clears the running state — call this at the start of a new flight."""
    if os.path.exists(RUNNING_STATE_PATH):
        os.remove(RUNNING_STATE_PATH)
    print("Running state reset. Ready for new flight.")


# ─────────────────────────────────────────────────────────────────────────────
# CG HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def compute_cg_from_locked(locked_ulds, zones_template, empty_aircraft):
    """Computes current CG in % MAC from locked ULDs only."""
    total_moment = (
        empty_aircraft.operating_empty_weight_kg
        * empty_aircraft.empty_cg_x_m
    )
    total_weight = empty_aircraft.operating_empty_weight_kg

    for uld in locked_ulds:
        zone         = zones_template[uld["actual_zone"]]
        total_moment += uld["weight_kg"] * zone.x_position_m
        total_weight  += uld["weight_kg"]

    cg_arm = total_moment / total_weight
    return ref.x_to_percent_mac(cg_arm), total_weight


def check_cg_feasibility(locked_ulds, remaining_ulds, zones_template,
                          empty_aircraft, occupied_zones):
    """
    Checks whether CG can still be balanced with remaining ULDs.
    Returns (feasible: bool, message: str)
    """
    available_zones = [
        z for z in zones_template.keys()
        if z not in occupied_zones
    ]

    if not available_zones:
        return False, "No available zones remaining for unloaded ULDs"

    zones_by_arm = sorted(
        available_zones,
        key=lambda z: zones_template[z].x_position_m
    )

    total_remaining_weight = sum(u["weight_kg"] for u in remaining_ulds)

    # base moment from locked ULDs + empty aircraft
    locked_moment = (
        empty_aircraft.operating_empty_weight_kg
        * empty_aircraft.empty_cg_x_m
    )
    locked_weight = empty_aircraft.operating_empty_weight_kg

    for uld in locked_ulds:
        zone          = zones_template[uld["actual_zone"]]
        locked_moment += uld["weight_kg"] * zone.x_position_m
        locked_weight  += uld["weight_kg"]

    # most forward possible CG
    fwd_moment     = locked_moment + total_remaining_weight * zones_template[zones_by_arm[0]].x_position_m
    fwd_total      = locked_weight + total_remaining_weight
    fwd_cg         = ref.x_to_percent_mac(fwd_moment / fwd_total)

    # most aft possible CG
    aft_moment     = locked_moment + total_remaining_weight * zones_template[zones_by_arm[-1]].x_position_m
    aft_total      = locked_weight + total_remaining_weight
    aft_cg         = ref.x_to_percent_mac(aft_moment / aft_total)

    fwd_limit = ref.CG_FORWARD_LIMIT_PERCENT_MAC
    aft_limit = ref.CG_AFT_LIMIT_PERCENT_MAC

    if fwd_cg > aft_limit:
        return False, (
            f"Even placing all remaining ULDs forward gives CG {fwd_cg:.2f}% MAC "
            f"— still aft of limit ({aft_limit}% MAC)"
        )
    if aft_cg < fwd_limit:
        return False, (
            f"Even placing all remaining ULDs aft gives CG {aft_cg:.2f}% MAC "
            f"— still forward of limit ({fwd_limit}% MAC)"
        )

    return True, f"Achievable CG range: {fwd_cg:.2f}% — {aft_cg:.2f}% MAC"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RECALCULATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def recalculate(state):
    """
    Core recalculation logic.
    Takes the current state, checks feasibility,
    runs the RL agent on remaining ULDs,
    and outputs a new plan.
    """
    locked_ulds    = state["locked"]
    remaining_ulds = state["remaining"]
    scan_count     = state["scan_count"]

    empty_aircraft = ref.operating_empty_aircraft()
    zones_template = ref.cargo_zones()
    occupied_zones = {uld["actual_zone"] for uld in locked_ulds}

    # ── current CG from locked ULDs ────────────────────────────────────────
    if locked_ulds:
        current_cg, _ = compute_cg_from_locked(
            locked_ulds, zones_template, empty_aircraft
        )
    else:
        current_cg = ref.x_to_percent_mac(empty_aircraft.empty_cg_x_m)

    print(f"\nCurrent CG (locked ULDs) : {current_cg:.2f}% MAC")
    print(f"CG envelope              : {ref.CG_FORWARD_LIMIT_PERCENT_MAC}% — {ref.CG_AFT_LIMIT_PERCENT_MAC}% MAC")
    print(f"Locked ULDs              : {len(locked_ulds)}")
    print(f"Remaining ULDs           : {len(remaining_ulds)}")

    # ── if nothing remaining, we are done ──────────────────────────────────
    if not remaining_ulds:
        within = ref.CG_FORWARD_LIMIT_PERCENT_MAC <= current_cg <= ref.CG_AFT_LIMIT_PERCENT_MAC
        print(f"\nAll ULDs loaded! Final CG: {current_cg:.2f}% MAC")
        print(f"Status: {'WITHIN LIMITS ✓' if within else 'OUTSIDE LIMITS ✗'}")

        output = {
            "status":           "LOADING COMPLETE",
            "final_cg_pct_mac": round(current_cg, 3),
            "cg_within_limits": within,
            "locked_ulds":      locked_ulds,
            "remaining_ulds":   [],
        }
        _save_output(output, scan_count, "COMPLETE")
        return

    # ── feasibility check ──────────────────────────────────────────────────
    print("\nChecking CG feasibility...")
    feasible, feasibility_msg = check_cg_feasibility(
        locked_ulds, remaining_ulds,
        zones_template, empty_aircraft, occupied_zones
    )
    print(feasibility_msg)

    if not feasible:
        output = {
            "status":             "WARNING - CG CANNOT BE BALANCED",
            "feasible":           False,
            "reason":             feasibility_msg,
            "current_cg_pct_mac": round(current_cg, 3),
            "cg_fwd_limit":       ref.CG_FORWARD_LIMIT_PERCENT_MAC,
            "cg_aft_limit":       ref.CG_AFT_LIMIT_PERCENT_MAC,
            "locked_ulds":        locked_ulds,
            "remaining_ulds":     remaining_ulds,
            "recommendation":     "Remove or reposition a locked ULD before continuing.",
        }
        _save_output(output, scan_count, "WARNING")

        print(f"\n{'='*55}")
        print(f"  ⚠  WARNING: CG CANNOT BE BALANCED")
        print(f"  Recommendation: reposition a locked ULD")
        print(f"{'='*55}")
        return

    # ── load trained RL agent ──────────────────────────────────────────────
    print(f"\nLoading trained agent...")
    model = PPO.load(MODEL_PATH)

    # ── set up environment ─────────────────────────────────────────────────
    env = CargoEnv(manifest_dir="data/manifests")
    obs, _ = env.reset()

    env.cargo_items      = remaining_ulds
    env.current_item_idx = 0
    env.zone_weights     = {zone_id: 0.0 for zone_id in env.zone_ids}

    # pre-fill locked zone weights
    for uld in locked_ulds:
        env.zone_weights[uld["actual_zone"]] = uld["weight_kg"]

    env.current_cg = current_cg
    obs = env._get_observation()

    # ── run agent ──────────────────────────────────────────────────────────
    print(f"\nOptimal placement for remaining {len(remaining_ulds)} ULDs:")
    print("-" * 55)

    done         = False
    total_reward = 0
    new_plan     = []

    while not done:
        action, _  = model.predict(obs, deterministic=True)
        zone_id    = env.zone_ids[action]
        item       = env.cargo_items[env.current_item_idx]

        obs, reward, done, _, info = env.step(action)
        total_reward += reward

        new_plan.append({
            "name":               item["name"],
            "weight_kg":          item["weight_kg"],
            "assigned_zone":      zone_id,
            "cg_after_placement": round(info["cg_pct_mac"], 3),
        })

        status_icon = "✓" if info["cg_within_limits"] else "✗"
        print(
            f"  {item['name']:30s} "
            f"({item['weight_kg']:6.1f} kg)  →  "
            f"{zone_id:10s}  |  "
            f"CG: {info['cg_pct_mac']:.2f}% MAC {status_icon}"
        )

    # ── final result ───────────────────────────────────────────────────────
    final_cg = info["cg_pct_mac"]
    within   = info["cg_within_limits"]
    status   = "WITHIN LIMITS" if within else "WARNING - OUTSIDE LIMITS"

    print("-" * 55)
    print(f"Projected final CG : {final_cg:.2f}% MAC")
    print(f"Status             : {status}")

    output = {
        "status":              status,
        "feasible":            True,
        "current_cg_pct_mac":  round(current_cg, 3),
        "projected_final_cg":  round(final_cg, 3),
        "cg_within_limits":    within,
        "cg_fwd_limit":        ref.CG_FORWARD_LIMIT_PERCENT_MAC,
        "cg_aft_limit":        ref.CG_AFT_LIMIT_PERCENT_MAC,
        "cg_target":           ref.CG_TARGET_PERCENT_MAC,
        "locked_ulds": [
            {
                "name":        u["name"],
                "weight_kg":   u["weight_kg"],
                "actual_zone": u["actual_zone"],
                "status":      "LOCKED",
            }
            for u in locked_ulds
        ],
        "optimal_remaining_plan": new_plan,
    }

    _save_output(output, scan_count, "OK")


def _save_output(output, scan_count, tag):
    """Saves the output YAML file with a step number in the filename."""
    fname    = f"step_{scan_count:02d}_{tag}.yaml"
    out_path = os.path.join(OUTPUT_DIR, fname)
    with open(out_path, "w") as f:
        yaml.dump(output, f, default_flow_style=False, sort_keys=False)
    print(f"\nPlan saved → {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # reset for new flight
    if args.reset:
        reset_running_state()
        exit()

    # load running state
    state = load_running_state()

    # if a new RFID scan came in, add it to locked
    if args.uld_id and args.zone and args.weight:
        print(f"\nRFID scan received: {args.uld_id} → {args.zone} ({args.weight} kg)")

        # remove this item from remaining
        state["remaining"] = [
            item for item in state["remaining"]
            if item["name"] != args.uld_id
        ]

        # add to locked
        state["locked"].append({
            "name":        args.uld_id,
            "weight_kg":   args.weight,
            "actual_zone": args.zone,
        })

        state["scan_count"] += 1
        save_running_state(state)

    # load from a full state file if provided
    elif args.state:
        with open(args.state, "r") as f:
            state = yaml.safe_load(f)

    # run recalculation
    recalculate(state)