"""
cargo_env.py
============
Custom Gymnasium environment for aircraft cargo CG optimisation.

How it works:
-------------
- The agent is given a list of cargo items that must all fly
- One by one, the agent places each item into a ULD zone
- After each placement the CG is recalculated using Person 1's
  AIRLoadPhysicsCoreV2 engine (includes empty aircraft + passengers + fuel)
- The agent gets a reward based on how well the CG stays within limits
- Episode ends when all items are placed
"""

import gymnasium as gym
import numpy as np
import yaml
import os
import random
from gymnasium import spaces
from airload_person1_core_v2 import (
    A3501000ReferenceModel,
    AIRLoadPhysicsCoreV2,
    ULD,
)


class CargoEnv(gym.Env):
    """
    Custom RL environment for aircraft cargo loading.

    Observation (what the agent sees):
    ------------------------------------
    - Current weight in each zone (normalised)
    - Current CG as % MAC (normalised)
    - Weight of the next item to place (normalised)
    - How full each zone is (normalised)

    Action (what the agent decides):
    ----------------------------------
    - Which zone to place the current cargo item into
    - One action per zone (9 zones for A350-1000)

    Reward:
    --------
    - Positive reward for keeping CG within limits
    - Bonus reward for CG being close to target (28% MAC)
    - Negative reward (penalty) for CG outside limits
    - Extra penalty for overfilling a zone
    """

    metadata = {"render_modes": []}

    def __init__(self, manifest_dir="data/manifests"):
        super().__init__()

        # ── load aircraft reference model ──────────────────────────────────
        self.ref            = A3501000ReferenceModel
        self.empty_aircraft = self.ref.operating_empty_aircraft()
        self.zones_template = self.ref.cargo_zones()
        self.zone_ids       = list(self.zones_template.keys())
        self.n_zones        = len(self.zone_ids)

        # ── load passengers and fuel once — these dont change ──────────────
        self.passenger_zones = self.ref.passenger_zones()
        self.fuel_tanks      = self.ref.fuel_tanks()

        # ── CG limits from Person 1's reference model ──────────────────────
        self.cg_fwd    = self.ref.CG_FORWARD_LIMIT_PERCENT_MAC   # 20.0
        self.cg_aft    = self.ref.CG_AFT_LIMIT_PERCENT_MAC       # 35.0
        self.cg_target = self.ref.CG_TARGET_PERCENT_MAC           # 28.0

        # ── manifest files ─────────────────────────────────────────────────
        self.manifest_dir   = manifest_dir
        self.manifest_files = [
            os.path.join(manifest_dir, f)
            for f in os.listdir(manifest_dir)
            if f.endswith(".yaml")
        ]

        # ── action space: which zone to place the current item into ────────
        self.action_space = spaces.Discrete(self.n_zones)

        # ── observation space ──────────────────────────────────────────────
        # [zone_weights (n_zones), current_cg, next_item_weight, zone_fullness (n_zones)]
        obs_size = self.n_zones + 1 + 1 + self.n_zones
        self.observation_space = spaces.Box(
            low=-1.0,
            high=2.0,
            shape=(obs_size,),
            dtype=np.float32,
        )

        # ── internal state (reset on each episode) ─────────────────────────
        self.cargo_items      = []
        self.current_item_idx = 0
        self.zone_weights     = {}
        self.current_cg       = None

        # ── compute base CG once (empty aircraft + passengers + fuel) ──────
        # this is the CG before any cargo is loaded
        self.base_cg = self._compute_cg_from_weights({})

        # ── normalisation constants ────────────────────────────────────────
        self.max_item_weight = 1200.0
        self.max_zone_weight = 1200.0
        self.total_cg_range  = self.cg_aft - self.cg_fwd

    # ──────────────────────────────────────────────────────────────────────────
    # RESET
    # Called at the start of every new episode (new manifest)
    # ──────────────────────────────────────────────────────────────────────────

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        # pick a random manifest file
        manifest_path = random.choice(self.manifest_files)
        with open(manifest_path, "r") as f:
            manifest = yaml.safe_load(f)

        # load cargo items for this episode
        self.cargo_items      = manifest["cargo"]
        self.current_item_idx = 0

        # reset zone weights to zero
        self.zone_weights = {zone_id: 0.0 for zone_id in self.zone_ids}

        # initial CG = base CG (empty aircraft + passengers + fuel, no cargo)
        self.current_cg = self.base_cg

        return self._get_observation(), {}

    # ──────────────────────────────────────────────────────────────────────────
    # STEP
    # Called every time the agent makes a decision
    # ──────────────────────────────────────────────────────────────────────────

    def step(self, action):
        zone_id     = self.zone_ids[action]
        zone        = self.zones_template[zone_id]
        item        = self.cargo_items[self.current_item_idx]
        item_weight = item["weight_kg"]

        # ── place item into chosen zone ────────────────────────────────────
        self.zone_weights[zone_id] += item_weight

        # ── check zone overload ────────────────────────────────────────────
        zone_overloaded = self.zone_weights[zone_id] > zone.max_weight_kg

        # ── recalculate CG using Person 1's full physics engine ────────────
        self.current_cg = self._compute_cg_from_weights(self.zone_weights)

        # ── calculate reward ───────────────────────────────────────────────
        reward = self._compute_reward(zone_overloaded)

        # ── advance to next item ───────────────────────────────────────────
        self.current_item_idx += 1
        done = self.current_item_idx >= len(self.cargo_items)

        # ── bonus reward at end of episode if CG is within limits ──────────
        if done and not zone_overloaded:
            if self.cg_fwd <= self.current_cg <= self.cg_aft:
                reward += 10.0
                distance_to_target = abs(self.current_cg - self.cg_target)
                reward += max(0, 5.0 - distance_to_target)

        observation = self._get_observation()
        info = {
            "cg_pct_mac":       self.current_cg,
            "cg_within_limits": self.cg_fwd <= self.current_cg <= self.cg_aft,
            "zone_overloaded":  zone_overloaded,
            "items_placed":     self.current_item_idx,
        }

        return observation, reward, done, False, info

    # ──────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_cg_from_weights(self, zone_weights):
        """
        Calculates total aircraft CG in % MAC using Person 1's
        AIRLoadPhysicsCoreV2 engine.

        Includes:
        - Empty aircraft weight and CG
        - Passenger weight and CG (all cabin zones)
        - Fuel weight and CG (all tanks)
        - Cargo weight in each zone

        zone_weights: dict of {zone_id: cargo_kg}
        """
        # build ULD objects from zone weights
        ulds = {}
        for zone_id, cargo_kg in zone_weights.items():
            if cargo_kg > 0:
                uld_id       = f"CARGO_{zone_id}"
                ulds[uld_id] = ULD(
                    uld_id        = uld_id,
                    weight_kg     = cargo_kg,
                    assigned_zone = zone_id,
                    actual_zone   = zone_id,
                    rfid_status   = "Loaded",
                )

        # build Person 1's physics engine with full aircraft state
        engine = AIRLoadPhysicsCoreV2(
            empty_aircraft  = self.empty_aircraft,
            zones           = self.zones_template,
            ulds            = ulds,
            passenger_zones = self.passenger_zones,
            fuel_tanks      = self.fuel_tanks,
        )

        cg = engine.calculate_total_aircraft_cg_percent_mac()

        # fallback to base CG if engine returns None (no cargo loaded yet)
        if cg is None:
            return self.ref.x_to_percent_mac(self.empty_aircraft.empty_cg_x_m)

        return cg

    def _compute_reward(self, zone_overloaded):
        """
        Reward function — the heart of the RL training signal.

        The agent gets:
        +2.0  for every step where CG is within limits
        -5.0  penalty if CG goes outside limits
        -3.0  penalty if zone is overloaded
        bonus for being close to target CG
        """
        reward = 0.0

        # penalise zone overload
        if zone_overloaded:
            reward -= 3.0

        # reward/penalise based on CG position
        if self.cg_fwd <= self.current_cg <= self.cg_aft:
            reward += 2.0
            distance_to_target = abs(self.current_cg - self.cg_target)
            reward += max(0, 1.0 - distance_to_target * 0.1)
        else:
            reward -= 5.0

        return reward

    def _get_observation(self):
        """
        Builds the observation vector the agent sees at each step.

        Vector layout:
        [zone_weights normalised, current_cg normalised,
         next_item_weight normalised, zone_fullness normalised]
        """
        # normalised zone weights
        zone_weights_norm = np.array(
            [self.zone_weights[z] / self.max_zone_weight for z in self.zone_ids],
            dtype=np.float32,
        )

        # normalised CG (0=forward limit, 1=aft limit)
        cg_norm = np.array(
            [(self.current_cg - self.cg_fwd) / self.total_cg_range],
            dtype=np.float32,
        )

        # normalised next item weight
        if self.current_item_idx < len(self.cargo_items):
            next_weight = self.cargo_items[self.current_item_idx]["weight_kg"]
        else:
            next_weight = 0.0

        next_item_norm = np.array(
            [next_weight / self.max_item_weight],
            dtype=np.float32,
        )

        # normalised zone fullness
        zone_fullness_norm = np.array(
            [
                self.zone_weights[z] / self.zones_template[z].max_weight_kg
                for z in self.zone_ids
            ],
            dtype=np.float32,
        )

        return np.concatenate([
            zone_weights_norm,
            cg_norm,
            next_item_norm,
            zone_fullness_norm,
        ])


# ──────────────────────────────────────────────────────────────────────────────
# QUICK TEST
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    env = CargoEnv(manifest_dir="data/manifests")
    obs, _ = env.reset()

    print(f"Environment created successfully")
    print(f"Zones      : {env.n_zones}")
    print(f"Obs shape  : {obs.shape}")
    print(f"CG limits  : {env.cg_fwd}% — {env.cg_aft}% MAC")
    print(f"CG target  : {env.cg_target}% MAC")
    print(f"Base CG    : {env.base_cg:.2f}% MAC  (empty aircraft + passengers + fuel)")

    # run one random episode
    done         = False
    total_reward = 0

    while not done:
        action = env.action_space.sample()
        obs, reward, done, _, info = env.step(action)
        total_reward += reward

    print(f"\nRandom episode complete:")
    print(f"Total reward : {total_reward:.2f}")
    print(f"Final CG     : {info['cg_pct_mac']:.2f}% MAC")
    print(f"Within limits: {info['cg_within_limits']}")