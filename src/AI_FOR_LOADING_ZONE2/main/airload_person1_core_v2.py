
"""
AIRLoad A350-1000 Person 1 Core V2
==================================

Purpose of this file
--------------------
This file is the "Person 1" contribution for the AIRLoad project.

Person 1 is responsible for the aircraft loading physics, calculation rules,
constraint dictionary, and data structures that the rest of AIRLoad depends on.

V2 extends the first version by including:

1. ULD cargo loading.
2. Passenger cabin zone loading.
3. Check-in baggage / baggage ULD modelling.
4. Fuel tank modelling.
5. Itinerary / destination priority information.
6. Total aircraft center-of-gravity calculation.
7. Cargo-only center-of-gravity calculation.
8. Alerts and final verification logic.

Important note
--------------
The aircraft reference is the Airbus A350-1000.

However, the cargo positions, passenger-zone positions, fuel-tank positions,
weight limits, operating empty weight, and CG limits in this file are simulation
assumptions for the AIRLoad competition prototype.

They are NOT official Airbus certified operational values.

The goal is to build a realistic prototype logic layer so the dashboard and
recommendation team can simulate AIRLoad from A to Z.
"""

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
import json


# ============================================================
# 1. DATA MODELS
# ============================================================

@dataclass
class CargoZone:
    """
    Represents one ULD loading position inside the aircraft cargo hold.

    Why this exists:
    ----------------
    AIRLoad needs a mathematical representation of cargo bay slots.
    Each slot has:
    - an ID
    - a hold name
    - a longitudinal position
    - a maximum allowed weight
    - an occupancy state

    This allows the digital twin, CG engine, and optimization engine to talk
    about cargo placement using the same shared structure.
    """

    zone_id: str
    hold: str
    x_position_m: float
    max_weight_kg: float
    occupied_by: Optional[str] = None


@dataclass
class PassengerCabinZone:
    """
    Represents one passenger cabin section.

    Why this exists:
    ----------------
    Passengers affect aircraft weight and balance.
    Instead of modelling every passenger seat individually, the prototype groups
    passengers into cabin zones such as Forward Cabin, Mid Cabin, and Aft Cabin.

    passenger_count:
        Number of passengers in this cabin zone.

    average_passenger_weight_kg:
        Simulation assumption for average passenger mass including carry-on allowance.

    x_position_m:
        Longitudinal position of the cabin zone.
    """

    zone_id: str
    cabin_section: str
    x_position_m: float
    passenger_count: int
    average_passenger_weight_kg: float = 85.0

    def total_weight(self) -> float:
        """
        Calculates total passenger weight in this cabin zone.
        """
        return self.passenger_count * self.average_passenger_weight_kg


@dataclass
class FuelTank:
    """
    Represents one fuel tank or fuel group.

    Why this exists:
    ----------------
    Fuel is one of the largest variable weight components on an aircraft.
    It significantly affects total aircraft weight and CG.

    For this prototype, fuel tanks are simplified into:
    - left wing tank
    - right wing tank
    - center tank

    fuel_kg:
        Fuel mass in this tank.

    x_position_m:
        Longitudinal position of the tank's effective fuel CG.
    """

    tank_id: str
    x_position_m: float
    fuel_kg: float
    max_fuel_kg: float


@dataclass
class ULD:
    """
    Represents one Unit Load Device.

    Why this exists:
    ----------------
    AIRLoad tracks aircraft cargo at the ULD level, not individual baggage level.
    ULDs may contain cargo, baggage, or mixed contents.

    assigned_zone:
        The planned loading zone from the flight/load plan.

    actual_zone:
        The zone where AIRLoad currently believes the ULD has been loaded.

    contents_type:
        Examples:
        - "Cargo"
        - "Baggage"
        - "Mixed"
        - "Dangerous Goods"

    destination:
        Used for itinerary/loading priority logic.
        Example:
        A ULD meant for the first stop may need easier access or special ordering.
    """

    uld_id: str
    weight_kg: float
    assigned_zone: str
    contents_type: str = "Cargo"
    priority: str = "Normal"
    destination: str = "SIN"
    itinerary_priority: int = 2
    rfid_status: str = "Pending"
    actual_zone: Optional[str] = None


@dataclass
class OperatingEmptyAircraft:
    """
    Represents the base aircraft weight before payload and fuel.

    Why this exists:
    ----------------
    A realistic aircraft CG calculation should include the aircraft's own weight,
    not just cargo.

    operating_empty_weight_kg:
        Aircraft empty operating weight for simulation.

    empty_cg_x_m:
        Assumed CG position of the empty aircraft.

    Important:
    ----------
    These are prototype assumptions, not certified Airbus values.
    """

    operating_empty_weight_kg: float
    empty_cg_x_m: float


# ============================================================
# 2. AIRBUS A350-1000 REFERENCE MODEL
# ============================================================

class A3501000ReferenceModel:
    """
    Stores all simplified aircraft reference assumptions for the A350-1000 prototype.

    Why this class exists:
    ----------------------
    We keep all aircraft-specific assumptions in one place so that:
    - the physics engine is easier to maintain
    - the team can update values later
    - the optimizer and dashboard use the same shared definitions
    """

    # --------------------------------------------------------
    # Simulated CG safety envelope
    # --------------------------------------------------------
    # These values are prototype assumptions only.
    # Real aircraft CG limits depend on certified aircraft manuals,
    # aircraft configuration, loading condition, and airline procedures.
    CG_FORWARD_LIMIT_PERCENT_MAC = 26.0
    CG_TARGET_PERCENT_MAC = 30.0
    CG_AFT_LIMIT_PERCENT_MAC = 35.0

    # --------------------------------------------------------
    # Simplified %MAC conversion assumptions
    # --------------------------------------------------------
    # %MAC = ((CG_x - LEMAC_X) / MAC_LENGTH) * 100
    #
    # LEMAC means Leading Edge of Mean Aerodynamic Chord.
    # These values are simplified for prototype visualisation.
    LEMAC_X_M = 25.0
    MAC_LENGTH_M = 10.0

    @staticmethod
    def operating_empty_aircraft() -> OperatingEmptyAircraft:
        """
        Defines the simulated empty aircraft weight and CG.

        Why this matters:
        -----------------
        Total aircraft CG should include:
        empty aircraft + passengers + fuel + baggage + cargo.

        This makes the AIRLoad model more realistic than using cargo alone.
        """

        return OperatingEmptyAircraft(
            operating_empty_weight_kg=155000.0,
            empty_cg_x_m=27.5
        )

    @staticmethod
    def cargo_zones() -> Dict[str, CargoZone]:
        """
        Defines simplified A350-1000 lower-deck cargo zones.

        FWD zones:
            Forward cargo hold.

        AFT zones:
            Aft cargo hold.

        BULK zone:
            Smaller rear bulk hold.

        The x_position_m values are used for CG moment calculations.
        """

        return {
            "FWD_01": CargoZone("FWD_01", "Forward Hold", 15.0, 1200),
            "FWD_02": CargoZone("FWD_02", "Forward Hold", 17.0, 1200),
            "FWD_03": CargoZone("FWD_03", "Forward Hold", 19.0, 1200),
            "FWD_04": CargoZone("FWD_04", "Forward Hold", 21.0, 1200),

            "AFT_01": CargoZone("AFT_01", "Aft Hold", 31.0, 1200),
            "AFT_02": CargoZone("AFT_02", "Aft Hold", 33.0, 1200),
            "AFT_03": CargoZone("AFT_03", "Aft Hold", 35.0, 1200),
            "AFT_04": CargoZone("AFT_04", "Aft Hold", 37.0, 1200),

            "BULK_01": CargoZone("BULK_01", "Bulk Hold", 40.0, 600),
        }

    @staticmethod
    def passenger_zones() -> Dict[str, PassengerCabinZone]:
        """
        Defines simplified passenger cabin zones.

        Why passenger zones are grouped:
        -------------------------------
        For this prototype, we do not need seat-by-seat passenger modelling.
        Grouping passengers by cabin section is sufficient to show how
        passengers affect total CG.
        """

        return {
            "PAX_FWD": PassengerCabinZone("PAX_FWD", "Forward Cabin", 18.0, 90),
            "PAX_MID": PassengerCabinZone("PAX_MID", "Mid Cabin", 27.0, 140),
            "PAX_AFT": PassengerCabinZone("PAX_AFT", "Aft Cabin", 36.0, 80),
        }

    @staticmethod
    def fuel_tanks() -> Dict[str, FuelTank]:
        """
        Defines simplified fuel tank groups.

        Why this exists:
        ----------------
        Fuel weight changes total aircraft CG.
        The A350 has complex real fuel tank geometry, but for the prototype
        we group fuel into simplified left, right, and center tanks.
        """

        return {
            "LEFT_WING": FuelTank("LEFT_WING", 27.0, 25000.0, 35000.0),
            "RIGHT_WING": FuelTank("RIGHT_WING", 27.0, 25000.0, 35000.0),
            "CENTER": FuelTank("CENTER", 26.0, 15000.0, 25000.0),
        }

    @classmethod
    def x_to_percent_mac(cls, x_cg_m: float) -> float:
        """
        Converts CG position from metres into %MAC.

        Formula:
        --------
            %MAC = ((CG_x - LEMAC_X_M) / MAC_LENGTH_M) * 100
        """

        return ((x_cg_m - cls.LEMAC_X_M) / cls.MAC_LENGTH_M) * 100


# ============================================================
# 3. AIRLOAD PHYSICS CORE V2
# ============================================================

class AIRLoadPhysicsCoreV2:
    """
    Main AIRLoad Person 1 engine.

    This class handles:
    - RFID scan events
    - ULD loading state
    - passenger weight contribution
    - fuel weight contribution
    - operating empty aircraft contribution
    - cargo-only CG
    - total aircraft CG
    - constraint validation
    - alert generation
    - digital twin state export
    """

    def __init__(
        self,
        empty_aircraft: OperatingEmptyAircraft,
        zones: Dict[str, CargoZone],
        ulds: Dict[str, ULD],
        passenger_zones: Dict[str, PassengerCabinZone],
        fuel_tanks: Dict[str, FuelTank],
    ):
        """
        Initializes the full AIRLoad physics core.

        Inputs:
        -------
        empty_aircraft:
            Base aircraft weight and CG.

        zones:
            Cargo bay zone dictionary.

        ulds:
            Planned ULD manifest.

        passenger_zones:
            Passenger cabin loading data.

        fuel_tanks:
            Fuel tank loading data.
        """

        self.empty_aircraft = empty_aircraft
        self.zones = zones
        self.ulds = ulds
        self.passenger_zones = passenger_zones
        self.fuel_tanks = fuel_tanks

    # --------------------------------------------------------
    # RFID / ULD loading event logic
    # --------------------------------------------------------

    def scan_uld(self, uld_id: str) -> None:
        """
        Simulates RFID detection of an arriving ULD.

        If the ULD is not in the flight manifest, AIRLoad raises an error.
        """

        if uld_id not in self.ulds:
            raise ValueError(f"Unknown ULD scanned: {uld_id}")

        self.ulds[uld_id].rfid_status = "Scanned"

    def load_uld_to_zone(self, uld_id: str, zone_id: str) -> None:
        """
        Updates the digital twin when a ULD is loaded into a zone.

        This represents the system state after RFID detection and loading event
        confirmation from the simulated cargo loading workflow.
        """

        if uld_id not in self.ulds:
            raise ValueError(f"Unknown ULD: {uld_id}")

        if zone_id not in self.zones:
            raise ValueError(f"Unknown cargo zone: {zone_id}")

        if self.zones[zone_id].occupied_by is not None:
            raise ValueError(
                f"Zone {zone_id} is already occupied by {self.zones[zone_id].occupied_by}"
            )

        self.ulds[uld_id].actual_zone = zone_id
        self.ulds[uld_id].rfid_status = "Loaded"
        self.zones[zone_id].occupied_by = uld_id

    # --------------------------------------------------------
    # Weight contribution helper functions
    # --------------------------------------------------------

    def get_loaded_ulds(self) -> List[ULD]:
        """
        Returns all ULDs that are currently loaded in the digital twin.
        """

        return [uld for uld in self.ulds.values() if uld.actual_zone is not None]

    def cargo_loaded_weight(self) -> float:
        """
        Total weight of loaded cargo/baggage ULDs only.
        """

        return sum(uld.weight_kg for uld in self.get_loaded_ulds())

    def passenger_weight(self) -> float:
        """
        Total passenger weight across all cabin zones.
        """

        return sum(zone.total_weight() for zone in self.passenger_zones.values())

    def fuel_weight(self) -> float:
        """
        Total fuel weight across all simplified fuel tanks.
        """

        return sum(tank.fuel_kg for tank in self.fuel_tanks.values())

    def total_aircraft_weight(self) -> float:
        """
        Full aircraft weight used in total CG calculation.

        Includes:
        - operating empty aircraft
        - passengers
        - fuel
        - loaded ULDs
        """

        return (
            self.empty_aircraft.operating_empty_weight_kg
            + self.passenger_weight()
            + self.fuel_weight()
            + self.cargo_loaded_weight()
        )

    # --------------------------------------------------------
    # CG calculation functions
    # --------------------------------------------------------

    def calculate_cargo_only_cg_x_m(self) -> Optional[float]:
        """
        Calculates CG using only loaded ULDs.

        Why this is useful:
        -------------------
        The cargo team may want to see how cargo placement alone is behaving.
        The optimizer can use this to recommend cargo rearrangement.
        """

        loaded_ulds = self.get_loaded_ulds()
        total_cargo_weight = self.cargo_loaded_weight()

        if total_cargo_weight == 0:
            return None

        total_moment = 0.0

        for uld in loaded_ulds:
            zone = self.zones[uld.actual_zone]
            total_moment += uld.weight_kg * zone.x_position_m

        return total_moment / total_cargo_weight

    def calculate_total_aircraft_cg_x_m(self) -> Optional[float]:
        """
        Calculates total aircraft CG including all major weight groups.

        Formula:
        --------
        CG = total moment / total weight

        total moment includes:
        - empty aircraft moment
        - passenger moment
        - fuel moment
        - loaded ULD moment
        """

        total_weight = self.total_aircraft_weight()

        if total_weight == 0:
            return None

        total_moment = 0.0

        # Empty aircraft contribution.
        total_moment += (
            self.empty_aircraft.operating_empty_weight_kg
            * self.empty_aircraft.empty_cg_x_m
        )

        # Passenger contribution.
        for pax_zone in self.passenger_zones.values():
            total_moment += pax_zone.total_weight() * pax_zone.x_position_m

        # Fuel contribution.
        for tank in self.fuel_tanks.values():
            total_moment += tank.fuel_kg * tank.x_position_m

        # Loaded ULD contribution.
        for uld in self.get_loaded_ulds():
            zone = self.zones[uld.actual_zone]
            total_moment += uld.weight_kg * zone.x_position_m

        return total_moment / total_weight

    def calculate_total_aircraft_cg_percent_mac(self) -> Optional[float]:
        """
        Converts total aircraft CG from metres into %MAC.
        """

        cg_x = self.calculate_total_aircraft_cg_x_m()

        if cg_x is None:
            return None

        return A3501000ReferenceModel.x_to_percent_mac(cg_x)

    def calculate_cargo_only_cg_percent_mac(self) -> Optional[float]:
        """
        Converts cargo-only CG from metres into %MAC.
        """

        cg_x = self.calculate_cargo_only_cg_x_m()

        if cg_x is None:
            return None

        return A3501000ReferenceModel.x_to_percent_mac(cg_x)

    # --------------------------------------------------------
    # Constraint and validation checks
    # --------------------------------------------------------

    def check_zone_weight_limits(self) -> List[str]:
        """
        Checks if any loaded ULD exceeds its zone's maximum allowed weight.
        """

        alerts = []

        for zone in self.zones.values():
            if zone.occupied_by is not None:
                uld = self.ulds[zone.occupied_by]

                if uld.weight_kg > zone.max_weight_kg:
                    alerts.append(
                        f"CRITICAL: {uld.uld_id} exceeds max weight for {zone.zone_id}. "
                        f"ULD weight={uld.weight_kg}kg, limit={zone.max_weight_kg}kg."
                    )

        return alerts

    def check_fuel_limits(self) -> List[str]:
        """
        Checks whether any fuel tank exceeds the simulated maximum fuel capacity.
        """

        alerts = []

        for tank in self.fuel_tanks.values():
            if tank.fuel_kg > tank.max_fuel_kg:
                alerts.append(
                    f"CRITICAL: {tank.tank_id} exceeds max fuel capacity. "
                    f"Fuel={tank.fuel_kg}kg, limit={tank.max_fuel_kg}kg."
                )

        return alerts

    def check_assignment_mismatches(self) -> List[str]:
        """
        Checks whether each loaded ULD is placed in its assigned zone.
        """

        alerts = []

        for uld in self.get_loaded_ulds():
            if uld.actual_zone != uld.assigned_zone:
                alerts.append(
                    f"WARNING: {uld.uld_id} loaded in {uld.actual_zone}, "
                    f"but assigned to {uld.assigned_zone}."
                )

        return alerts

    def check_missing_ulds(self) -> List[str]:
        """
        Checks whether any planned ULD has not yet reached Loaded status.
        """

        alerts = []

        for uld in self.ulds.values():
            if uld.rfid_status != "Loaded":
                alerts.append(
                    f"INFO: {uld.uld_id} not fully loaded yet. Current status: {uld.rfid_status}."
                )

        return alerts

    def check_total_cg_limits(self) -> List[str]:
        """
        Checks total aircraft CG against the simulated safe CG envelope.

        This is more realistic than cargo-only CG because it includes:
        - empty aircraft
        - passengers
        - fuel
        - loaded ULDs
        """

        alerts = []
        cg_mac = self.calculate_total_aircraft_cg_percent_mac()

        if cg_mac is None:
            alerts.append("INFO: Total aircraft CG unavailable.")
            return alerts

        if cg_mac < A3501000ReferenceModel.CG_FORWARD_LIMIT_PERCENT_MAC:
            alerts.append(
                f"CRITICAL: Total aircraft CG too far forward. Current CG={cg_mac:.2f}% MAC."
            )

        elif cg_mac > A3501000ReferenceModel.CG_AFT_LIMIT_PERCENT_MAC:
            alerts.append(
                f"CRITICAL: Total aircraft CG too far aft. Current CG={cg_mac:.2f}% MAC."
            )

        elif abs(cg_mac - A3501000ReferenceModel.CG_TARGET_PERCENT_MAC) <= 2.0:
            alerts.append(
                f"OK: Total aircraft CG near target. Current CG={cg_mac:.2f}% MAC."
            )

        else:
            alerts.append(
                f"WARNING: Total aircraft CG safe but away from target. Current CG={cg_mac:.2f}% MAC."
            )

        return alerts

    def generate_all_alerts(self) -> List[str]:
        """
        Runs every validation check and returns one combined alert list.

        This is the function the dashboard team can call directly.
        """

        alerts = []
        alerts.extend(self.check_zone_weight_limits())
        alerts.extend(self.check_fuel_limits())
        alerts.extend(self.check_assignment_mismatches())
        alerts.extend(self.check_missing_ulds())
        alerts.extend(self.check_total_cg_limits())

        return alerts

    def final_verification_status(self) -> Tuple[bool, List[str]]:
        """
        Final departure-readiness check for the simulation.

        AIRLoad considers the aircraft ready if:
        1. All planned ULDs are loaded.
        2. There are no WARNING or CRITICAL alerts.
        """

        alerts = self.generate_all_alerts()

        has_blocking_alert = any(
            alert.startswith("CRITICAL") or alert.startswith("WARNING")
            for alert in alerts
        )

        all_loaded = all(uld.rfid_status == "Loaded" for uld in self.ulds.values())

        ready = all_loaded and not has_blocking_alert

        return ready, alerts

    # --------------------------------------------------------
    # Export logic for dashboard / optimization / AI team
    # --------------------------------------------------------

    def export_state(self) -> dict:
        """
        Exports the current AIRLoad digital twin state.

        The dashboard and recommendation engine can use this JSON-like structure.

        It includes:
        - total aircraft weight
        - cargo-only CG
        - total aircraft CG
        - passenger zones
        - fuel tanks
        - ULD states
        - cargo zones
        - alerts
        """

        return {
            "aircraft_reference": "Airbus A350-1000",
            "note": "Simulation data only. Not certified aircraft operational data.",

            "weights": {
                "operating_empty_weight_kg": self.empty_aircraft.operating_empty_weight_kg,
                "passenger_weight_kg": self.passenger_weight(),
                "fuel_weight_kg": self.fuel_weight(),
                "cargo_loaded_weight_kg": self.cargo_loaded_weight(),
                "total_aircraft_weight_kg": self.total_aircraft_weight(),
            },

            "cg": {
                "cargo_only_cg_x_m": self.calculate_cargo_only_cg_x_m(),
                "cargo_only_cg_percent_mac": self.calculate_cargo_only_cg_percent_mac(),
                "total_aircraft_cg_x_m": self.calculate_total_aircraft_cg_x_m(),
                "total_aircraft_cg_percent_mac": self.calculate_total_aircraft_cg_percent_mac(),
                "target_cg_percent_mac": A3501000ReferenceModel.CG_TARGET_PERCENT_MAC,
                "forward_limit_percent_mac": A3501000ReferenceModel.CG_FORWARD_LIMIT_PERCENT_MAC,
                "aft_limit_percent_mac": A3501000ReferenceModel.CG_AFT_LIMIT_PERCENT_MAC,
            },

            "zones": {
                zone_id: asdict(zone)
                for zone_id, zone in self.zones.items()
            },

            "ulds": {
                uld_id: asdict(uld)
                for uld_id, uld in self.ulds.items()
            },

            "passenger_zones": {
                zone_id: asdict(zone)
                for zone_id, zone in self.passenger_zones.items()
            },

            "fuel_tanks": {
                tank_id: asdict(tank)
                for tank_id, tank in self.fuel_tanks.items()
            },

            "alerts": self.generate_all_alerts(),
        }


# ============================================================
# 4. SAMPLE DATA BUILDERS
# ============================================================

def build_sample_ulds() -> Dict[str, ULD]:
    """
    Creates a sample ULD manifest.

    The manifest includes both cargo and baggage ULDs.

    itinerary_priority:
        1 = highest priority / first unload
        2 = normal priority
        3 = lower priority
    """

    return {
        "AKE2045": ULD(
            uld_id="AKE2045",
            weight_kg=850,
            assigned_zone="FWD_02",
            contents_type="Cargo",
            priority="Normal",
            destination="SIN",
            itinerary_priority=2,
        ),

        "AKE2190": ULD(
            uld_id="AKE2190",
            weight_kg=920,
            assigned_zone="FWD_03",
            contents_type="Baggage",
            priority="Normal",
            destination="SIN",
            itinerary_priority=2,
        ),

        "AKE2311": ULD(
            uld_id="AKE2311",
            weight_kg=1100,
            assigned_zone="AFT_01",
            contents_type="Cargo",
            priority="High",
            destination="SIN",
            itinerary_priority=1,
        ),

        "AKE2442": ULD(
            uld_id="AKE2442",
            weight_kg=780,
            assigned_zone="AFT_02",
            contents_type="Baggage",
            priority="Normal",
            destination="SIN",
            itinerary_priority=2,
        ),

        "AKE2555": ULD(
            uld_id="AKE2555",
            weight_kg=500,
            assigned_zone="BULK_01",
            contents_type="Baggage",
            priority="Low",
            destination="SIN",
            itinerary_priority=3,
        ),
    }


# ============================================================
# 5. DEMO RUN
# ============================================================

def demo() -> None:
    """
    Runs an end-to-end AIRLoad simulation.

    What this demo shows:
    ---------------------
    1. A350-1000 reference model initialized.
    2. Passenger loads included.
    3. Fuel loads included.
    4. ULDs scanned using simulated RFID.
    5. ULDs loaded into cargo zones.
    6. Digital twin updates after each loading event.
    7. Cargo-only CG and total aircraft CG are recalculated.
    8. Alerts are generated.
    9. Final verification is performed.
    10. Final state is exported to JSON.
    """

    empty_aircraft = A3501000ReferenceModel.operating_empty_aircraft()
    zones = A3501000ReferenceModel.cargo_zones()
    passenger_zones = A3501000ReferenceModel.passenger_zones()
    fuel_tanks = A3501000ReferenceModel.fuel_tanks()
    ulds = build_sample_ulds()

    airload = AIRLoadPhysicsCoreV2(
        empty_aircraft=empty_aircraft,
        zones=zones,
        ulds=ulds,
        passenger_zones=passenger_zones,
        fuel_tanks=fuel_tanks,
    )

    print("\n--- AIRLoad A350-1000 Person 1 Core V2 Demo ---\n")

    print("Initial aircraft loading model:")
    print(f"Operating empty weight: {empty_aircraft.operating_empty_weight_kg:.1f} kg")
    print(f"Passenger weight: {airload.passenger_weight():.1f} kg")
    print(f"Fuel weight: {airload.fuel_weight():.1f} kg")
    print("-" * 60)

    event_sequence = [
        ("AKE2045", "FWD_02"),
        ("AKE2190", "FWD_03"),
        ("AKE2311", "AFT_01"),
        ("AKE2442", "AFT_02"),
        ("AKE2555", "BULK_01"),
    ]

    for uld_id, zone_id in event_sequence:
        airload.scan_uld(uld_id)
        airload.load_uld_to_zone(uld_id, zone_id)

        print(f"Loaded {uld_id} into {zone_id}")
        print(f"Cargo loaded weight: {airload.cargo_loaded_weight():.1f} kg")
        print(f"Total aircraft weight: {airload.total_aircraft_weight():.1f} kg")

        cargo_cg = airload.calculate_cargo_only_cg_percent_mac()
        total_cg = airload.calculate_total_aircraft_cg_percent_mac()

        if cargo_cg is not None:
            print(f"Cargo-only CG: {cargo_cg:.2f}% MAC")

        if total_cg is not None:
            print(f"Total aircraft CG: {total_cg:.2f}% MAC")

        print("Current alerts:")
        for alert in airload.generate_all_alerts():
            print(f" - {alert}")

        print("-" * 60)

    ready, final_alerts = airload.final_verification_status()

    print("\n--- Final Verification ---")
    print(f"Flight ready: {ready}")

    for alert in final_alerts:
        print(f" - {alert}")

    with open("airload_final_state_v2.json", "w") as file:
        json.dump(airload.export_state(), file, indent=2)

    print("\nFinal digital twin state exported to airload_final_state_v2.json")


if __name__ == "__main__":
    demo()
