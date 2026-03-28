#!/usr/bin/env python3
"""Find the Predicted Consumption card in the HA dashboard."""
import asyncio
import json
from pathlib import Path

import aiohttp

HA_URL = "http://172.16.0.9:8123"
WS_URL = "ws://172.16.0.9:8123/api/websocket"
token = Path(__file__).parent.joinpath(".ha_token").read_text().strip()


async def main():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            assert auth["type"] == "auth_ok"

            await ws.send_json(
                {"id": 1, "type": "lovelace/config", "url_path": "energy-management"}
            )
            while True:
                resp = await ws.receive_json()
                if resp.get("id") == 1:
                    break

            views = resp.get("result", {}).get("views", [])
            for vi, view in enumerate(views):
                cards = view.get("cards", [])
                for ci, card in enumerate(cards):
                    card_str = json.dumps(card, default=str)
                    if "predict" in card_str.lower():
                        print(f"=== View {vi}, Card {ci} ===")
                        print(json.dumps(card, indent=2, default=str))
                        print()


asyncio.run(main())
