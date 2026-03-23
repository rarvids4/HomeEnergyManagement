"""Check available services and try calling read_local_config."""
import json
import requests

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# 1. List all HEM services
print("=== Available HEM services ===")
resp = requests.get(f"{BASE}/api/services", headers=HEADERS)
for svc in resp.json():
    if svc.get("domain") == "home_energy_management":
        print(f"  Domain: {svc['domain']}")
        for name, details in svc.get("services", {}).items():
            print(f"    - {name}: {details.get('name', '?')}")

# 2. Try calling read_local_config with explicit empty data
print("\n=== Calling read_local_config ===")
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
    json={},
)
print(f"  Status: {resp.status_code}")
print(f"  Response: {resp.text[:500]}")

# 3. Try without body
print("\n=== Calling without body ===")
resp = requests.post(
    f"{BASE}/api/services/home_energy_management/read_local_config",
    headers=HEADERS,
)
print(f"  Status: {resp.status_code}")
print(f"  Response: {resp.text[:500]}")
