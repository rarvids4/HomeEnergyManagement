"""Verify the charger plan is now reading from input helpers."""
import json
import requests
import time

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# Wait a bit for the first data update
time.sleep(3)

# 1. Check charger plan
print("=== EV Charger Plan ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    attrs = data.get("attributes", {})
    chargers = attrs.get("ev_chargers", [])
    for c in chargers:
        print(f"\n  Vehicle: {c.get('name')}")
        print(f"    min_departure_soc: {c.get('min_departure_soc')}")
        print(f"    min_charge_level: {c.get('min_charge_level', 'N/A')}")
        print(f"    departure_time: {c.get('departure_time')}")
        print(f"    vehicle_soc: {c.get('vehicle_soc')}")
        print(f"    connected: {c.get('connected')}")

    vehicles = attrs.get("ev_vehicles", [])
    print(f"\n  ev_vehicles:")
    for v in vehicles:
        print(f"    {v.get('name')}: target_soc={v.get('target_soc')}, soc={v.get('soc')}, "
              f"kwh_needed={v.get('kwh_needed')}, hours_needed={v.get('hours_needed')}, "
              f"departure={v.get('departure_time')}, dep_soc={v.get('min_departure_soc')}")
    
    print(f"\n  last_updated: {data.get('last_updated')}")
else:
    print(f"  Status: {resp.status_code}")

# 2. Cross-ref with input helpers
print("\n=== Input Helper Values ===")
for eid in [
    "input_number.ex90_min_departure_soc",
    "input_number.zoe_min_departure_soc",
    "input_number.ex90_min_charge_level",
    "input_number.zoe_min_charge_level",
]:
    resp = requests.get(f"{BASE}/api/states/{eid}", headers=HEADERS)
    d = resp.json()
    print(f"  {eid}: {d.get('state')}")

# 3. Check optimization status
print("\n=== Optimization Status ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_optimization_status", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    print(f"  state: {data.get('state')}")
    attrs = data.get("attributes", {})
    print(f"  summary: {attrs.get('summary')}")
    print(f"  last_updated: {data.get('last_updated')}")
