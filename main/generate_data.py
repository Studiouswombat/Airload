import json
import yaml
import random
import os
import sys

# ── import from Person 1 core file ────────────────────────────────────────────
# make sure airload_person1_core_v2.py is in the same folder or on the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from airload_person1_core_v2 import (
    A3501000ReferenceModel,
    AIRLoadPhysicsCoreV2,
    OperatingEmptyAircraft,
    CargoZone,
    ULD,
    build_sample_ulds,
)


# ─────────────────────────────────────────────────────────────────────────────
# AIRCRAFT + ZONES
# Pulled directly from Person 1's reference model — no duplication needed
# ─────────────────────────────────────────────────────────────────────────────

def get_aircraft_and_zones():
    """
    Loads aircraft and zone definitions from Person 1's reference model.
    Returns them in a format our manifest generator can use.
    """
    empty_aircraft = A3501000ReferenceModel.operating_empty_aircraft()
    zones          = A3501000ReferenceModel.cargo_zones()   # dict of CargoZone
    return empty_aircraft, zones


# ─────────────────────────────────────────────────────────────────────────────
# CG CALCULATION
# Uses Person 1's AIRLoadPhysicsCoreV2 engine directly
# ─────────────────────────────────────────────────────────────────────────────

def compute_cg(ulds, empty_aircraft, zones, passenger_zones, fuel_tanks):
    """
    Computes total aircraft CG in % MAC using Person 1's physics engine.

    Inputs:
        ulds            : dict of ULD objects (loaded into zones)
        empty_aircraft  : OperatingEmptyAircraft object
        zones           : dict of CargoZone objects
        passenger_zones : dict of PassengerCabinZone objects
        fuel_tanks      : dict of FuelTank objects

    Returns:
        cg_pct_mac      : total aircraft CG as % MAC (float or None)
        total_weight_kg : total aircraft weight in kg (float)
    """
    engine = AIRLoadPhysicsCoreV2(
        empty_aircraft=empty_aircraft,
        zones=zones,
        ulds=ulds,
        passenger_zones=passenger_zones,
        fuel_tanks=fuel_tanks,
    )

    cg_pct_mac      = engine.calculate_total_aircraft_cg_percent_mac()
    total_weight_kg = engine.total_aircraft_weight()

    return cg_pct_mac, total_weight_kg


# ─────────────────────────────────────────────────────────────────────────────
# CARGO TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────

CARGO_TEMPLATES = [
    {"name": "Engine crate",         "weight_range": (200, 1200)},
    {"name": "Avionics boxes",       "weight_range": (200, 1200)},
    {"name": "Spare tyres",          "weight_range": (200, 1200)},
    {"name": "Ground support equip", "weight_range": (200, 1200)},
    {"name": "General freight",      "weight_range": (200, 1200)},
    {"name": "Mail bags",            "weight_range": (200, 1200)},
    {"name": "Spare parts kit",      "weight_range": (200, 1200)},
    {"name": "Catering supplies",    "weight_range": (200, 1200)},
    {"name": "Medical equipment",    "weight_range": (200, 1200)},
    {"name": "Fuel equipment",       "weight_range": (200, 1200)},
]


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_manifest(empty_aircraft, zones, n_items=None, seed=None):
    """
    Generates one random cargo manifest.

    - Picks between 4 and 9 items (capped to number of zones available)
    - Each item gets a random name and weight
    - No zone assignment here — that is the RL agent's job

    Returns a dict with aircraft info, zone ids, and cargo items.
    """
    if seed is not None:
        random.seed(seed)

    max_items = min(len(zones), 9)
    n_items   = n_items or random.randint(4, max_items)
    items     = []
    seen_names = {}

    for _ in range(n_items):
        template         = random.choice(CARGO_TEMPLATES)
        name             = template["name"]
        seen_names[name] = seen_names.get(name, 0) + 1
        suffix           = f" {seen_names[name]}" if seen_names[name] > 1 else ""
        weight           = round(random.uniform(*template["weight_range"]), 1)

        items.append({
            "name":      name + suffix,
            "weight_kg": weight,
        })

    # summarise zone info for the manifest file
    zone_summary = [
        {
            "zone_id":       z.zone_id,
            "hold":          z.hold,
            "x_position_m":  z.x_position_m,
            "max_weight_kg": z.max_weight_kg,
        }
        for z in zones.values()
    ]

    return {
        "aircraft": {
            "name":                     "Airbus A350-1000",
            "operating_empty_weight_kg": empty_aircraft.operating_empty_weight_kg,
            "empty_cg_x_m":             empty_aircraft.empty_cg_x_m,
            "cg_fwd_limit_pct_mac":     A3501000ReferenceModel.CG_FORWARD_LIMIT_PERCENT_MAC,
            "cg_aft_limit_pct_mac":     A3501000ReferenceModel.CG_AFT_LIMIT_PERCENT_MAC,
            "cg_target_pct_mac":        A3501000ReferenceModel.CG_TARGET_PERCENT_MAC,
            "lemac_x_m":                A3501000ReferenceModel.LEMAC_X_M,
            "mac_length_m":             A3501000ReferenceModel.MAC_LENGTH_M,
        },
        "zones": zone_summary,
        "cargo": items,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DATASET GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_dataset(n_manifests=1000, out_dir="data/manifests"):
    """
    Generates n_manifests random cargo manifests and saves each as a YAML file.
    Also saves a summary.json listing all files.
    """
    empty_aircraft, zones = get_aircraft_and_zones()
    os.makedirs(out_dir, exist_ok=True)

    summary = []

    for i in range(n_manifests):
        manifest = generate_manifest(empty_aircraft, zones, seed=i)

        fname = f"manifest_{i:04d}.yaml"
        fpath = os.path.join(out_dir, fname)

        with open(fpath, "w") as f:
            yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

        summary.append({
            "file":    fname,
            "n_items": len(manifest["cargo"]),
        })

    summary_path = os.path.join(out_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Aircraft  : Airbus A350-1000")
    print(f"Zones     : {len(zones)} ULD positions")
    print(f"CG limits : {A3501000ReferenceModel.CG_FORWARD_LIMIT_PERCENT_MAC}% "
          f"(fwd) — {A3501000ReferenceModel.CG_AFT_LIMIT_PERCENT_MAC}% (aft)")
    print(f"Generated : {n_manifests} manifests → {out_dir}/")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    generate_dataset(n_manifests=1000)