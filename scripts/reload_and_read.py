"""Reload integration using correct websocket command, then read mapping."""
import json
import time
import requests
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"
ENTRY_ID = "01KMAGB0ZDHFR8J9KQXY428EY7"

# 1. Reload via REST API
print("=== Reloading integration via REST ===")
resp = requests.post(
    f"{BASE}/api/config/config_entries/entry/{ENTRY_ID}/reload",
    headers=HEADERS,
)
print(f"  Status: {resp.status_code}")
print(f"  Response: {resp.text[:200]}")

print("  Waiting 5s for reload...")
time.sleep(5)

# 2. Call read_local_config via REST
print("\n=== Calling read_local_config ===")
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")

time.sleep(2)

# 3. Read the persistent notification
print("\n=== Reading persistent notification ===")
resp = requests.get(
    f"{BASE}/api/states/persistent_notification.hem_local_config",
    headers=HEADERS,
)
if resp.status_code == 200:
    data = resp.json()
    attrs = data.get("attributes", {})
    title = attrs.get("title", "?")
    message = attrs.get("message", "?")
    print(f"  Title: {title}")
    print(f"  Content:\n{message}")
else:
    print(f"  Not found: {resp.status_code}")
    # Check all persistent notifications
    resp2 = requests.get(f"{BASE}/api/states", headers=HEADERS)
    for s in resp2.json():
        if "persistent_notification" in s["entity_id"]:
            print(f"  Found: {s['entity_id']} - {s.get('attributes', {}).get('title', '?')}")

print("\n=== Done ===")
