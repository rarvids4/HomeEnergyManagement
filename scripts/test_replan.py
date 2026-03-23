"""Test auto-replan: change a slider and check HA logs for replan trigger."""
import json
import time
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

# 1. Read current value of EX90 departure SoC
print("=== Current State ===")
resp = call({"type": "get_states"})
for s in resp.get("result", []):
    if s["entity_id"] == "input_number.ex90_min_departure_soc":
        current_val = float(s["state"])
        print(f"  ex90_min_departure_soc = {current_val}%")

# 2. Read EV charging schedule BEFORE
for s in resp.get("result", []):
    if s["entity_id"] == "sensor.home_energy_management_charger_plan":
        attrs = s.get("attributes", {})
        schedule = attrs.get("schedule", [])
        last_updated = s.get("last_updated", "?")
        print(f"  charger_plan last_updated: {last_updated}")
        print(f"  charger_plan schedule entries: {len(schedule)}")
        if schedule:
            print(f"  First entry: {schedule[0]}")

# 3. Bump departure SoC by 1 (or reset)
new_val = current_val + 1 if current_val < 100 else current_val - 1
print(f"\n=== Changing departure SoC: {current_val} → {new_val} ===")
call({
    "type": "call_service",
    "domain": "input_number",
    "service": "set_value",
    "service_data": {
        "entity_id": "input_number.ex90_min_departure_soc",
        "value": new_val,
    },
})

# 4. Wait for replan to trigger
print("\n=== Waiting 10s for replan... ===")
time.sleep(10)

# 5. Check updated state
print("=== After Replan ===")
resp = call({"type": "get_states"})
for s in resp.get("result", []):
    if s["entity_id"] == "input_number.ex90_min_departure_soc":
        print(f"  ex90_min_departure_soc = {s['state']}%")
    if s["entity_id"] == "sensor.home_energy_management_charger_plan":
        attrs = s.get("attributes", {})
        schedule = attrs.get("schedule", [])
        last_updated = s.get("last_updated", "?")
        print(f"  charger_plan last_updated: {last_updated}")
        print(f"  charger_plan schedule entries: {len(schedule)}")
        if schedule:
            print(f"  First entry: {schedule[0]}")
    if s["entity_id"] == "sensor.home_energy_management_optimization_status":
        print(f"  optimization_status: {s['state']}")
        attrs = s.get("attributes", {})
        print(f"    last_run: {attrs.get('last_run', '?')}")
        print(f"    ev_vehicles: {json.dumps(attrs.get('ev_vehicles', []), indent=4)}")

# 6. Restore original value
print(f"\n=== Restoring departure SoC to {current_val} ===")
call({
    "type": "call_service",
    "domain": "input_number",
    "service": "set_value",
    "service_data": {
        "entity_id": "input_number.ex90_min_departure_soc",
        "value": current_val,
    },
})

ws.close()
print("\n=== Done ===")
