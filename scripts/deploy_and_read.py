"""Update HACS, reload integration, then call read_local_config."""
import json
import time
import requests
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "http://172.16.0.9:8123"
ENTRY_ID = "01KMAGB0ZDHFR8J9KQXY428EY7"
REPO_ID = "1188173164"

ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()

msg_id = 10

def call(payload):
    global msg_id
    msg_id += 1
    payload["id"] = msg_id
    ws.send(json.dumps(payload))
    resp = json.loads(ws.recv())
    ok = resp.get("success", False)
    print(f"  [{'OK' if ok else 'FAIL'}] {payload.get('type')}")
    if not ok:
        err = resp.get("error", {})
        print(f"       Error: {err.get('message', '?')}")
    return resp

# 1. Update HACS repository
print("=== Updating HACS repo ===")
call({
    "type": "hacs/repository/download",
    "repository": REPO_ID,
    "version": "main",
})
print("  Waiting 5s for download...")
time.sleep(5)

# 2. Reload integration
print("\n=== Reloading integration ===")
call({
    "type": "config_entries/reload",
    "entry_id": ENTRY_ID,
})
print("  Waiting 5s for reload...")
time.sleep(5)

# 3. Call read_local_config
print("\n=== Calling read_local_config ===")
call({
    "type": "call_service",
    "domain": "home_energy_management",
    "service": "read_local_config",
    "service_data": {},
})
time.sleep(2)

# 4. Read the persistent notification
print("\n=== Reading persistent notification ===")
resp = call({"type": "get_states"})
for s in resp.get("result", []):
    if s["entity_id"] == "persistent_notification.hem_local_config":
        attrs = s.get("attributes", {})
        title = attrs.get("title", "?")
        message = attrs.get("message", "?")
        print(f"  Title: {title}")
        print(f"  Content:\n{message}")

ws.close()
print("\n=== Done ===")
