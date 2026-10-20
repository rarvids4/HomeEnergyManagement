"""EV hour selection when effective prices tie (flat monthly-average buy price)."""

from datetime import datetime

from custom_components.home_energy_management.ev_scheduler import EVScheduler

PEAK, OFFPEAK = 2.3626, 1.8936   # monthly average + Vattenfall tariff

# 20 Oct 2026 from 19:00: real Nordpool SE3 spot (SEK/kWh) per hour
SPOT = {
    19: 1.887, 20: 1.405, 21: 1.046, 22: 0.919, 23: 0.655, 0: 0.739,
    1: 0.635, 2: 0.523, 3: 0.477, 4: 0.519, 5: 0.600, 6: 0.897,
    7: 0.782, 8: 0.825,
}


def _plan():
    return [
        {
            "hour": h,
            "action": "self_consumption",
            "price": OFFPEAK if (h >= 22 or h < 6) else PEAK,
            "spot_price": s,
        }
        for h, s in SPOT.items()
    ]


def _xc60(**overrides):
    ev = {
        "name": "ex90",
        "connected": True,
        "vehicle_soc": 54.0,
        "vehicle_soc_available": True,
        "vehicle_capacity_kwh": 18.819,
        "vehicle_charging_power_w": 7000,
        "departure_time": "07:00",
        "min_departure_soc": 57,
        "min_charge_level": 50,
    }
    ev.update(overrides)
    return ev


def _hours(result):
    return result["vehicles"][0]["scheduled_hours"]


def test_equal_offpeak_prices_pick_the_lowest_spot_hour():
    """All off-peak hours cost 1.89 → charge at 03:00 (spot 0.48), not 22:00."""
    result = EVScheduler({}).plan(
        _plan(), [_xc60()], 19, now=datetime(2026, 10, 20, 19, 0),
    )
    assert _hours(result) == [3]


def test_bigger_need_fills_cheapest_spot_hours_first():
    """Two hours needed → 03:00 and 04:00 (spots 0.477, 0.519)."""
    result = EVScheduler({}).plan(
        _plan(), [_xc60(min_departure_soc=110, vehicle_charging_power_w=7000)],
        19, now=datetime(2026, 10, 20, 19, 0),
    )
    # 56 % of 18.8 kWh ≈ 10.5 kWh → 2 h at 7 kW
    assert _hours(result) == [3, 4]


def test_effective_price_still_wins_over_spot():
    """A peak hour with low spot is never chosen over an off-peak hour."""
    plan = _plan()
    for e in plan:
        if e["hour"] == 20:
            e["spot_price"] = 0.01          # cheap spot, but peak tariff
    result = EVScheduler({}).plan(
        plan, [_xc60()], 19, now=datetime(2026, 10, 20, 19, 0),
    )
    assert _hours(result) == [3]
