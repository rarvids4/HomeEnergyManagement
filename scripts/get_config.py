"""HACS download → restart → call read_local_config → read debug sensor."""
import json
import requests
import time
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Download from HACS
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

# 3. Call read_local_config
print("\n=== Calling read_local_config ===")
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")
time.sleep(2)

# 4. Read the debug sensor
print("\n=== Debug Config Sensor ===")
resp = requests.get(
    f"{BASE}/api/states/sensor.home_energy_management_debug_config",
    headers=HEADERS,
)
if resp.status_code == 200:
    data = resp.json()
    attrs = data.get("attributes", {})
    config_str = attrs.get("config", "")
    if config_str:
        config = json.loads(config_str)
        print(json.dumps(config, indent=2))
    else:
        print(f"  state: {data.get('state')}")
        print(f"  attrs: {json.dumps(attrs, indent=2)}")
else:
    print(f"  Status: {resp.status_code} - sensor not found")

print("\n=== Done ===")
