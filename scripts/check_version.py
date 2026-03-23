"""Check installed version and wait longer after restart."""
import json
import requests
import time

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Check version
print("=== Current Version ===")
resp = requests.get(f"{BASE}/api/states/update.home_energy_management_update", headers=HEADERS)
data = resp.json()
attrs = data.get("attributes", {})
print(f"  installed: {attrs.get('installed_version')}")
print(f"  latest: {attrs.get('latest_version')}")

# 2. Check ALL system_log entries (no filter)
import websocket
ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()

ws.send(json.dumps({"id": 11, "type": "system_log/list"}))
resp = json.loads(ws.recv())
entries = resp.get("result", [])
print(f"\n=== ALL System Log ({len(entries)} entries) ===")
for entry in entries:
    name = str(entry.get("name", ""))
    message = entry.get("message", "")
    if isinstance(message, list):
        message = " | ".join(str(m) for m in message)
    else:
        message = str(message)
    level = entry.get("level", "")
    count = entry.get("count", 1)
    print(f"  [{level}] {name}: {message[:200]} (count={count})")

# 3. Check if integration is loaded properly
print("\n=== Integration Status ===")
resp = requests.get(f"{BASE}/api/states/sensor.home_energy_management_optimization_status", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    print(f"  state: {data.get('state')}")
    print(f"  last_updated: {data.get('last_updated')}")
    print(f"  summary: {data.get('attributes', {}).get('summary', '?')[:200]}")

ws.close()
