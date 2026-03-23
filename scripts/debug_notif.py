"""Debug: check notifications, error log, and call service again."""
import json
import requests
import time

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Call service again
print("=== Calling read_local_config ===")
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")
print(f"  Response: {resp.text[:200]}")
time.sleep(3)

# 2. List ALL entities with "persistent" or "notification"
print("\n=== All entities matching 'persistent' ===")
resp = requests.get(f"{BASE}/api/states", headers=HEADERS)
states = resp.json()
count = 0
for s in states:
    if "persistent" in s["entity_id"]:
        count += 1
        attrs = s.get("attributes", {})
        msg = attrs.get("message", "")
        print(f"  {s['entity_id']}: title={attrs.get('title','?')}")
        if msg:
            print(f"    msg (first 200): {msg[:200]}")
print(f"  Total persistent_notification entities: {count}")

# 3. Check error log
print("\n=== HA Error Log (recent) ===")
resp = requests.get(f"{BASE}/api/error_log", headers=HEADERS)
log_text = resp.text
lines = log_text.split("\n")
print(f"  Total log lines: {len(lines)}")
# Print last 50 lines
for line in lines[-50:]:
    if line.strip():
        print(f"  {line.strip()[:200]}")
