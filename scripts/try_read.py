"""Try reading the local mapping file via various HA methods."""
import json
import requests

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# Method 1: Check if SSH add-on is available (port 22222 is common for HA SSH)
# Method 2: Try the Supervisor API to list add-ons
print("=== Supervisor Add-ons ===")
resp = requests.get(f"{BASE}/api/hassio/addons", headers=HEADERS)
if resp.status_code == 200:
    addons = resp.json().get("data", {}).get("addons", [])
    for addon in addons:
        slug = addon.get("slug", "")
        name = addon.get("name", "")
        state = addon.get("state", "")
        if any(kw in slug.lower() or kw in name.lower() for kw in ["ssh", "terminal", "file", "editor", "code"]):
            print(f"  {slug}: {name} ({state})")
else:
    print(f"  Supervisor API: {resp.status_code}")

# Method 3: Try reading via Supervisor filesystem API
print("\n=== Try Supervisor Filesystem API ===")
resp = requests.get(
    f"{BASE}/api/hassio/app/entrypoint.js",
    headers=HEADERS,
)
print(f"  Supervisor UI: {resp.status_code}")

# Method 4: Call write_local_config with empty to see error, or use HA API to read
# Actually let's check the add-on API more broadly
print("\n=== All Supervisor Add-ons ===")
resp = requests.get(f"{BASE}/api/hassio/addons", headers=HEADERS)
if resp.status_code == 200:
    addons = resp.json().get("data", {}).get("addons", [])
    for addon in addons[:20]:
        slug = addon.get("slug", "")
        name = addon.get("name", "")
        state = addon.get("state", "")
        print(f"  {slug}: {name} ({state})")

# Method 5: Try to use the Terminal & SSH add-on to cat the file
print("\n=== Try SSH API (Advanced SSH) ===")
for addon_slug in ["core_ssh", "a0d7b954_ssh", "ssh"]:
    resp = requests.post(
        f"{BASE}/api/hassio/addons/{addon_slug}/stdin",
        headers=HEADERS,
        data="cat /config/variable_mapping.local.yaml\n"
    )
    if resp.status_code == 200:
        print(f"  {addon_slug}: {resp.text}")
        break
    else:
        print(f"  {addon_slug}: {resp.status_code}")
