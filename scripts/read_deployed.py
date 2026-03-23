"""Read the deployed mapping from HA via template rendering."""
import json
import requests

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"

# Use a Jinja2 template that reads the file from HA's config directory
template = """{% set content = read_file("variable_mapping.local.yaml") %}{{ content }}"""

resp = requests.post(
    f"{BASE}/api/template",
    headers=HEADERS,
    json={"template": template}
)
print("Status:", resp.status_code)
if resp.status_code == 200:
    print("=== Deployed Mapping ===")
    print(resp.text)
else:
    print("Error:", resp.text[:500])
    # Try alternative: just render 'config dir' path
    resp2 = requests.post(
        f"{BASE}/api/template",
        headers=HEADERS,
        json={"template": "{{ config_dir }}"}
    )
    print(f"\nConfig dir: {resp2.text}")
