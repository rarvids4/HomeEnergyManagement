"""Read deployed mapping from HA via integration data dump."""
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

# 1. Try rendering a template that reads the mapping file
print("=== Reading mapping via template ===")
# Use Jinja2 template to read a file (if allowed)
resp = call({
    "type": "render_template",
    "template": """{% set ns = namespace(content='') %}
{{ states | selectattr('entity_id', 'match', 'sensor.home_energy_management.*') | map(attribute='entity_id') | list }}""",
})
result = resp.get("result")
if result is not None:
    print(f"  Result: {result}")
else:
    print(f"  No result (resp keys: {list(resp.keys())})")

# 2. Try the diagnostics endpoint — this sometimes includes config data
# Check what the coordinator has in ev_chargers config
print("\n=== EV Charger Plan Details ===")
resp = call({"type": "get_states"})
for s in resp.get("result", []):
    if s["entity_id"] == "sensor.home_energy_management_ev_charger_plan":
        attrs = s.get("attributes", {})
        chargers = attrs.get("ev_chargers", [])
        for c in chargers:
            print(f"\n  Vehicle: {c.get('name')}")
            print(f"    min_departure_soc: {c.get('min_departure_soc')}")
            print(f"    min_charge_level: {c.get('min_charge_level', 'N/A')}")
            print(f"    departure_time: {c.get('departure_time')}")
            print(f"    vehicle_soc: {c.get('vehicle_soc')}")
            print(f"    connected: {c.get('connected')}")
        
        # Check ev_vehicles (per-vehicle schedule details)
        vehicles = attrs.get("ev_vehicles", [])
        print(f"\n  ev_vehicles: {json.dumps(vehicles, indent=4)}")
        
        # Check schedule
        schedule = attrs.get("ev_charge_schedule", [])
        print(f"\n  Schedule ({len(schedule)} entries):")
        for entry in schedule:
            if entry.get("charging"):
                print(f"    Hour {entry['hour']}: {entry['price']:.4f} SEK — {entry.get('vehicles', {})}")

# 3. Check the full HA log for auto-replan watched message
print("\n=== Full HA Error Log (searching for 'watching') ===")
import requests
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
resp = requests.get("http://172.16.0.9:8123/api/error_log", headers=HEADERS)
full_log = resp.text
for line in full_log.split("\n"):
    ll = line.lower()
    if any(kw in ll for kw in ["watching", "replan", "setting changed", "min_departure", "departure_soc", "charge_level", "optimization_days"]):
        print(f"  {line.strip()[:200]}")

ws.close()
print("\n=== Done ===")
