#!/usr/bin/env python3
"""Check available resources and the house_load sensor."""
import asyncio
import json
from pathlib import Path

import aiohttp

HA_URL = "http://172.16.0.9:8123"
WS_URL = "ws://172.16.0.9:8123/api/websocket"
token = Path(__file__).parent.joinpath(".ha_token").read_text().strip()


async def main():
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        # Check if ApexCharts is available
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            assert auth["type"] == "auth_ok"

            await ws.send_json({"id": 1, "type": "lovelace/resources"})
            while True:
                resp = await ws.receive_json()
                if resp.get("id") == 1:
                    break
            resources = resp.get("result", [])
            print("=== Lovelace Resources ===")
            for r in resources:
                url = r.get("url", "")
                print(f"  {url}  (type={r.get('type')})")
                if "apex" in url.lower():
                    print("  ^^^ ApexCharts FOUND!")

        # Check existing house load sensor
        for entity in [
            "sensor.home_energy_management_predicted_consumption",
            "sensor.sungrow_house_load",
            "sensor.sungrow_load_power",
        ]:
            async with session.get(f"{HA_URL}/api/states/{entity}", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"\n=== {entity} ===")
                    print(f"  state: {data.get('state')}")
                    attrs = data.get("attributes", {})
                    print(f"  unit: {attrs.get('unit_of_measurement')}")
                    # Show attribute keys
                    print(f"  attribute keys: {list(attrs.keys())}")
                else:
                    print(f"\n=== {entity} === NOT FOUND (HTTP {resp.status})")


asyncio.run(main())
