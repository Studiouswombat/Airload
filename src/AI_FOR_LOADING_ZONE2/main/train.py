"""
train.py
========
Trains the RL agent to optimise aircraft cargo loading for CG.

Algorithm: PPO (Proximal Policy Optimization)
Library: Stable-Baselines3

How to run:
-----------
    python train.py

Output:
-------
    models/cargo_cg_agent.zip   <- trained agent
    models/training_log/        <- training progress logs
"""

import os
from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.callbacks import EvalCallback, CheckpointCallback
from AI_FOR_ACTUAL_LOADING.main.env.cargo_env import CargoEnv

# ── settings ──────────────────────────────────────────────────────────────────
MANIFEST_DIR  = "data/manifests"
MODEL_DIR     = "models"
LOG_DIR       = "models/training_log"
TOTAL_STEPS   = 500_000    # increase to 1_000_000 for better results
N_ENVS        = 4          # run 4 parallel environments to speed up training

os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ── create vectorised training environment ────────────────────────────────────
# running multiple envs in parallel speeds up training significantly
env = make_vec_env(
    lambda: CargoEnv(manifest_dir=MANIFEST_DIR),
    n_envs=N_ENVS,
)

# ── create evaluation environment ─────────────────────────────────────────────
eval_env = make_vec_env(
    lambda: CargoEnv(manifest_dir=MANIFEST_DIR),
    n_envs=1,
)

# ── callbacks ─────────────────────────────────────────────────────────────────
# EvalCallback: tests the agent every 10,000 steps and saves the best version
eval_callback = EvalCallback(
    eval_env,
    best_model_save_path=MODEL_DIR,
    log_path=LOG_DIR,
    eval_freq=10_000,
    n_eval_episodes=20,
    deterministic=True,
    verbose=1,
)

# CheckpointCallback: saves the agent every 50,000 steps as a backup
checkpoint_callback = CheckpointCallback(
    save_freq=50_000,
    save_path=MODEL_DIR,
    name_prefix="cargo_cg_checkpoint",
)

# ── create PPO agent ──────────────────────────────────────────────────────────
model = PPO(
    policy="MlpPolicy",    # multi-layer perceptron neural network
    env=env,
    learning_rate=3e-4,    # how fast the agent updates its knowledge
    n_steps=2048,          # steps per environment before each update
    batch_size=64,         # samples per gradient update
    n_epochs=10,           # passes over each batch
    gamma=0.99,            # how much future rewards matter
    verbose=1,             # print training progress
    tensorboard_log=LOG_DIR,
)

# ── train ─────────────────────────────────────────────────────────────────────
print(f"\nStarting training for {TOTAL_STEPS:,} steps...")
print(f"Parallel envs : {N_ENVS}")
print(f"Model will be saved to: {MODEL_DIR}/")
print("-" * 50)

model.learn(
    total_timesteps=TOTAL_STEPS,
    callback=[eval_callback, checkpoint_callback],
    progress_bar=True,
)

# ── save final model ──────────────────────────────────────────────────────────
final_path = os.path.join(MODEL_DIR, "cargo_cg_agent_final")
model.save(final_path)
print(f"\nTraining complete!")
print(f"Final model saved to: {final_path}.zip")