"""HACS download → restart → test auto-replan → check system log."""
import json
import requests
import time
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. HACS download
print("=== HACS Download ===")
ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()
ws.send(json.dumps({"id": 11, "type": "hacs/repository/download", "repository": "1188173164", "version": "main"}))
r = json.loads(ws.recv())
print(f"  [{'OK' if r.get('success') else 'FAIL'}]")
ws.close()
time.sleep(3)

# 2. Restart HA
print("\n=== Restarting HA ===")
try:
    requests.post(f"{BASE}/api/services/homeassistant/restart", headers=HEADERS, json={}, timeout=5)
except:
    pass

for i in range(20):
    time.sleep(5)
    try:
        r = requests.get(f"{BASE}/api/", headers=HEADERS, timeout=3)
        if r.status_code == 200:
            print(f"  HA up after ~{(i+1)*5}s")
            break
    except:
        pass
time.sleep(10)

# 3. Check system log for "Auto-replan: watching" 
print("\n=== System Log (after restart) ===")
ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()

ws.send(json.dumps({"id": 21, "type": "system_log/list"}))
resp = json.loads(ws.recv())
for entry in resp.get("result", []):
    name = str(entry.get("name", ""))
    message = entry.get("message", "")
    if isinstance(message, list):
        message = " | ".join(str(m) for m in message)
    else:
        message = str(message)
    level = entry.get("level", "")
    if "energy" in name.lower() or "replan" in message.lower() or "watching" in message.lower() or "auto-replan" in message.lower():
        print(f"  [{level}] {name}: {message[:300]}")

# 4. Change slider
print("\n=== Changing slider for auto-replan test ===")
resp = requests.get(f"{BASE}/api/states/input_number.ex90_min_departure_soc", headers=HEADERS)
current = float(resp.json().get("state"))
new_val = current - 5 if current > 50 else current + 5
print(f"  Current: {current}, changing to: {new_val}")

requests.post(
    f"{BASE}/api/services/input_number/set_value",
    headers=HEADERS,
    json={"entity_id": "input_number.ex90_min_departure_soc", "value": new_val},
)

time.sleep(15)

# 5. Check system log for "Setting changed"
print("\n=== System Log (after slider change) ===")
ws.send(json.dumps({"id": 22, "type": "system_log/list"}))
resp = json.loads(ws.recv())
for entry in resp.get("result", []):
    name = str(entry.get("name", ""))
    message = entry.get("message", "")
    if isinstance(message, list):
        message = " | ".join(str(m) for m in message)
    else:
        message = str(message)
    level = entry.get("level", "")
    if "energy" in name.lower() or "replan" in message.lower() or "setting" in message.lower() or "auto-replan" in message.lower() or "watching" in message.lower():
        print(f"  [{level}] {name}: {message[:300]}")

# 6. Check if charger plan was updated
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_ev_charger_plan", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    for c in data.get("attributes", {}).get("ev_chargers", []):
        if c.get("name") == "ex90":
            print(f"\n  ex90 min_departure_soc: {c.get('min_departure_soc')} (expected: {int(new_val)})")

# Restore
print(f"\n=== Restoring to {current} ===")
requests.post(
    f"{BASE}/api/services/input_number/set_value",
    headers=HEADERS,
    json={"entity_id": "input_number.ex90_min_departure_soc", "value": current},
)

ws.close()
print("\n=== Done ===")
