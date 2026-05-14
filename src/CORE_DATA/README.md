# AIRLoad A350-1000 Person 1 Core V2

This package contains the commented Python foundation for the AIRLoad aircraft loading physics and constraint layer.

V2 now includes:
- ULD cargo loading
- passenger cabin zones
- check-in baggage / baggage ULD modelling
- fuel tank modelling
- itinerary / destination priority fields
- total aircraft CG calculation
- cargo-only CG calculation
- constraint validation
- alert generation
- final verification
- JSON export for dashboard / AI recommendation team

Important:
The values used here are simulation assumptions for a competition prototype.
They are not certified Airbus or airline operational loading data.

## Files

- `airload_person1_core_v2.py`
  Main commented Python code.

- `sample_flight_plan.json`
  Example flight plan including ULDs, passengers, baggage, and fuel.

## How to Run

```bash
python airload_person1_core_v2.py
```

## Recommended GitHub Structure

```text
AIRLoad-A350-Core/
├── README.md
├── airload_person1_core_v2.py
└── sample_flight_plan.json
```
