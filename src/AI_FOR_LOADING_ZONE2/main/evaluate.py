"""
evaluate.py
===========
Loads a trained RL agent and runs it on your own cargo manifest.
Outputs the optimised loading plan as a YAML file.

How to run:
-----------
    python evaluate.py --manifest input_file/my_flight.yaml
"""

import yaml
import os
import argparse
from stable_baselines3 import PPO
from AI_FOR_ACTUAL_LOADING.main.env.cargo_env import CargoEnv
from AI_FOR_ACTUAL_LOADING.main.airload_person1_core_v2 import A3501000ReferenceModel

# ── argument parser ────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument(
    "--manifest",
    type=str,
    required=True,
    help="Path to your cargo manifest YAML file e.g. input_file/my_flight.yaml"
)
args = parser.parse_args()

# ── settings ──────────────────────────────────────────────────────────────────
MODEL_PATH   = "models/best_model.zip"
OUTPUT_DIR   = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

ref = A3501000ReferenceModel

# ── load manifest ──────────────────────────────────────────────────────────────
print(f"\nLoading manifest: {args.manifest}")
with open(args.manifest, "r") as f:
    custom_manifest = yaml.safe_load(f)

cargo_items = custom_manifest["cargo"]
print(f"Cargo items found: {len(cargo_items)}")
for item in cargo_items:
    print(f"  {item['name']:30s} {item['weight_kg']} kg")

# ── load trained agent ─────────────────────────────────────────────────────────
print(f"\nLoading trained agent from {MODEL_PATH}...")
model = PPO.load(MODEL_PATH)

# ── create environment ─────────────────────────────────────────────────────────
env = CargoEnv(manifest_dir="data/manifests")
obs, _ = env.reset()

# ── inject your cargo into the environment ─────────────────────────────────────
env.cargo_items      = cargo_items
env.current_item_idx = 0
env.zone_weights     = {zone_id: 0.0 for zone_id in env.zone_ids}
env.current_cg       = env.base_cg
obs = env._get_observation()

# ── run optimisation ───────────────────────────────────────────────────────────
print(f"\nCargo items to load : {len(env.cargo_items)}")
print(f"Available zones     : {env.n_zones}")
print(f"Base CG             : {env.base_cg:.2f}% MAC (empty + pax + fuel)")
print("-" * 55)

done         = False
total_reward = 0
assignments  = []

while not done:
    action, _ = model.predict(obs, deterministic=True)
    zone_id   = env.zone_ids[action]
    item      = env.cargo_items[env.current_item_idx]

    obs, reward, done, _, info = env.step(action)
    total_reward += reward

    assignments.append({
        "item":               item["name"],
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

# ── final summary ──────────────────────────────────────────────────────────────
final_cg = info["cg_pct_mac"]
within   = info["cg_within_limits"]
status   = "WITHIN LIMITS" if within else "WARNING - OUTSIDE LIMITS"

print("-" * 55)
print(f"Final CG      : {final_cg:.2f}% MAC")
print(f"CG envelope   : {ref.CG_FORWARD_LIMIT_PERCENT_MAC}% (fwd) — {ref.CG_AFT_LIMIT_PERCENT_MAC}% (aft) MAC")
print(f"CG target     : {ref.CG_TARGET_PERCENT_MAC}% MAC")
print(f"Status        : {status}")

# ── save output YAML ───────────────────────────────────────────────────────────
output = {
    "aircraft":         "Airbus A350-1000",
    "input_manifest":   args.manifest,
    "status":           status,
    "cg_within_limits": within,
    "base_cg_pct_mac":  round(env.base_cg, 3),
    "final_cg_pct_mac": round(final_cg, 3),
    "cg_fwd_limit":     ref.CG_FORWARD_LIMIT_PERCENT_MAC,
    "cg_aft_limit":     ref.CG_AFT_LIMIT_PERCENT_MAC,
    "cg_target":        ref.CG_TARGET_PERCENT_MAC,
    "total_reward":     round(total_reward, 2),
    "weight_breakdown": {
        "empty_aircraft_kg": env.empty_aircraft.operating_empty_weight_kg,
        "passengers_kg":     sum(p.total_weight() for p in env.passenger_zones.values()),
        "fuel_kg":           sum(t.fuel_kg for t in env.fuel_tanks.values()),
        "cargo_kg":          sum(item["weight_kg"] for item in cargo_items),
    },
    "loading_plan": assignments,
}

out_path = os.path.join(OUTPUT_DIR, "optimised_loading_plan.yaml")
with open(out_path, "w") as f:
    yaml.dump(output, f, default_flow_style=False, sort_keys=False)

print(f"\nOptimised loading plan saved → {out_path}")