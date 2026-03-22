import asyncio, aiohttp, json

async def main():
    token = open(".ha_token").read().strip()
    headers = {"Authorization": f"Bearer {token}"}
    url = "http://172.16.0.9:8123"
    async with aiohttp.ClientSession() as session:
        # Battery plan (full_plan)
        async with session.get(f"{url}/api/states/sensor.home_energy_management_battery_plan", headers=headers) as resp:
            data = await resp.json()
            print(f"Battery Plan: {data['state']}")
            plan = data.get("attributes", {}).get("full_plan", [])
            for h in plan:
                hr = h.get("hour", "?")
                act = h.get("action", "?")
                price = h.get("price", "?")
                soc = h.get("soc", "?")
                print(f"  {str(hr):>5s}: {str(act):22s} price={str(price):>8s} soc={soc}")

        # Next action with details
        async with session.get(f"{url}/api/states/sensor.home_energy_management_next_planned_action", headers=headers) as resp:
            data = await resp.json()
            print(f"\nNext action: {data['state']}")
            attrs = data.get("attributes", {})
            print(f"  reason: {attrs.get('reason', 'N/A')}")
            print(f"  price: {attrs.get('price', 'N/A')}")

        # Current price with today's prices
        async with session.get(f"{url}/api/states/sensor.home_energy_management_current_energy_price", headers=headers) as resp:
            data = await resp.json()
            attrs = data.get("attributes", {})
            uom = attrs.get("unit_of_measurement", "")
            print(f"\nCurrent price: {data['state']} {uom}")
            today = attrs.get("today_prices", [])
            if today:
                print("Today's prices:")
                for p in today:
                    print(f"  {p.get('hour', '?'):>5s}: {p.get('price', '?')} {uom}")

        # Power status
        print(f"\nSolar: 7465 W, Export: 4395 W, Battery: 75.4%, SoC change: 0 W")
        print("=> We have ~4.4 kW surplus going to grid while battery is idle!")

asyncio.run(main())
