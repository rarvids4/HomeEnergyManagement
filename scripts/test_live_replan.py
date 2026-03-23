"""Test auto-replan: change slider → check charger plan updates live."""
import json
import requests
import time

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Get current state
print("=== BEFORE ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
data = resp.json()
ts_before = data.get("last_updated")
chargers = data.get("attributes", {}).get("ev_chargers", [])
for c in chargers:
    if c.get("name") == "ex90":
        print(f"  ex90 min_departure_soc: {c.get('min_departure_soc')}")

resp = requests.get(f"{BASE}/api/states/input_number.ex90_min_departure_soc", headers=HEADERS)
current_val = float(resp.json().get("state"))
print(f"  Input helper value: {current_val}")
print(f"  Charger plan last_updated: {ts_before}")

# 2. Change the slider
new_val = current_val - 3 if current_val > 50 else current_val + 3
print(f"\n=== Changing ex90_min_departure_soc: {current_val} → {new_val} ===")
resp = requests.post(
    f"{BASE}/api/services/input_number/set_value",
    headers=HEADERS,
    json={
        "entity_id": "input_number.ex90_min_departure_soc",
        "value": new_val,
    },
)
print(f"  set_value status: {resp.status_code}")

# 3. Wait for auto-replan
print("\n=== Waiting 15s for auto-replan... ===")
time.sleep(15)

# 4. Check updated state
print("=== AFTER ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
data = resp.json()
ts_after = data.get("last_updated")
chargers = data.get("attributes", {}).get("ev_chargers", [])
for c in chargers:
    if c.get("name") == "ex90":
        print(f"  ex90 min_departure_soc: {c.get('min_departure_soc')}")

print(f"  Charger plan last_updated: {ts_after}")
print(f"\n  Plan updated? {'YES ✅' if ts_after != ts_before else 'NO ❌'}")

# 5. Restore
print(f"\n=== Restoring to {current_val} ===")
requests.post(
    f"{BASE}/api/services/input_number/set_value",
    headers=HEADERS,
    json={
        "entity_id": "input_number.ex90_min_departure_soc",
        "value": current_val,
    },
)
time.sleep(10)

# 6. Check final state
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
data = resp.json()
for c in data.get("attributes", {}).get("ev_chargers", []):
    if c.get("name") == "ex90":
        print(f"  ex90 min_departure_soc restored: {c.get('min_departure_soc')}")
