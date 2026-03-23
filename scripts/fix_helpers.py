"""Fix input helper units and names on Home Assistant."""
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

# --- 1. Fix unit_of_measurement on departure SoC helpers ---
# We need to use input_number/update which needs the input_number_id (not entity_id).
# First, list all input_numbers to find the IDs.
print("=== Listing input_number helpers ===")
resp = call({"type": "input_number/list"})
helpers = resp.get("result", [])

target_fixes = {
    "input_number.ex90_min_departure_soc": {"unit_of_measurement": "%"},
    "input_number.zoe_min_departure_soc": {"unit_of_measurement": "%"},
    "input_number.ev_optimization_days": {"name": "EV Optimization Window"},
}

for helper in helpers:
    eid = f"input_number.{helper['id']}"
    if eid in target_fixes:
        fix = target_fixes[eid]
        print(f"\n--- Current {eid}: name={helper.get('name')}, min={helper.get('min')}, max={helper.get('max')}, step={helper.get('step')}, unit={helper.get('unit_of_measurement')} ---")
        print(f"    Applying: {fix}")
        update_payload = {
            "type": "input_number/update",
            "input_number_id": helper["id"],
            # Required fields — preserve existing values
            "name": helper.get("name", ""),
            "min": helper.get("min", 0),
            "max": helper.get("max", 100),
        }
        # Optional fields — preserve if they exist
        if helper.get("step") is not None:
            update_payload["step"] = helper["step"]
        if helper.get("mode") is not None:
            update_payload["mode"] = helper["mode"]
        if helper.get("unit_of_measurement") is not None:
            update_payload["unit_of_measurement"] = helper["unit_of_measurement"]
        # Apply our fixes on top
        update_payload.update(fix)
        call(update_payload)

# --- 2. Verify the changes ---
print("\n=== Verifying changes ===")
resp = call({"type": "get_states"})
check_entities = [
    "input_number.ex90_min_departure_soc",
    "input_number.zoe_min_departure_soc",
    "input_number.ex90_min_charge_level",
    "input_number.zoe_min_charge_level",
    "input_number.ev_optimization_days",
]
for s in resp.get("result", []):
    if s["entity_id"] in check_entities:
        attrs = s.get("attributes", {})
        unit = attrs.get("unit_of_measurement", "?")
        fname = attrs.get("friendly_name", "?")
        print(f"  {s['entity_id']}: {s['state']}  (unit={unit}, friendly={fname})")

ws.close()
print("\n=== Done ===")
