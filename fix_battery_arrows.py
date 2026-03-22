#!/usr/bin/env python3
"""Fix power-flow-card-plus battery arrow direction on two HA dashboards.

Adds `invert_state: true` to the battery entity config so arrows
point the correct way (positive = FROM battery = discharging).
"""

import asyncio
import json
import copy
import websockets

HA_WS = "ws://172.16.0.9:8123/api/websocket"

# Read token from file
with open("/Users/rickardarvidsson/Library/Mobile Documents/com~apple~CloudDocs/Rickard/Hemsidor/HomeEnergyManagement/.ha_token") as f:
    TOKEN = f.read().strip()

msg_id = 0

def next_id():
    global msg_id
    msg_id += 1
    return msg_id


async def send_and_receive(ws, payload):
    """Send a JSON message and wait for the response with matching id."""
    await ws.send(json.dumps(payload))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == payload.get("id"):
            return resp


def find_and_fix_power_flow_cards(config):
    """Walk the dashboard config, find power-flow-card-plus cards,
    and add invert_state: true to their battery section.
    Returns (modified_config, count_of_fixes)."""
    config = copy.deepcopy(config)
    fixes = 0

    for view in config.get("views", []):
        for card in view.get("cards", []):
            fixes += _fix_card(card)
    return config, fixes


def _fix_card(card):
    """Recursively fix a card (handles nested cards like stacks)."""
    fixes = 0

    # Check if this card is a power-flow-card-plus
    if card.get("type") in ("custom:power-flow-card-plus", "custom:power-flow-card"):
        battery = card.get("entities", {}).get("battery", {})
        if isinstance(battery, dict) and battery.get("entity") and not battery.get("invert_state"):
            battery["invert_state"] = True
            fixes += 1
            print(f"  ✅ Fixed battery entity: {battery.get('entity')} — added invert_state: true")

    # Recurse into nested cards (stacks, grid, etc.)
    for nested in card.get("cards", []):
        fixes += _fix_card(nested)

    return fixes


async def fix_dashboard(ws, url_path, label):
    """Fetch a dashboard config, fix it, and save it back."""
    print(f"\n{'='*60}")
    print(f"📋 Dashboard: {label} (url_path={url_path!r})")
    print(f"{'='*60}")

    # Fetch config
    fetch_msg = {
        "id": next_id(),
        "type": "lovelace/config",
        "url_path": url_path,
    }
    resp = await send_and_receive(ws, fetch_msg)

    if not resp.get("success"):
        print(f"  ❌ Failed to fetch config: {resp.get('error', resp)}")
        return False

    config = resp["result"]
    print(f"  Found {len(config.get('views', []))} view(s)")

    # Show current battery config for debugging
    for vi, view in enumerate(config.get("views", [])):
        for ci, card in enumerate(view.get("cards", [])):
            _print_battery_info(card, f"view[{vi}].cards[{ci}]")

    # Fix
    new_config, fixes = find_and_fix_power_flow_cards(config)

    if fixes == 0:
        print("  ⚠️  No power-flow-card-plus cards found needing fix (already fixed or not found)")
        return True

    # Save config back
    save_msg = {
        "id": next_id(),
        "type": "lovelace/config/save",
        "url_path": url_path,
        "config": new_config,
    }
    save_resp = await send_and_receive(ws, save_msg)

    if save_resp.get("success"):
        print(f"  ✅ Saved! ({fixes} card(s) fixed)")
        return True
    else:
        print(f"  ❌ Save failed: {save_resp.get('error', save_resp)}")
        return False


def _print_battery_info(card, path):
    """Print battery config info for debugging."""
    if card.get("type") in ("custom:power-flow-card-plus", "custom:power-flow-card"):
        battery = card.get("entities", {}).get("battery", {})
        print(f"  📊 {path}: type={card['type']}")
        print(f"      battery config: {json.dumps(battery, indent=8)}")
    for i, nested in enumerate(card.get("cards", [])):
        _print_battery_info(nested, f"{path}.cards[{i}]")


async def main():
    print("🔌 Connecting to Home Assistant WebSocket API...")
    async with websockets.connect(HA_WS) as ws:
        # Wait for auth_required
        hello = json.loads(await ws.recv())
        print(f"  Server: {hello.get('type')} (HA version {hello.get('ha_version', '?')})")

        # Authenticate
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            print(f"  ❌ Auth failed: {auth_resp}")
            return
        print("  ✅ Authenticated")

        # Fix sidebar dashboard: energy-management
        await fix_dashboard(ws, "energy-management", "Sidebar — Energy Management")

        # Fix home/default dashboard (url_path is null for default lovelace)
        await fix_dashboard(ws, None, "Home Dashboard (default lovelace)")

    print("\n🎉 Done!")


if __name__ == "__main__":
    asyncio.run(main())
