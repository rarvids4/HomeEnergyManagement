"""Update dashboard card[7] label from 'Optimization Days' to 'Optimization Window'."""
import json
import websocket

TOKEN = open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token").read().strip()
ws = websocket.create_connection("ws://172.16.0.9:8123/api/websocket")
ws.recv()  # auth_required
ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
ws.recv()  # auth_ok

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

# 1. Fetch current dashboard config
print("=== Fetching dashboard config ===")
resp = call({"type": "lovelace/config", "url_path": "energy-management"})
config = resp["result"]

# 2. Update card[7] label: "Optimization Days" → "Optimization Window"
card7 = config["views"][0]["cards"][7]
# The first sub-card is the "Optimization Settings" entities card
opt_card = card7["cards"][0]
for entity in opt_card.get("entities", []):
    if isinstance(entity, dict) and entity.get("entity") == "input_number.ev_optimization_days":
        old_name = entity.get("name")
        entity["name"] = "Optimization Window"
        print(f"  Updated label: '{old_name}' → '{entity['name']}'")

# 3. Save the updated config
print("\n=== Saving dashboard config ===")
call({
    "type": "lovelace/config/save",
    "url_path": "energy-management",
    "config": config,
})

# 4. Verify
print("\n=== Verifying ===")
resp = call({"type": "lovelace/config", "url_path": "energy-management"})
card7_updated = resp["result"]["views"][0]["cards"][7]
opt_entities = card7_updated["cards"][0].get("entities", [])
for e in opt_entities:
    if isinstance(e, dict) and e.get("entity") == "input_number.ev_optimization_days":
        print(f"  ✅ Label is now: '{e.get('name')}'")

ws.close()
print("\n=== Done ===")
