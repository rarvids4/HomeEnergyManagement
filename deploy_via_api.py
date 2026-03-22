#!/usr/bin/env python3
"""Deploy HomeEnergyManagement to HA entirely via API.

Steps:
1. Update integration via HACS (re-download latest from GitHub)
2. Restart HA so the new code loads
3. Wait for HA to come back online
4. Call write_local_config service with the real entity IDs
"""

import asyncio
import json
import os
import sys
import time

import aiohttp

HA_URL = "http://172.16.0.9:8123"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ha_token")
LOCAL_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "variable_mapping.local.yaml"
)

HACS_REPO_ID = "1188173164"


def load_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def load_local_config():
    with open(LOCAL_CONFIG, encoding="utf-8") as f:
        return f.read()


async def ws_send_and_receive(ws, msg_id, payload):
    """Send a WebSocket message and wait for the matching response."""
    payload["id"] = msg_id
    await ws.send_json(payload)
    while True:
        resp = await ws.receive_json()
        if resp.get("id") == msg_id:
            return resp


async def main():
    token = load_token()
    local_yaml = load_local_config()

    async with aiohttp.ClientSession() as session:
        # --- Step 1: Update via HACS WebSocket ---
        print("=== Step 1: Updating integration via HACS ===")
        ws_url = f"{HA_URL}/api/websocket"
        async with session.ws_connect(ws_url) as ws:
            # Auth handshake
            hello = await ws.receive_json()
            print(f"  HA version: {hello.get('ha_version')}")
            await ws.send_json({"type": "auth", "access_token": token})
            auth_resp = await ws.receive_json()
            if auth_resp.get("type") != "auth_ok":
                print(f"  AUTH FAILED: {auth_resp}")
                sys.exit(1)
            print("  Authenticated ✓")

            msg_id = 1

            # Re-download latest from HACS
            msg_id += 1
            resp = await ws_send_and_receive(ws, msg_id, {
                "type": "hacs/repository/download",
                "repository": HACS_REPO_ID,
                "version": "main",
            })
            if resp.get("success"):
                print("  HACS download: SUCCESS ✓")
            else:
                print(f"  HACS download result: {resp}")
                # Continue anyway — might already be latest

        # --- Step 2: Restart HA ---
        print("\n=== Step 2: Restarting Home Assistant ===")
        headers = {"Authorization": f"Bearer {token}"}
        resp = await session.post(
            f"{HA_URL}/api/services/homeassistant/restart",
            headers=headers,
            json={},
        )
        print(f"  Restart response: {resp.status}")

        # --- Step 3: Wait for HA to come back ---
        print("\n=== Step 3: Waiting for HA to come back online ===")
        await asyncio.sleep(10)  # Initial wait
        for attempt in range(60):
            try:
                resp = await session.get(
                    f"{HA_URL}/api/",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                if resp.status == 200:
                    print(f"  HA is online ✓ (attempt {attempt + 1})")
                    break
            except Exception:
                pass
            print(f"  Waiting... ({attempt + 1}/60)")
            await asyncio.sleep(5)
        else:
            print("  ERROR: HA did not come back online in 5 minutes!")
            sys.exit(1)

        # Give it a few more seconds to fully initialize
        print("  Waiting 15s for full initialization...")
        await asyncio.sleep(15)

        # --- Step 4: Deploy local config via service ---
        print("\n=== Step 4: Deploying local config ===")
        print(f"  Config size: {len(local_yaml)} bytes")

        resp = await session.post(
            f"{HA_URL}/api/services/home_energy_management/write_local_config",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"content": local_yaml},
        )
        print(f"  Service call response: {resp.status}")
        if resp.status == 200:
            body = await resp.json()
            print(f"  Result: {body}")
            print("\n✅ Deployment complete!")
            print(
                "   The local config is written. Restart HA once more"
                " (or call force_replan) to load it."
            )
        elif resp.status == 404:
            print(
                "  Service not found — integration may not be set up yet."
                "\n  Go to Settings → Integrations → + → 'Home Energy Management'"
                "\n  Then re-run this script or call the service manually."
            )
        else:
            body = await resp.text()
            print(f"  Unexpected response: {body}")


if __name__ == "__main__":
    asyncio.run(main())
