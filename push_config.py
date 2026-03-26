"""Push local variable_mapping.local.yaml to the HA server via write_local_config service."""
import json
import os
import requests

HA = "http://172.16.0.9:8123"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ha_token")
YAML_FILE = os.path.join(os.path.dirname(__file__), "config", "variable_mapping.local.yaml")

with open(TOKEN_FILE) as f:
    token = f.read().strip()

with open(YAML_FILE) as f:
    content = f.read()

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

resp = requests.post(
    f"{HA}/api/services/home_energy_management/write_local_config",
    headers=headers,
    json={"content": content},
)

print(f"Status: {resp.status_code}")
print(f"Wrote {len(content)} bytes")

# Now force a replan so the new mapping takes effect
resp2 = requests.post(
    f"{HA}/api/services/home_energy_management/force_replan",
    headers=headers,
    json={},
)
print(f"Force replan: {resp2.status_code}")
