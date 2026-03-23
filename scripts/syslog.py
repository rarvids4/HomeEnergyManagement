"""Use system_log/list to get structured log entries."""
import json
import requests
import time
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
BASE = "http://172.16.0.9:8123"

ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()

msg_id = 10

def call_ws(payload):
    global msg_id
    msg_id += 1
    payload["id"] = msg_id
    ws.send(json.dumps(payload))
    resp = json.loads(ws.recv())
    return resp

# 1. Call read_local_config service
print("=== Calling read_local_config ===")
resp = call_ws({
    "type": "call_service",
    "domain": "home_energy_management",
    "service": "read_local_config",
    "service_data": {},
})
print(f"  Success: {resp.get('success')}")
if not resp.get("success"):
    print(f"  Error: {resp.get('error', {}).get('message', '?')}")

time.sleep(3)

# 2. Get system log
print("\n=== System Log ===")
resp = call_ws({"type": "system_log/list"})
entries = resp.get("result", [])
print(f"  Total entries: {len(entries)}")

for entry in entries:
    name = entry.get("name", "")
    message = entry.get("message", "")
    level = entry.get("level", "")
    ts = entry.get("timestamp", "")
    first_occurred = entry.get("first_occurred", "")
    count = entry.get("count", 1)
    
    # Show all entries related to HEM or CONFIG_DUMP
    name_str = str(name) if not isinstance(name, list) else str(name)
    msg_str = str(message) if not isinstance(message, list) else str(message)
    
    if any(kw in name_str.lower() + msg_str.lower() for kw in [
        "energy", "config_dump", "read_local", "mapping", "replan", "watching"
    ]):
        print(f"\n  [{level}] {name_str}")
        print(f"    ts: {ts}")
        print(f"    msg: {msg_str[:500]}")
        if count > 1:
            print(f"    count: {count}")

# 3. Show ALL log entries for full picture
print("\n=== All Recent Log Entries ===")
for entry in entries[:15]:
    name = entry.get("name", "")
    message = entry.get("message", "")
    level = entry.get("level", "")
    name_str = str(name) if not isinstance(name, list) else str(name)
    msg_str = str(message) if not isinstance(message, list) else str(message)
    print(f"  [{level}] {name_str}: {msg_str[:150]}")

ws.close()
print("\n=== Done ===")
