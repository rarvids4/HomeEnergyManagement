"""Download from HACS, restart HA, call read_local_config, read error log."""
import json
import requests
import time
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Download from HACS
print("=== Downloading from HACS ===")
ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()
ws.send(json.dumps({"id": 11, "type": "hacs/repository/download", "repository": "1188173164", "version": "main"}))
resp = json.loads(ws.recv())
print(f"  [{'OK' if resp.get('success') else 'FAIL'}]")
ws.close()

time.sleep(5)

# 2. Restart HA
print("\n=== Restarting HA ===")
resp = requests.post(f"{BASE}/api/services/homeassistant/restart", headers=HEADERS, json={})
print(f"  Status: {resp.status_code}")

# Wait for HA to come back
print("  Waiting for HA to restart...")
for i in range(12):
    time.sleep(10)
    try:
        resp = requests.get(f"{BASE}/api/", headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            print(f"  HA is back up after ~{(i+1)*10}s")
            break
    except:
        pass
else:
    print("  Timeout waiting for HA")
    exit(1)

time.sleep(5)

# 3. Call read_local_config
print("\n=== Calling read_local_config ===")
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")

time.sleep(3)

# 4. Read error log for CONFIG_DUMP lines
print("\n=== Error Log — CONFIG_DUMP ===")
resp = requests.get(f"{BASE}/api/error_log", headers=HEADERS)
log_text = resp.text
found_dump = False
for line in log_text.split("\n"):
    if "CONFIG_DUMP" in line or "read_local_config" in line:
        print(f"  {line.strip()[:300]}")
        found_dump = True

if not found_dump:
    print("  No CONFIG_DUMP found in log")
    # Print last 30 lines of log for debugging
    print("\n  --- Last 30 lines of log ---")
    lines = log_text.split("\n")
    for line in lines[-30:]:
        if line.strip():
            print(f"  {line.strip()[:200]}")

print("\n=== Done ===")
