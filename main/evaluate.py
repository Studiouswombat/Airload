"""
evaluate.py
===========
Loads a trained RL agent and runs it on your own cargo manifest.
Outputs the optimised loading plan as a YAML file.

How to run:
-----------
    # use your own manifest
    python evaluate.py --manifest input/my_flight.yaml

    # or use a random training manifest
    python evaluate.py
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
parser.add_argument(
    "--manifest",
    type=str,
    default=None,
    help="Path to your own cargo manifest YAML file (optional)"
)
args = parser.parse_args()

# ── settings ──────────────────────────────────────────────────────────────────
MODEL_PATH   = "models/best_model.zip"
MANIFEST_DIR = "data/manifests"
OUTPUT_DIR   = "output"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── load trained agent ─────────────────────────────────────────────────────────
print(f"Loading trained agent from {MODEL_PATH}...")
model = PPO.load(MODEL_PATH)

# ── create environment ─────────────────────────────────────────────────────────
env = CargoEnv(manifest_dir=MANIFEST_DIR)

# ── load manifest ──────────────────────────────────────────────────────────────
if args.manifest:
    # use your own manifest file
    print(f"Using your manifest: {args.manifest}")
    with open(args.manifest, "r") as f:
        custom_manifest = yaml.safe_load(f)

    # inject your cargo into the environment
    obs, _ = env.reset()
    env.cargo_items      = custom_manifest["cargo"]
    env.current_item_idx = 0
    env.zone_weights     = {zone_id: 0.0 for zone_id in env.zone_ids}
    env.current_cg       = A3501000ReferenceModel.x_to_percent_mac(
        env.empty_aircraft.empty_cg_x_m
    )
    obs = env._get_observation()
else:
    # use a random manifest from training data
    print("No manifest provided — using a random training manifest")
    obs, _ = env.reset()

# ── run optimisation ───────────────────────────────────────────────────────────
print(f"\nCargo items to load : {len(env.cargo_items)}")
print(f"Available zones     : {env.n_zones}")
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
ref    = A3501000ReferenceModel
within = info["cg_within_limits"]
status = "WITHIN LIMITS" if within else "OUTSIDE LIMITS"

print("-" * 55)
print(f"Final CG      : {info['cg_pct_mac']:.2f}% MAC")
print(f"CG envelope   : {ref.CG_FORWARD_LIMIT_PERCENT_MAC}% (fwd) — {ref.CG_AFT_LIMIT_PERCENT_MAC}% (aft) MAC")
print(f"CG target     : {ref.CG_TARGET_PERCENT_MAC}% MAC")
print(f"Status        : {status}")

# ── save output YAML ───────────────────────────────────────────────────────────
output = {
    "aircraft":           "Airbus A350-1000",
    "status":             status,
    "cg_within_limits":   within,
    "final_cg_pct_mac":   round(info["cg_pct_mac"], 3),
    "cg_fwd_limit":       ref.CG_FORWARD_LIMIT_PERCENT_MAC,
    "cg_aft_limit":       ref.CG_AFT_LIMIT_PERCENT_MAC,
    "cg_target":          ref.CG_TARGET_PERCENT_MAC,
    "total_reward":       round(total_reward, 2),
    "loading_plan":       assignments,
}

out_path = os.path.join(OUTPUT_DIR, "optimised_loading_plan.yaml")
with open(out_path, "w") as f:
    yaml.dump(output, f, default_flow_style=False, sort_keys=False)

print(f"\nOptimised loading plan saved → {out_path}")