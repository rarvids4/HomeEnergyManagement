import yaml, requests

with open("local_config.yaml") as f:
    cfg = yaml.safe_load(f)
with open(".ha_token") as f:
    token = f.read().strip()
base = f"http://{cfg['ha1']['host']}:8123"
headers = {"Authorization": f"Bearer {token}"}

r = requests.get(f"{base}/api/states", headers=headers, timeout=10)
states = r.json()

print("--- Grid/meter power sensors ---")
keywords = ["import_power", "purchased", "meter_active", "grid_power", "meter_power", "feed_in", "import_energy"]
for s in states:
    eid = s["entity_id"].lower()
    if any(k in eid for k in keywords):
        print(f"  {s['entity_id']}: {s['state']}")

print("\n--- Current surplus state ---")
check = [
    "sensor.export_power",
    "sensor.ex90_power",
    "sensor.renault_zoe_power",
    "switch.ex90_charger_enabled",
    "switch.renault_zoe_charger_enabled",
    "sensor.home_energy_management_surplus_charging",
]
for eid in check:
    r2 = requests.get(f"{base}/api/states/{eid}", headers=headers, timeout=5)
    if r2.ok:
        s = r2.json()
        keys = ["active_charger", "grid_export_w", "in_grace_period"]
        attrs = {k: v for k, v in s["attributes"].items() if k in keys}
        print(f"  {eid}: {s['state']} {attrs}")
