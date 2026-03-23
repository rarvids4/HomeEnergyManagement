"""Check system log for auto-replan and test with force_replan."""
import json
import requests
import time
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()

msg_id = 10
def call_ws(payload):
    global msg_id
    msg_id += 1
    payload["id"] = msg_id
    ws.send(json.dumps(payload))
    return json.loads(ws.recv())

# 1. Check system log for auto-replan messages
print("=== System Log ===")
resp = call_ws({"type": "system_log/list"})
entries = resp.get("result", [])
for entry in entries:
    name = str(entry.get("name", ""))
    message = str(entry.get("message", ""))
    if isinstance(entry.get("message"), list):
        message = " | ".join(str(m) for m in entry["message"])
    level = entry.get("level", "")
    ts = entry.get("timestamp", "")
    if "energy" in name.lower() or "replan" in message.lower() or "watching" in message.lower():
        print(f"  [{level}] {name}: {message[:200]}")

# 2. Check if async_track_state_change_event was called by looking at update entity version
print("\n=== Update Entity ===")
resp = requests.get(f"{BASE}/api/states/update.home_energy_management_update", headers=HEADERS)
data = resp.json()
attrs = data.get("attributes", {})
print(f"  installed_version: {attrs.get('installed_version')}")
print(f"  latest_version: {attrs.get('latest_version')}")

# 3. Force replan via service (this always works)
print("\n=== Force Replan ===")
ts_before = None
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
if resp.status_code == 200:
    ts_before = resp.json().get("last_updated")
    print(f"  Before: {ts_before}")

resp = requests.post(
    f"{BASE}/api/services/home_energy_management/force_replan",
    headers=HEADERS,
    json={},
)
print(f"  force_replan status: {resp.status_code}")

time.sleep(10)

resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    ts_after = data.get("last_updated")
    print(f"  After: {ts_after}")
    print(f"  Plan updated? {'YES ✅' if ts_after != ts_before else 'NO ❌'}")

# 4. Now change slider and wait
print("\n=== Changing slider for auto-replan test ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
ts_before2 = resp.json().get("last_updated") if resp.status_code == 200 else "?"
print(f"  Before: {ts_before2}")

resp = requests.get(f"{BASE}/api/states/input_number.ex90_min_departure_soc", headers=HEADERS)
current = float(resp.json().get("state"))

new_val = current - 5 if current > 50 else current + 5
print(f"  Changing ex90_min_departure_soc: {current} → {new_val}")

requests.post(
    f"{BASE}/api/services/input_number/set_value",
    headers=HEADERS,
    json={"entity_id": "input_number.ex90_min_departure_soc", "value": new_val},
)

# Wait and check multiple times
for wait in [5, 10, 15]:
    time.sleep(5)
    resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
    if resp.status_code == 200:
        ts = resp.json().get("last_updated")
        updated = ts != ts_before2
        chargers = resp.json().get("attributes", {}).get("ev_chargers", [])
        dep_soc = None
        for c in chargers:
            if c.get("name") == "ex90":
                dep_soc = c.get("min_departure_soc")
        print(f"  After {wait}s: updated={updated}, ex90_dep_soc={dep_soc}, ts={ts}")

# Restore
print(f"\n=== Restoring to {current} ===")
requests.post(
    f"{BASE}/api/services/input_number/set_value",
    headers=HEADERS,
    json={"entity_id": "input_number.ex90_min_departure_soc", "value": current},
)

ws.close()
print("\n=== Done ===")
