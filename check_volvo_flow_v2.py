#!/usr/bin/env python3
"""Check Volvo Cars config flow status in Home Assistant - v2."""

import asyncio
import json
import os
import urllib.request
import urllib.error

import websockets

HA_WS = "ws://172.16.0.9:8123/api/websocket"
HA_URL = "http://172.16.0.9:8123"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ha_token")
OLD_FLOW_ID = "01KMAQFQYFA86JN42VZZNTDV71"


def read_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def rest_post(path, token, data=None):
    """Make a REST API call to HA."""
    url = f"{HA_URL}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "reason": e.reason, "body": e.read().decode()}


def rest_get(path, token):
    """Make a REST GET call to HA."""
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "reason": e.reason, "body": e.read().decode()}


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
    print("VOLVO CONFIG FLOW STATUS CHECK (v2)")
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
        print(f"    Authenticated OK")

        # --- Step 1: Check existing config entries for volvo ---
        msg_id += 1
        print(f"\n[2] Checking existing Volvo config entries...")
        resp = await send_and_receive(ws, msg_id, {"type": "config_entries/get"})
        volvo_entries = []
        if resp.get("success"):
            volvo_entries = [e for e in resp["result"] if e.get("domain") == "volvo"]
            if volvo_entries:
                print(f"    ✅ Volvo is ALREADY configured! Found {len(volvo_entries)} entry(ies):")
                for entry in volvo_entries:
                    print(f"       - Entry ID: {entry.get('entry_id')}")
                    print(f"         Title: {entry.get('title')}")
                    print(f"         State: {entry.get('state')}")
            else:
                print("    ❌ No existing Volvo config entries found.")

        # --- Step 2: List in-progress config flows via WS ---
        msg_id += 1
        print(f"\n[3] Listing all in-progress config flows...")
        resp = await send_and_receive(ws, msg_id, {"type": "config_entries/flow/progress"})
        old_flow_active = False
        volvo_flows = []
        if resp.get("success"):
            flows = resp["result"]
            print(f"    Total in-progress flows: {len(flows)}")
            for f in flows:
                if f.get("handler") == "volvo":
                    volvo_flows.append(f)
                if f.get("flow_id") == OLD_FLOW_ID:
                    old_flow_active = True
                    print(f"       ✅ OLD FLOW FOUND: {f.get('flow_id')}")
            for f in volvo_flows:
                print(f"       Volvo flow: {f.get('flow_id')}, context={f.get('context')}")

            if old_flow_active:
                print(f"\n    ✅ Old flow {OLD_FLOW_ID} is STILL ACTIVE.")
            else:
                print(f"\n    ❌ Old flow {OLD_FLOW_ID} is NOT active (gone/expired).")
        else:
            print(f"    Error: {resp}")

        # --- Step 3: Try to get details of old flow via REST ---
        if old_flow_active:
            print(f"\n[4a] Getting old flow details via REST...")
            result = rest_get(f"/api/config/config_entries/flow/{OLD_FLOW_ID}", token)
            print(f"    Result: {json.dumps(result, indent=4)}")

    # --- Step 4: Start a NEW flow via REST API ---
    new_flow_result = None
    if not old_flow_active and not volvo_entries:
        print(f"\n[4] Starting NEW Volvo config flow via REST API...")
        result = rest_post("/api/config/config_entries/flow", token, {
            "handler": "volvo",
            "show_advanced_options": False,
        })
        print(f"    REST Response:")
        print(json.dumps(result, indent=4))
        if "flow_id" in (result or {}):
            new_flow_result = result
        elif isinstance(result, dict) and "error" in result:
            print(f"\n    ⚠️  Error starting flow. Trying alternative handler names...")
            # Try volvo_cars, volvocars
            for handler in ["volvo_cars", "volvocars"]:
                print(f"    Trying handler: {handler}")
                result = rest_post("/api/config/config_entries/flow", token, {
                    "handler": handler,
                    "show_advanced_options": False,
                })
                print(f"    Response: {json.dumps(result, indent=4)}")
                if "flow_id" in (result or {}):
                    new_flow_result = result
                    break

    # --- Also list available integrations to find exact volvo handler ---
    if not new_flow_result and not volvo_entries:
        print(f"\n[5] Searching for Volvo in available integrations...")
        async with websockets.connect(HA_WS) as ws:
            auth_req = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth_resp = json.loads(await ws.recv())
            
            msg_id = 1
            # Try listing integrations
            resp = await send_and_receive(ws, msg_id, {
                "type": "integration/descriptions",
            })
            if resp.get("success"):
                # Search for volvo in the results
                result = resp.get("result", {})
                for key in result:
                    if "volvo" in key.lower():
                        print(f"    Found integration: {key}")
            else:
                print(f"    integration/descriptions not available: {resp.get('error', {}).get('message', '')}")

            # Try manifest search
            msg_id += 1
            resp = await send_and_receive(ws, msg_id, {
                "type": "integration/manifests",
            })
            if resp.get("success"):
                manifests = resp.get("result", [])
                volvo_manifests = [m for m in manifests if "volvo" in m.get("domain", "").lower() or "volvo" in m.get("name", "").lower()]
                for m in volvo_manifests:
                    print(f"    Found manifest: domain={m.get('domain')}, name={m.get('name')}")
            else:
                print(f"    integration/manifests: {resp.get('error', {}).get('message', '')}")

            # Try listing all config flow handlers
            msg_id += 1
            resp = await send_and_receive(ws, msg_id, {
                "type": "config_entries/flow_handlers",
            })
            if resp.get("success"):
                handlers = resp.get("result", [])
                volvo_handlers = [h for h in handlers if "volvo" in h.lower()]
                print(f"    Available volvo-related flow handlers: {volvo_handlers}")
                if not volvo_handlers:
                    print(f"    ⚠️  No Volvo handler found! The integration may not be installed.")
                    print(f"    Searching for similar: {[h for h in handlers if h.startswith('v')]}")
            else:
                print(f"    flow_handlers: {resp.get('error', {}).get('message', '')}")

    # --- Summary ---
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if volvo_entries:
        print(f"\n🟢 Volvo is already configured ({len(volvo_entries)} entry).")
        for entry in volvo_entries:
            print(f"   State: {entry.get('state')}, Title: {entry.get('title')}")
    else:
        print("\n🔴 Volvo is NOT yet configured.")

    if old_flow_active:
        print(f"\n🟡 Old flow {OLD_FLOW_ID} is still active.")
        print(f"   HA UI URL: {HA_URL}/config/integrations/dashboard")
        print(f"   Direct flow: {HA_URL}/config/integrations/flow/{OLD_FLOW_ID}")
    else:
        print(f"\n🔴 Old flow {OLD_FLOW_ID} is gone/expired.")

    if new_flow_result:
        new_fid = new_flow_result.get("flow_id")
        step = new_flow_result.get("step_id")
        print(f"\n🟢 New flow started: {new_fid}")
        print(f"   Step: {step}")
        print(f"   Type: {new_flow_result.get('type')}")
        print(f"   HA UI flow URL: {HA_URL}/config/integrations/flow/{new_fid}")

        placeholders = new_flow_result.get("description_placeholders") or {}
        oauth_url = placeholders.get("url") or placeholders.get("auth_url")
        if oauth_url:
            print(f"\n   🔗 OAuth URL: {oauth_url}")

        if step in ("auth", "authorize", "external", "pick_implementation"):
            print(f"\n   ⚠️  External auth required. Open in browser:")
            print(f"   {HA_URL}/config/integrations/flow/{new_fid}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
