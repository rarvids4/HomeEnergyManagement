#!/usr/bin/env python3
"""Discover all Volvo-related entities in Home Assistant."""

import json
import requests
from pathlib import Path

HA_URL = "http://172.16.0.9:8123"
TOKEN_FILE = Path(__file__).parent / ".ha_token"

def main():
    token = TOKEN_FILE.read_text().strip()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(f"{HA_URL}/api/states", headers=headers, timeout=30)
    resp.raise_for_status()
    states = resp.json()

    keywords = ["volvo", "ex90", "xc90", "battery_charge"]
    matched = []
    for entity in states:
        eid = entity["entity_id"].lower()
        fname = entity.get("attributes", {}).get("friendly_name", "").lower()
        if any(kw in eid or kw in fname for kw in keywords):
            matched.append(entity)

    # Sort by entity_id
    matched.sort(key=lambda e: e["entity_id"])

    print(f"Found {len(matched)} Volvo-related entities\n")
    print("=" * 120)

    for e in matched:
        eid = e["entity_id"]
        state = e["state"]
        attrs = e.get("attributes", {})
        fname = attrs.get("friendly_name", "")
        unit = attrs.get("unit_of_measurement", "")
        device_class = attrs.get("device_class", "")
        icon = attrs.get("icon", "")

        print(f"\n  entity_id:    {eid}")
        print(f"  friendly_name: {fname}")
        print(f"  state:         {state} {unit}")
        if device_class:
            print(f"  device_class:  {device_class}")
        if icon:
            print(f"  icon:          {icon}")

        # Print other interesting attributes (skip common noise)
        skip = {"friendly_name", "unit_of_measurement", "device_class", "icon",
                "attribution", "supported_features", "entity_picture"}
        extras = {k: v for k, v in attrs.items() if k not in skip and v not in (None, "", [], {})}
        if extras:
            print(f"  attributes:    {json.dumps(extras, default=str, ensure_ascii=False)}")
        print("-" * 120)

    print(f"\nTotal: {len(matched)} entities")


if __name__ == "__main__":
    main()
