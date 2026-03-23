"""Read the notification with the mapping content."""
import json
import requests

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# Check all states matching persistent_notification
resp = requests.get(f"{BASE}/api/states", headers=HEADERS)
for s in resp.json():
    eid = s["entity_id"]
    if "persistent_notification" in eid or "hem" in eid.lower():
        attrs = s.get("attributes", {})
        print(f"\n=== {eid} ===")
        print(f"  state: {s.get('state')}")
        print(f"  title: {attrs.get('title', '?')}")
        msg = attrs.get("message", "?")
        print(f"  message:\n{msg}")
