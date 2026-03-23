"""Read deployed mapping details via REST API."""
import json
import requests

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Get EV charger plan sensor
print("=== EV Charger Plan ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
data = resp.json()
attrs = data.get("attributes", {})

chargers = attrs.get("ev_chargers", [])
for c in chargers:
    print(f"\nVehicle: {c.get('name')}")
    print(f"  departure_time: {c.get('departure_time')}")
    print(f"  min_departure_soc: {c.get('min_departure_soc')}")
    print(f"  min_charge_level: {c.get('min_charge_level', 'N/A')}")
    print(f"  vehicle_soc: {c.get('vehicle_soc')}")
    print(f"  connected: {c.get('connected')}")

vehicles = attrs.get("ev_vehicles", [])
print(f"\nev_vehicles: {json.dumps(vehicles, indent=2)}")

schedule = attrs.get("ev_charge_schedule", [])
print(f"\nCharging schedule ({len(schedule)} entries):")
for entry in schedule:
    if entry.get("charging"):
        print(f"  Hour {entry['hour']}: {entry['price']:.4f} SEK — {entry.get('vehicles', {})}")

print(f"\nlast_updated: {data.get('last_updated')}")

# 2. Get optimization status
print("\n=== Optimization Status ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_optimization_status", headers=HEADERS)
data = resp.json()
print(f"state: {data.get('state')}")
print(f"last_updated: {data.get('last_updated')}")
attrs = data.get("attributes", {})
print(f"summary: {attrs.get('summary')}")

# 3. Get HA error log - search for relevant lines
print("\n=== HA Error Log (HEM-related) ===")
resp = requests.get(f"{BASE}/api/error_log", headers=HEADERS)
full_log = resp.text
lines = full_log.split("\n")
print(f"Total log lines: {len(lines)}")
for line in lines:
    ll = line.lower()
    if any(kw in ll for kw in ["auto-replan", "watching", "setting changed", "departure_soc", "charge_level_entity", "optimization_days", "min_departure"]):
        print(f"  {line.strip()[:300]}")

# 4. Check input helper current values
print("\n=== Input Helper Values ===")
for eid in [
    "input_number.ex90_min_departure_soc",
    "input_number.zoe_min_departure_soc",
    "input_number.ex90_min_charge_level",
    "input_number.zoe_min_charge_level",
    "input_number.ev_optimization_days",
]:
    resp = requests.get(f"{BASE}/api/states/{eid}", headers=HEADERS)
    d = resp.json()
    unit = d.get("attributes", {}).get("unit_of_measurement", "?")
    print(f"  {eid}: {d.get('state')} ({unit})")
