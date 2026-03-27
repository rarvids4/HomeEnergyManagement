"""Restart Home Assistant via WebSocket API."""
import asyncio
import json
import websockets

HA = "ws://172.16.0.9:8123/api/websocket"


async def main():
    with open(".ha_token") as f:
        token = f.read().strip()

    ws = await websockets.connect(HA, max_size=20_000_000)
    await ws.recv()  # auth_required
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    r = json.loads(await ws.recv())
    assert r["type"] == "auth_ok", r

    # Call homeassistant.restart via WS
    await ws.send(json.dumps({
        "id": 2,
        "type": "call_service",
        "domain": "homeassistant",
        "service": "restart",
    }))
    try:
        r = json.loads(await ws.recv())
        print(f"restart response: {r}")
    except Exception as e:
        print(f"Connection closed (expected during restart): {e}")

    await ws.close()
    print("Restart initiated")


asyncio.run(main())
