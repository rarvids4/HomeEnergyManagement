"""Deploy latest code via HACS WebSocket API."""
import asyncio
import json
import os
import websockets

HA = "ws://172.16.0.9:8123/api/websocket"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ha_token")
REPO_ID = "1188173164"
ENTRY_ID = "01KMAGB0ZDHFR8J9KQXY428EY7"


async def main():
    with open(TOKEN_FILE) as f:
        token = f.read().strip()

    ws = await websockets.connect(HA, max_size=20_000_000)
    await ws.recv()  # auth_required
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    r = json.loads(await ws.recv())
    assert r["type"] == "auth_ok", r

    mid = 1

    # 1) Refresh repo
    mid += 1
    await ws.send(json.dumps({"id": mid, "type": "hacs/repository/refresh", "repository": REPO_ID}))
    r = json.loads(await ws.recv())
    print(f"refresh: {r.get('success', r)}")

    # 2) Download latest
    mid += 1
    await ws.send(json.dumps({"id": mid, "type": "hacs/repository/download", "repository": REPO_ID, "version": "main"}))
    r = json.loads(await ws.recv())
    print(f"download: {r.get('success', r)}")

    # 3) Disable config entry
    mid += 1
    await ws.send(json.dumps({"id": mid, "type": "config_entries/disable", "entry_id": ENTRY_ID, "disabled_by": "user"}))
    r = json.loads(await ws.recv())
    print(f"disable: {r.get('success', r)}")

    # 4) Re-enable config entry
    mid += 1
    await ws.send(json.dumps({"id": mid, "type": "config_entries/disable", "entry_id": ENTRY_ID, "disabled_by": None}))
    r = json.loads(await ws.recv())
    print(f"re-enable: {r.get('success', r)}")

    await ws.close()
    print("Done — integration reloaded")


asyncio.run(main())
