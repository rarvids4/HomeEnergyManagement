#!/usr/bin/env python3
"""Step 2: Set up the integration via REST API and deploy local config."""

import asyncio
import json
import os
import sys

import aiohttp

HA_URL = "http://172.16.0.9:8123"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ha_token")
LOCAL_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "variable_mapping.local.yaml"
)


def load_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def load_local_config():
    with open(LOCAL_CONFIG, encoding="utf-8") as f:
        return f.read()


async def main():
    token = load_token()
    local_yaml = load_local_config()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        # 1) Check HA error log for loading issues
        print("=== Checking HA error log for our integration ===")
        resp = await session.get(f"{HA_URL}/api/error_log", headers=headers)
        if resp.status == 200:
            log_text = await resp.text()
            our_lines = [l for l in log_text.split("\n")
                         if "home_energy" in l.lower() or "energy_management" in l.lower()]
            if our_lines:
                print("  Log entries found:")
                for line in our_lines[-20:]:
                    print(f"    {line}")
            else:
                print("  No log entries about our integration")
        print()

        # 2) Check existing config entries
        print("=== Checking loaded integrations ===")
        resp = await session.get(
            f"{HA_URL}/api/config/config_entries/entry",
            headers=headers,
        )
        if resp.status == 200:
            entries = await resp.json()
            hem = [e for e in entries if e.get("domain") == "home_energy_management"]
            if hem:
                print(f"  Config entry exists: {json.dumps(hem, indent=2)}")
            else:
                print(f"  No config entry. Total entries: {len(entries)}")
        print()

        # 3) Try to start a config flow via REST API
        print("=== Starting config flow via REST API ===")
        resp = await session.post(
            f"{HA_URL}/api/config/config_entries/flow",
            headers=headers,
            json={"handler": "home_energy_management", "show_advanced_options": False},
        )
        flow_body = await resp.json() if resp.status < 500 else await resp.text()
        print(f"  Status: {resp.status}")
        print(f"  Response: {json.dumps(flow_body, indent=2) if isinstance(flow_body, (dict, list)) else flow_body}")

        if resp.status == 200 and isinstance(flow_body, dict):
            flow_id = flow_body.get("flow_id")
            step_id = flow_body.get("step_id")
            flow_type = flow_body.get("type")

            if flow_type == "create_entry":
                print("  Entry created directly!")
            elif flow_id:
                print(f"  Flow started: flow_id={flow_id}, step={step_id}")
                resp2 = await session.post(
                    f"{HA_URL}/api/config/config_entries/flow/{flow_id}",
                    headers=headers,
                    json={},
                )
                body2 = await resp2.json()
                print(f"  Submit step: {json.dumps(body2, indent=2)}")
                if body2.get("type") == "create_entry":
                    print("  Integration added successfully!")
        print()

        # 4) Wait and check services
        print("Waiting 5s for integration to initialize...")
        await asyncio.sleep(5)

        resp = await session.get(f"{HA_URL}/api/services", headers=headers)
        if resp.status == 200:
            services = await resp.json()
            hem_svc = [s for s in services if s.get("domain") == "home_energy_management"]
            if hem_svc:
                print(f"Services registered: {list(hem_svc[0].get('services', {}).keys())}")

                print("\n=== Deploying local config ===")
                print(f"  Config size: {len(local_yaml)} bytes")
                resp = await session.post(
                    f"{HA_URL}/api/services/home_energy_management/write_local_config",
                    headers=headers,
                    json={"content": local_yaml},
                )
                print(f"  Status: {resp.status}")
                if resp.status == 200:
                    print("  Local config deployed!")
                    print("  Restarting HA to load real entity mappings...")
                    await session.post(
                        f"{HA_URL}/api/services/homeassistant/restart",
                        headers=headers,
                        json={},
                    )
                    print("  HA restart triggered. Done!")
                else:
                    body = await resp.text()
                    print(f"  Error: {body}")
            else:
                print("Services not registered yet")
                print("  Check: is the integration loaded?")


if __name__ == "__main__":
    asyncio.run(main())
