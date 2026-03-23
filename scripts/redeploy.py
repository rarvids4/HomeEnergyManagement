"""Re-deploy the local mapping YAML to HA via write_local_config service."""
import json
import requests
import time

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Read the local mapping file
with open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/config/variable_mapping.local.yaml", "r") as f:
    yaml_content = f.read()

print(f"=== Deploying mapping ({len(yaml_content)} bytes) ===")

# 2. Call write_local_config
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/write_local_config",
    headers=HEADERS,
    json={"content": yaml_content},
)
print(f"  write_local_config status: {resp.status_code}")

time.sleep(2)

# 3. Restart HA to pick up the new mapping
print("\n=== Restarting HA ===")
try:
    requests.post(f"{BASE}/api/services/homeassistant/restart", headers=HEADERS, json={}, timeout=5)
except:
    pass

print("  Waiting for HA...")
for i in range(20):
    time.sleep(5)
    try:
        r = requests.get(f"{BASE}/api/", headers=HEADERS, timeout=3)
        if r.status_code == 200:
            print(f"  HA up after ~{(i+1)*5}s")
            break
    except:
        pass

time.sleep(5)

# 4. Verify the charger plan now reads from input helpers
print("\n=== Verifying charger plan ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    attrs = data.get("attributes", {})
    chargers = attrs.get("ev_chargers", [])
    for c in chargers:
        print(f"\n  Vehicle: {c.get('name')}")
        print(f"    min_departure_soc: {c.get('min_departure_soc')}  (should match input helper)")
        print(f"    min_charge_level: {c.get('min_charge_level', 'N/A')}")
        print(f"    departure_time: {c.get('departure_time')}")
        print(f"    vehicle_soc: {c.get('vehicle_soc')}")

# 5. Cross-reference with input helpers
print("\n=== Input helper values ===")
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

print("\n=== Done ===")
