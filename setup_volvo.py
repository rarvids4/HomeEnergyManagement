#!/usr/bin/env python3
"""Set up Volvo Cars integration in Home Assistant via WebSocket API."""

import asyncio
import json
import os

import websockets

HA_WS_URL = "ws://172.16.0.9:8123/api/websocket"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ha_token")
VOLVO_API_KEY = "817d94ef93a24da887ba4449407a7e08"


def load_token():
    with open(TOKEN_FILE, "r") as f:
        return f.read().strip()


async def send_and_receive(ws, msg):
    """Send a JSON message and wait for the response with the matching id."""
    msg_id = msg.get("id")
    print(f"\n>>> SENDING (id={msg_id}):\n{json.dumps(msg, indent=2)}")
    await ws.send(json.dumps(msg))

    # Keep reading until we get our matching response
    while True:
        raw = await ws.recv()
        data = json.loads(raw)
        # HA sends events and other messages; filter for our id
        if data.get("id") == msg_id:
            print(f"\n<<< RESPONSE (id={msg_id}):\n{json.dumps(data, indent=2)}")
            return data
        else:
            # Print other messages for debugging
            msg_type = data.get("type", "unknown")
            if msg_type != "event":
                print(f"    [other message type={msg_type}]: {json.dumps(data)[:200]}")


async def main():
    token = load_token()
    msg_id = 0

    print(f"Connecting to {HA_WS_URL} ...")
    async with websockets.connect(HA_WS_URL) as ws:
        # Step 0: Wait for auth_required
        raw = await ws.recv()
        data = json.loads(raw)
        print(f"<<< {json.dumps(data)}")
        assert data["type"] == "auth_required", f"Unexpected: {data}"

        # Step 1: Authenticate
        auth_msg = {"type": "auth", "access_token": token}
        print(f"\n>>> Authenticating...")
        await ws.send(json.dumps(auth_msg))
        raw = await ws.recv()
        data = json.loads(raw)
        print(f"<<< {json.dumps(data)}")
        if data.get("type") != "auth_ok":
            print(f"ERROR: Authentication failed: {data}")
            return
        print("✅ Authenticated successfully!\n")

        # Step 2: Start config flow for "volvo"
        msg_id += 1
        start_flow = {
            "id": msg_id,
            "type": "config_entries/flow",
            "handler": "volvo",
            "show_advanced_options": False,
        }
        resp = await send_and_receive(ws, start_flow)

        if not resp.get("success"):
            print(f"\n❌ Failed to start flow: {resp}")
            # Try alternate handler name
            msg_id += 1
            start_flow2 = {
                "id": msg_id,
                "type": "config_entries/flow",
                "handler": "volvo_cars",
                "show_advanced_options": False,
            }
            print("\nTrying 'volvo_cars' as handler...")
            resp = await send_and_receive(ws, start_flow2)
            if not resp.get("success"):
                print(f"\n❌ Also failed with 'volvo_cars': {resp}")
                return

        result = resp.get("result", {})
        flow_id = result.get("flow_id")
        step_type = result.get("type")
        step_id = result.get("step_id")
        print(f"\n📋 Flow ID: {flow_id}")
        print(f"   Step type: {step_type}")
        print(f"   Step ID: {step_id}")

        if step_type == "abort":
            print(f"   Abort reason: {result.get('reason')}")
            print(f"   Description: {result.get('description_placeholders')}")
            return

        if step_type == "create_entry":
            print(f"✅ Entry created! Title: {result.get('title')}")
            return

        # If it's a form, check what fields are needed
        if step_type == "form":
            schema = result.get("data_schema", [])
            print(f"   Required fields: {json.dumps(schema, indent=2)}")
            description = result.get("description_placeholders")
            if description:
                print(f"   Description: {json.dumps(description, indent=2)}")

            # Check if the form asks for an API key
            field_names = [s.get("name", "") for s in schema] if schema else []
            print(f"   Field names: {field_names}")

            # Try submitting the API key
            user_input = {}
            for field in schema:
                name = field.get("name", "")
                if "api_key" in name.lower() or "key" in name.lower():
                    user_input[name] = VOLVO_API_KEY
                    print(f"   -> Filling '{name}' with API key")

            if user_input:
                msg_id += 1
                submit = {
                    "id": msg_id,
                    "type": "config_entries/flow",
                    "flow_id": flow_id,
                    "user_input": user_input,
                }
                resp2 = await send_and_receive(ws, submit)
                result2 = resp2.get("result", {})
                step_type2 = result2.get("type")
                step_id2 = result2.get("step_id")
                print(f"\n📋 Step 2 - Type: {step_type2}, Step ID: {step_id2}")

                if step_type2 == "external":
                    url = result2.get("url")
                    print(f"\n🔗 USER ACTION REQUIRED!")
                    print(f"   Open this URL in your browser to authorize:")
                    print(f"   {url}")
                    print(f"\n   Waiting for authorization callback...")

                    # Wait for the next step after user completes browser auth
                    # HA will push a message when the external step completes
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=300)
                        data = json.loads(raw)
                        print(f"    [waiting] got message: {json.dumps(data)[:300]}")
                        if data.get("id") == msg_id:
                            break

                elif step_type2 == "create_entry":
                    print(f"✅ Entry created! Title: {result2.get('title')}")
                    return

                elif step_type2 == "form":
                    schema2 = result2.get("data_schema", [])
                    print(f"   Next form fields: {json.dumps(schema2, indent=2)}")
                    desc2 = result2.get("description_placeholders")
                    if desc2:
                        print(f"   Description: {json.dumps(desc2, indent=2)}")
                    print("\n⚠️  More user input needed. Check the fields above.")

                elif step_type2 == "abort":
                    print(f"   Abort reason: {result2.get('reason')}")
                    return

                else:
                    print(f"   Full result: {json.dumps(result2, indent=2)}")

            else:
                print("\n⚠️  No API key field found in form. Fields available:")
                print(f"   {json.dumps(schema, indent=2)}")
                print("   Manual input may be needed.")

        elif step_type == "external":
            url = result.get("url")
            print(f"\n🔗 USER ACTION REQUIRED!")
            print(f"   Open this URL in your browser to authorize:")
            print(f"   {url}")

        else:
            print(f"\n   Full result: {json.dumps(result, indent=2)}")


if __name__ == "__main__":
    asyncio.run(main())
