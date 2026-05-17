"""
evaluate.py
===========
Loads a trained RL agent and tests it on cargo manifests.
Outputs the optimised loading plan as a YAML file.

How to run:
-----------
    python evaluate.py

Output:
-------
    output/optimised_loading_plan.yaml
"""

import yaml
import os
import numpy as np
from stable_baselines3 import PPO
from env.cargo_env import CargoEnv
from airload_person1_core_v2 import A3501000ReferenceModel

# ── settings ──────────────────────────────────────────────────────────────────
MODEL_PATH   = "models/best_model.zip"    # best model saved during training
MANIFEST_DIR = "data/manifests"
OUTPUT_DIR   = "output"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── load trained agent ─────────────────────────────────────────────────────────
print(f"Loading trained agent from {MODEL_PATH}...")
model = PPO.load(MODEL_PATH)

# ── create environment ─────────────────────────────────────────────────────────
env = CargoEnv(manifest_dir=MANIFEST_DIR)
obs, _ = env.reset()

# ── run one episode with the trained agent ─────────────────────────────────────
print(f"\nRunning optimised loading...")
print(f"Cargo items to load: {len(env.cargo_items)}")
print("-" * 50)

done         = False
total_reward = 0
assignments  = []   # track where each item was placed

while not done:
    # agent picks the best zone deterministically
    action, _ = model.predict(obs, deterministic=True)
    zone_id   = env.zone_ids[action]
    item      = env.cargo_items[env.current_item_idx]

    obs, reward, done, _, info = env.step(action)
    total_reward += reward

    assignments.append({
        "item":    item["name"],
        "weight_kg": item["weight_kg"],
        "zone":    zone_id,
        "cg_after_placement": round(info["cg_pct_mac"], 3),
    })

    print(f"  {item['name']:30s} ({item['weight_kg']:6.1f} kg)  →  {zone_id:10s}  |  CG: {info['cg_pct_mac']:.2f}% MAC")

# ── final summary ──────────────────────────────────────────────────────────────
ref    = A3501000ReferenceModel
status = "WITHIN LIMITS ✓" if info["cg_within_limits"] else "OUTSIDE LIMITS ✗"

print("-" * 50)
print(f"Final CG      : {info['cg_pct_mac']:.2f}% MAC")
print(f"CG limits     : {ref.CG_FORWARD_LIMIT_PERCENT_MAC}% — {ref.CG_AFT_LIMIT_PERCENT_MAC}% MAC")
print(f"CG target     : {ref.CG_TARGET_PERCENT_MAC}% MAC")
print(f"Status        : {status}")
print(f"Total reward  : {total_reward:.2f}")

# ── build output ───────────────────────────────────────────────────────────────
output = {
    "aircraft": "Airbus A350-1000",
    "optimisation_status": status,
    "final_cg_pct_mac": round(info["cg_pct_mac"], 3),
    "cg_fwd_limit": ref.CG_FORWARD_LIMIT_PERCENT_MAC,
    "cg_aft_limit": ref.CG_AFT_LIMIT_PERCENT_MAC,
    "cg_target":    ref.CG_TARGET_PERCENT_MAC,
    "cg_within_limits": info["cg_within_limits"],
    "total_reward": round(total_reward, 2),
    "loading_plan": assignments,
}

# ── save output YAML ───────────────────────────────────────────────────────────
out_path = os.path.join(OUTPUT_DIR, "optimised_loading_plan.yaml")
with open(out_path, "w") as f:
    yaml.dump(output, f, default_flow_style=False, sort_keys=False)

print(f"\nOptimised loading plan saved to: {out_path}")