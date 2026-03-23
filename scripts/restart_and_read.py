"""Check update entity and HA restart to pick up new code."""
import json
import requests
import time

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Check update entity
print("=== Update Entity ===")
resp = requests.get(f"{BASE}/api/states/update.home_energy_management_update", headers=HEADERS)
data = resp.json()
attrs = data.get("attributes", {})
print(f"  state: {data.get('state')}")
print(f"  installed_version: {attrs.get('installed_version')}")
print(f"  latest_version: {attrs.get('latest_version')}")
print(f"  in_progress: {attrs.get('in_progress')}")

# 2. Re-fetch from HACS to ensure latest code is downloaded
print("\n=== Re-downloading from HACS ===")
import websocket
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
    resp = json.loads(ws.recv())
    ok = resp.get("success", False)
    print(f"  [{'OK' if ok else 'FAIL'}] {payload.get('type')}")
    if not ok:
        print(f"       Error: {resp.get('error', {}).get('message', '?')}")
    return resp

call_ws({
    "type": "hacs/repository/download",
    "repository": "1188173164",
    "version": "main",
})
ws.close()

print("  Waiting 10s...")
time.sleep(10)

# 3. Restart HA completely
print("\n=== Restarting Home Assistant ===")
resp = requests.post(
    f"{BASE}/api/services/homeassistant/restart",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")

print("  Waiting 30s for HA to come back up...")
time.sleep(30)

# 4. Check if service is now available
print("\n=== Checking services after restart ===")
for attempt in range(5):
    try:
        resp = requests.get(f"{BASE}/api/services", headers=HEADERS, timeout=5)
        for svc in resp.json():
            if svc.get("domain") == "home_energy_management":
                print(f"  Domain: {svc['domain']}")
                for name, details in svc.get("services", {}).items():
                    print(f"    - {name}: {details.get('name', '?')}")
        break
    except Exception as e:
        print(f"  Attempt {attempt+1}: {e}")
        time.sleep(10)

# 5. Call read_local_config
print("\n=== Calling read_local_config ===")
time.sleep(2)
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")
time.sleep(2)

# 6. Read the persistent notification
print("\n=== Reading notification ===")
resp = requests.get(f"{BASE}/api/states/persistent_notification.hem_local_config", headers=HEADERS)
if resp.status_code == 200:
    data = resp.json()
    attrs = data.get("attributes", {})
    print(f"  Title: {attrs.get('title', '?')}")
    print(f"  Content:\n{attrs.get('message', '?')}")
else:
    print(f"  Status: {resp.status_code}")
    # Check all notifications
    resp2 = requests.get(f"{BASE}/api/states", headers=HEADERS)
    for s in resp2.json():
        if "persistent_notification" in s["entity_id"]:
            print(f"  Found: {s['entity_id']}")

print("\n=== Done ===")
