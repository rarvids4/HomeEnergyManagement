#!/usr/bin/env python3
"""Update HACS integration and restart Home Assistant."""

import asyncio
import json
import aiohttp
import pathlib

HA_URL = "http://172.16.0.9:8123"
WS_URL = "ws://172.16.0.9:8123/api/websocket"
TOKEN_FILE = pathlib.Path(__file__).parent / ".ha_token"
REPO_ID = "1188173164"


async def main():
    token = TOKEN_FILE.read_text().strip()
    print(f"✅ Token loaded ({len(token)} chars)")

    msg_id = 1

    async with aiohttp.ClientSession() as session:
        # --- WebSocket phase ---
        async with session.ws_connect(WS_URL) as ws:
            # 1) Wait for auth_required
            resp = await ws.receive_json()
            print(f"⬅  {resp['type']}")

            # 2) Authenticate
            await ws.send_json({"type": "auth", "access_token": token})
            resp = await ws.receive_json()
            print(f"⬅  {resp['type']}")
            if resp["type"] != "auth_ok":
                print("❌ Authentication failed!")
                return

            print(f"✅ Authenticated (HA {resp.get('ha_version', '?')})")

            # 3) HACS repository update
            print(f"\n📦 Sending hacs/repository/update for repo {REPO_ID}...")
            await ws.send_json({
                "id": msg_id,
                "type": "hacs/repository/update",
                "repository": REPO_ID,
            })
            resp = await ws.receive_json()
            print(f"⬅  id={resp.get('id')} success={resp.get('success')} "
                  f"result={json.dumps(resp.get('result'))}")
            if not resp.get("success"):
                print(f"⚠️  Error: {resp.get('error')}")
            msg_id += 1

            # 4) Wait, then HACS repository download
            print("\n⏳ Waiting 3 seconds before download...")
            await asyncio.sleep(3)

            print(f"📥 Sending hacs/repository/download for repo {REPO_ID}...")
            await ws.send_json({
                "id": msg_id,
                "type": "hacs/repository/download",
                "repository": REPO_ID,
            })
            resp = await ws.receive_json()
            print(f"⬅  id={resp.get('id')} success={resp.get('success')} "
                  f"result={json.dumps(resp.get('result'))}")
            if not resp.get("success"):
                print(f"⚠️  Error: {resp.get('error')}")
            msg_id += 1

        # --- REST phase: restart HA ---
        print("\n🔄 Restarting Home Assistant via REST API...")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with session.post(
            f"{HA_URL}/api/services/homeassistant/restart",
            headers=headers,
        ) as resp:
            status = resp.status
            body = await resp.text()
            print(f"⬅  HTTP {status}: {body[:200]}")
            if status == 200:
                print("✅ Restart command sent successfully!")
            else:
                print("❌ Restart request failed.")

    print("\n🏁 Done.")


if __name__ == "__main__":
    asyncio.run(main())
