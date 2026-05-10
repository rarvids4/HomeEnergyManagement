import yaml, requests

with open("local_config.yaml") as f:
    cfg = yaml.safe_load(f)
with open(".ha_token") as f:
    token = f.read().strip()

host = cfg["ha1"]["host"]
port = cfg["ha1"].get("port", 8123)
base = f"http://{host}:{port}"
headers = {"Authorization": f"Bearer {token}"}

entities = [
    "sensor.volvo_ex90_target_battery_charge_level",
    "input_number.ex90_min_departure_soc",
    "input_number.ex90_min_charge_level",
    "input_datetime.ex90_departure_time",
    "sensor.home_energy_optimizer_action",
    "sensor.export_power",
    "sensor.nordpool_kwh_se3_sek_3_10_025",
    "switch.ex90_charger_enabled",
    "switch.renault_zoe_charger_enabled",
    "sensor.volvo_ex90_battery",
    "sensor.renault_zoe_battery",
]
for eid in entities:
    r = requests.get(f"{base}/api/states/{eid}", headers=headers, timeout=5)
    if r.ok:
        s = r.json()
        print(f"{eid}: {s['state']}")
    else:
        print(f"{eid}: ERROR {r.status_code}")

r = requests.get(f"{base}/api/error_log", headers=headers, timeout=10)
if r.ok:
    lines = r.text.strip().splitlines()
    kw = ["home_energy", "surplus", "Fast EV", "EV ex90", "EV renault", "manual override", "ramp-down", "ramp_down"]
    relevant = [l for l in lines if any(k.lower() in l.lower() for k in kw)]
    print("\n--- Recent relevant logs ---")
    for l in relevant[-25:]:
        print(l)
