"""Check all HEM sensors and recent HA logs."""
import json
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
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
    print(f"  [{'OK' if ok else 'FAIL'}] id={msg_id}: {payload.get('type')}")
    if not ok:
        print(f"       Error: {resp.get('error', {}).get('message', '?')}")
    return resp

# 1. Find all HEM sensors
print("=== HEM Sensors ===")
resp = call({"type": "get_states"})
hem_sensors = []
for s in resp.get("result", []):
    if "home_energy_management" in s["entity_id"]:
        hem_sensors.append(s)
        attrs = s.get("attributes", {})
        attr_keys = list(attrs.keys())
        print(f"  {s['entity_id']}: state={s['state']}, last_updated={s.get('last_updated','?')}")
        print(f"    attr_keys: {attr_keys}")

# 2. Show charger plan and optimization status in detail
print("\n=== Detailed Sensor Data ===")
for s in hem_sensors:
    eid = s["entity_id"]
    if "charger" in eid or "optimization" in eid:
        print(f"\n--- {eid} ---")
        print(json.dumps(s, indent=2, default=str)[:2000])

# 3. Fetch recent system log entries for HEM
print("\n=== Recent System Log ===")
resp = call({"type": "system_log/list"})
for entry in resp.get("result", [])[:20]:
    msg = entry.get("message", "")
    name = entry.get("name", "")
    if "energy" in msg.lower() or "energy" in name.lower() or "replan" in msg.lower() or "setting" in msg.lower():
        ts = entry.get("timestamp", "?")
        print(f"  [{ts}] {name}: {msg[:200]}")

ws.close()
print("\n=== Done ===")
