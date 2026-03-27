"""Debug the EV Charging Schedule card content."""
import asyncio
import json
import websockets
import requests


async def main():
    with open(".ha_token") as f:
        token = f.read().strip()

    ws = await websockets.connect(
        "ws://172.16.0.9:8123/api/websocket", max_size=20_000_000
    )
    await ws.recv()
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    r = json.loads(await ws.recv())
    assert r["type"] == "auth_ok"

    await ws.send(
        json.dumps(
            {"id": 2, "type": "lovelace/config", "url_path": "energy-management"}
        )
    )
    r = json.loads(await ws.recv())
    card = r["result"]["views"][0]["cards"][9]

    print("Card type:", card["type"])
    print("Card title:", card.get("title"))
    print("Has entity_id:", "entity_id" in card)
    print("entity_id value:", card.get("entity_id"))
    content = card.get("content", "")
    print("Content python type:", type(content).__name__)
    print("Content length:", len(content))

    # Check for double-encoding
    if isinstance(content, str) and len(content) > 2:
        if content[0] == '"' and content[-1] == '"':
            print("!! DOUBLE-ENCODED: content is wrapped in literal quotes!")
        print("First char:", repr(content[0]), "ord:", ord(content[0]))
        print("Last char:", repr(content[-1]), "ord:", ord(content[-1]))

    # Try rendering it server-side
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        "http://172.16.0.9:8123/api/template",
        headers=headers,
        json={"template": content},
    )
    print(f"\nRender status: {resp.status_code}")
    rendered = resp.text
    non_blank = [l for l in rendered.split("\n") if l.strip()]
    print(f"Non-blank lines: {len(non_blank)}")
    for line in non_blank[:10]:
        print(f"  | {line}")

    # Now check: is the ORIGINAL markdown card (before our changes) working?
    # Let's check the Hourly Plan card (card index 4) which was always there
    hourly_card = r["result"]["views"][0]["cards"][4]
    print(f"\nHourly Plan card type: {hourly_card['type']}")
    hourly_content = hourly_card.get("content", "")
    resp2 = requests.post(
        "http://172.16.0.9:8123/api/template",
        headers=headers,
        json={"template": hourly_content},
    )
    hourly_non_blank = [l for l in resp2.text.split("\n") if l.strip()]
    print(f"Hourly Plan render: {resp2.status_code}, {len(hourly_non_blank)} non-blank lines")
    if hourly_non_blank:
        print(f"  First line: {hourly_non_blank[0][:80]}")

    await ws.close()


asyncio.run(main())
