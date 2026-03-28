#!/usr/bin/env python3
"""Update the Predicted Consumption card to show actual vs predicted overlay."""
import asyncio
import json
from pathlib import Path

import aiohttp

HA_URL = "http://172.16.0.9:8123"
WS_URL = "ws://172.16.0.9:8123/api/websocket"
token = Path(__file__).parent.joinpath(".ha_token").read_text().strip()

# New ApexCharts card to replace the old history-graph
NEW_CARD = {
    "type": "custom:apexcharts-card",
    "header": {
        "title": "📈 Predicted vs Actual Consumption",
        "show": True,
    },
    "graph_span": "48h",
    "span": {"end": "now"},
    "yaxis": [
        {
            "id": "kwh",
            "min": 0,
            "decimals": 2,
            "apex_config": {
                "title": {"text": "kWh"},
            },
        }
    ],
    "series": [
        {
            "entity": "sensor.home_energy_management_predicted_consumption",
            "name": "Predicted",
            "type": "line",
            "color": "#2196F3",
            "stroke_width": 2,
            "curve": "stepline",
            "yaxis_id": "kwh",
            "extend_to": False,
        },
        {
            "entity": "sensor.home_energy_management_actual_consumption",
            "name": "Actual",
            "type": "line",
            "color": "#F44336",
            "stroke_width": 2,
            "curve": "stepline",
            "yaxis_id": "kwh",
            "extend_to": False,
        },
    ],
}


async def main():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            # Auth
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            assert auth["type"] == "auth_ok"
            msg_id = 1

            # Get current config
            await ws.send_json(
                {"id": msg_id, "type": "lovelace/config", "url_path": "energy-management"}
            )
            while True:
                resp = await ws.receive_json()
                if resp.get("id") == msg_id:
                    break
            msg_id += 1

            config = resp["result"]

            # Find and replace the Predicted Consumption history-graph card in view 1
            view = config["views"][1]
            cards = view["cards"]
            replaced = False
            for i, card in enumerate(cards):
                if (
                    card.get("type") == "history-graph"
                    and "Predicted Consumption" in card.get("title", "")
                ):
                    print(f"Found card at index {i}: {card.get('title')}")
                    cards[i] = NEW_CARD
                    replaced = True
                    break

            if not replaced:
                print("ERROR: Could not find the Predicted Consumption history-graph card!")
                return

            # Save updated config
            await ws.send_json(
                {
                    "id": msg_id,
                    "type": "lovelace/config/save",
                    "url_path": "energy-management",
                    "config": config,
                }
            )
            while True:
                resp = await ws.receive_json()
                if resp.get("id") == msg_id:
                    break

            if resp.get("success"):
                print("✅ Dashboard updated! Predicted vs Actual chart is live.")
            else:
                print(f"❌ Failed to save: {json.dumps(resp, indent=2)}")


asyncio.run(main())
