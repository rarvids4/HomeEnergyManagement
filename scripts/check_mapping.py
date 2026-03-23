"""Read deployed mapping + check auto-replan watched entities."""
import json
import requests

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. Check HA error log for auto-replan messages
print("=== HA Error Log (last 5000 chars) ===")
resp = requests.get(f"{BASE}/api/error_log", headers=HEADERS)
log = resp.text[-5000:]
for line in log.split("\n"):
    if any(kw in line.lower() for kw in ["replan", "watching", "setting changed", "energy_management", "min_departure", "departure_soc"]):
        print(f"  {line.strip()}")

# 2. Try reading the local config via HA file API
# The integration reads from <config_dir>/variable_mapping.local.yaml
# We can check diagnostics endpoint for config entry data
print("\n=== Config Entries ===")
resp = requests.get(f"{BASE}/api/config/config_entries/entry", headers=HEADERS)
entries = resp.json()
for entry in entries:
    if "energy" in entry.get("title", "").lower() or "energy" in entry.get("domain", "").lower():
        print(f"  {entry['entry_id']}: {entry['domain']} / {entry['title']}")
        print(f"    data: {json.dumps(entry.get('data', {}), indent=4)}")
        print(f"    options: {json.dumps(entry.get('options', {}), indent=4)}")
