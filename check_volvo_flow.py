#!/usr/bin/env python3
"""Check Volvo Cars config flow status in Home Assistant."""

import asyncio
import json
import os

import websockets

HA_WS = "ws://172.16.0.9:8123/api/websocket"
HA_URL = "http://172.16.0.9:8123"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ha_token")
OLD_FLOW_ID = "01KMAQFQYFA86JN42VZZNTDV71"


def read_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


async def send_and_receive(ws, msg_id, payload):
    payload["id"] = msg_id
    await ws.send(json.dumps(payload))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == msg_id:
            return resp


async def main():
    token = read_token()
    msg_id = 0

    print("=" * 70)
    print("VOLVO CONFIG FLOW STATUS CHECK")
    print("=" * 70)

    async with websockets.connect(HA_WS) as ws:
        # Wait for auth_required
        auth_req = json.loads(await ws.recv())
        print(f"\n[1] Connected. HA version: {auth_req.get('ha_version', '?')}")

        # Authenticate
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            print(f"ERROR: Authentication failed: {auth_resp}")
            return
        print(f"    Authenticated OK (version {auth_resp.get('ha_version', '?')})")

        # --- Step 1: Check existing config entries for volvo ---
        msg_id += 1
        print(f"\n[2] Checking existing Volvo config entries...")
        resp = await send_and_receive(ws, msg_id, {"type": "config_entries/get"})
        if resp.get("success"):
            volvo_entries = [e for e in resp["result"] if e.get("domain") == "volvo"]
            if volvo_entries:
                print(f"    ✅ Volvo is ALREADY configured! Found {len(volvo_entries)} entry(ies):")
                for entry in volvo_entries:
                    print(f"       - Entry ID: {entry.get('entry_id')}")
                    print(f"         Title: {entry.get('title')}")
                    print(f"         State: {entry.get('state')}")
                    print(f"         Source: {entry.get('source')}")
            else:
                print("    ❌ No existing Volvo config entries found.")
        else:
            print(f"    Error: {resp}")

        # --- Step 2: List in-progress config flows ---
        msg_id += 1
        print(f"\n[3] Listing all in-progress config flows...")
        resp = await send_and_receive(ws, msg_id, {"type": "config_entries/flow/progress"})
        old_flow_active = False
        if resp.get("success"):
            flows = resp["result"]
            print(f"    Total in-progress flows: {len(flows)}")
            for f in flows:
                marker = " <<<< OLD FLOW" if f.get("flow_id") == OLD_FLOW_ID else ""
                print(f"       - flow_id={f.get('flow_id')}, handler={f.get('handler')}, context={f.get('context')}{marker}")
                if f.get("flow_id") == OLD_FLOW_ID:
                    old_flow_active = True

            if old_flow_active:
                print(f"\n    ✅ Old flow {OLD_FLOW_ID} is STILL ACTIVE.")
            else:
                print(f"\n    ❌ Old flow {OLD_FLOW_ID} is NOT active (gone/expired).")
        else:
            print(f"    Error: {resp}")

        # --- Step 3: Get details of old flow if still active ---
        if old_flow_active:
            msg_id += 1
            print(f"\n[4] Getting details of old flow {OLD_FLOW_ID}...")
            resp = await send_and_receive(ws, msg_id, {
                "type": "config_entries/flow/get",
                "flow_id": OLD_FLOW_ID,
            })
            if resp.get("success"):
                result = resp["result"]
                print(f"    Step: {result.get('step_id')}")
                print(f"    Type: {result.get('type')}")
                print(f"    Handler: {result.get('handler')}")
                print(f"    Description: {result.get('description_placeholders')}")
                if result.get("step_id") == "auth" or "url" in json.dumps(result).lower():
                    print(f"\n    Full flow result:")
                    print(json.dumps(result, indent=4))
            else:
                print(f"    Error (flow may have expired): {resp}")

        # --- Step 4: Start a NEW flow if old one is gone ---
        new_flow_result = None
        if not old_flow_active:
            msg_id += 1
            print(f"\n[4] Starting NEW Volvo config flow...")
            resp = await send_and_receive(ws, msg_id, {
                "type": "config_entries/flow",
                "handler": "volvo",
                "show_advanced_options": False,
            })
            if resp.get("success"):
                new_flow_result = resp["result"]
                print(f"    ✅ New flow started!")
                print(f"    Flow ID: {new_flow_result.get('flow_id')}")
                print(f"    Step: {new_flow_result.get('step_id')}")
                print(f"    Type: {new_flow_result.get('type')}")
                print(f"    Handler: {new_flow_result.get('handler')}")
                print(f"    Description placeholders: {new_flow_result.get('description_placeholders')}")
                print(f"\n    Full new flow result:")
                print(json.dumps(new_flow_result, indent=4))
            else:
                print(f"    Error starting flow: {resp}")
                print(json.dumps(resp, indent=4))

        # --- Summary ---
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)

        if volvo_entries:
            print(f"\n🟢 Volvo is already configured ({len(volvo_entries)} entry).")
            for entry in volvo_entries:
                print(f"   State: {entry.get('state')}")
        else:
            print("\n🔴 Volvo is NOT yet configured.")

        if old_flow_active:
            print(f"\n🟡 Old flow {OLD_FLOW_ID} is still active.")
            print(f"   HA UI URL to complete: {HA_URL}/config/integrations/dashboard")
            print(f"   Direct flow URL:       {HA_URL}/config/integrations/flow/{OLD_FLOW_ID}")
        else:
            print(f"\n🔴 Old flow {OLD_FLOW_ID} is gone/expired.")

        if new_flow_result:
            new_fid = new_flow_result.get("flow_id")
            step = new_flow_result.get("step_id")
            print(f"\n🟢 New flow started: {new_fid}, step: {step}")
            print(f"   HA UI flow URL: {HA_URL}/config/integrations/flow/{new_fid}")

            # Look for OAuth URL in description_placeholders
            placeholders = new_flow_result.get("description_placeholders") or {}
            oauth_url = placeholders.get("url") or placeholders.get("auth_url") or placeholders.get("oauth_url")
            if oauth_url:
                print(f"\n   🔗 OAuth URL (open in browser): {oauth_url}")

            # Check data_schema for any url field
            if new_flow_result.get("data_schema"):
                print(f"\n   Data schema: {json.dumps(new_flow_result['data_schema'], indent=4)}")

            # If it's an external auth step
            if step in ("auth", "authorize", "external"):
                print(f"\n   ⚠️  This flow requires external authentication.")
                print(f"   Open this in your browser to proceed:")
                print(f"   {HA_URL}/config/integrations/flow/{new_fid}")
                if oauth_url:
                    print(f"   Or directly: {oauth_url}")

        print()


if __name__ == "__main__":
    asyncio.run(main())
