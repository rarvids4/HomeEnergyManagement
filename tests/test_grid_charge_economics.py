"""Sanity check: when is grid-charging the battery cost-optimal?

Uses Rickard's real Vattenfall SE3 grid tariffs and round-trip
efficiency from const.py. Demonstrates the break-even ratio between
cheap and expensive hours, and runs the actual BatteryStrategy LP
on a realistic day to print exactly what it decides and why.
"""
from __future__ import annotations

import logging

from custom_components.home_energy_management.battery_strategy import (
    BatteryStrategy,
)
from custom_components.home_energy_management.const import (
    DEFAULT_BATTERY_CHARGE_EFFICIENCY,
    DEFAULT_BATTERY_DISCHARGE_EFFICIENCY,
)
from custom_components.home_energy_management.price_analysis import (
    PriceAnalyzer,
)


# Rickard's actual tariffs (Vattenfall SE3, incl. 25% VAT)
PEAK_TARIFF = 1.40   # SEK/kWh, 06:00–22:00
OFFPEAK_TARIFF = 0.831  # SEK/kWh, 22:00–06:00


def _eff(spot: list[float], peak: float, offpeak: float,
         peak_start: int = 6, peak_end: int = 22) -> list[float]:
    out = []
    for h, p in enumerate(spot):
        hour_of_day = h % 24
        tariff = peak if peak_start <= hour_of_day < peak_end else offpeak
        out.append(p + tariff)
    return out


def test_break_even_ratio_with_real_grid_fees():
    """Print the ratio above which grid-charging becomes profitable.

    Pure arbitrage (charge → discharge to offset future buy) is
    profitable iff:

        effective_high * eta_d * eta_c  >  effective_low

    i.e.  effective_high / effective_low  >  1 / (eta_c * eta_d)

    With default 0.9 / 0.9 efficiencies that is ~1.235.
    """
    eta_c = DEFAULT_BATTERY_CHARGE_EFFICIENCY  # 0.9
    eta_d = DEFAULT_BATTERY_DISCHARGE_EFFICIENCY  # 0.9
    rt = eta_c * eta_d
    ratio = 1.0 / rt
    print(f"\nRound-trip eff = {rt:.3f}")
    print(f"Break-even ratio effective_high / effective_low = {ratio:.3f}")

    # Worked example with Rickard's tariffs
    examples = [
        ("flat-cheap-day",     0.10, 0.15),  # spot off-peak / peak
        ("normal-day",         0.20, 0.40),
        ("windy-cheap-night",  0.05, 0.50),
        ("expensive-evening",  0.30, 1.50),
    ]
    print(
        f"\n{'scenario':<22} {'spot_off':>9} {'spot_peak':>10} "
        f"{'eff_off':>9} {'eff_peak':>10} {'ratio':>7} {'profitable?':>12}"
    )
    for name, s_off, s_peak in examples:
        e_off = s_off + OFFPEAK_TARIFF
        e_peak = s_peak + PEAK_TARIFF
        r = e_peak / e_off
        ok = "YES" if r > ratio else "no"
        print(
            f"{name:<22} {s_off:>9.2f} {s_peak:>10.2f} "
            f"{e_off:>9.3f} {e_peak:>10.3f} {r:>7.3f} {ok:>12}"
        )

    assert ratio < 1.3  # sanity


def test_lp_grid_charges_during_cheap_offpeak_realistic_day():
    """Realistic Rickard day — does the LP grid-charge at off-peak?

    Builds a 24h spot curve with cheap night + expensive evening,
    applies Vattenfall tariffs, and runs the actual BatteryStrategy.
    Prints hour-by-hour what the LP decides and why.
    """
    logging.basicConfig(level=logging.INFO)

    # Synthetic but realistic May day on SE3:
    # cheap windy night, normal morning, expensive evening peak.
    spot = [
        0.05, 0.04, 0.04, 0.05,   # 00–04 cheap night (off-peak tariff)
        0.08, 0.12,               # 04–06 still off-peak
        0.25, 0.30, 0.35, 0.40,   # 06–10 morning peak
        0.30, 0.20, 0.15, 0.12,   # 10–14 PV-flooded
        0.18, 0.25,               # 14–16
        0.45, 0.80, 1.20, 1.40,   # 16–20 evening peak
        1.10, 0.70,               # 20–22 declining
        0.30, 0.15,               # 22–24 off-peak again
    ]
    H = 24
    eff = _eff(spot, PEAK_TARIFF, OFFPEAK_TARIFF)

    # Construct a PriceWindow manually (skip PriceAnalyzer to keep
    # the test hermetic — we only test the LP layer).
    from custom_components.home_energy_management.price_analysis import (
        PriceWindow,
    )
    pw = PriceWindow(
        effective=eff,
        spot=spot,
        current_hour=0,
        avg=sum(eff) / H,
        min=min(eff),
        max=max(eff),
        spread=max(eff) - min(eff),
        currency="SEK",
    )

    # Configure BatteryStrategy with Rickard-like settings.
    params = {
        "enable_battery_control": True,
        "min_price_spread": 0.0,
        "battery_charge_efficiency": DEFAULT_BATTERY_CHARGE_EFFICIENCY,
        "battery_discharge_efficiency": DEFAULT_BATTERY_DISCHARGE_EFFICIENCY,
    }
    outputs = {
        "sungrow": {
            "max_soc": 100,
            "min_soc": 10,
            "capacity_kwh": 24.06,
            "set_forced_power": {"max": 5000},   # 5 kW charge limit
            "set_forced_discharge_power": {"max": 5000},
        }
    }
    strat = BatteryStrategy(params, outputs)

    consumption = [0.5] * H   # 0.5 kWh/h baseload
    solar = [0.0] * 6 + [0.3, 1.2, 2.5, 3.5, 4.0, 4.0,
                         3.5, 2.5, 1.2, 0.3] + [0.0] * 8  # 06–16 PV
    initial_soc = 95.0  # HIGH SoC — mirrors user's complaint

    plan = strat._solve_lp(pw, consumption, solar, initial_soc)

    print(
        f"\ninitial_soc = {initial_soc}%, max_soc = {strat.max_soc}%, "
        f"capacity = {strat.battery_capacity} kWh"
    )
    print(
        f"\n{'h':>2} {'spot':>5} {'eff':>5} {'cons':>5} {'solar':>5} "
        f"{'ch':>5} {'dis':>5} {'buy':>5} {'sell':>5} {'soc%':>5}  action"
    )
    for h, e in enumerate(plan):
        print(
            f"{h:>2} {pw.spot[h]:>5.2f} {pw.effective[h]:>5.2f} "
            f"{consumption[h]:>5.2f} {solar[h]:>5.2f} "
            f"{e['lp_charge_kwh']:>5.2f} {e['lp_discharge_kwh']:>5.2f} "
            f"{e['lp_grid_buy_kwh']:>5.2f} {e['lp_grid_sell_kwh']:>5.2f} "
            f"{e['lp_soc_after']:>5.1f}  {e['action']}"
        )
        if e['lp_charge_kwh'] > 0.01 or e['lp_discharge_kwh'] > 0.01:
            print(f"      reason: {e['reason']}")

    # Headroom from initial_soc=95 to max_soc=100 is only 5%
    # = 0.05 * 24.06 = 1.20 kWh raw capacity, and with eta_c=0.9
    # the LP can charge at most 1.20 / 0.9 = 1.34 kWh from the grid.
    total_ch = sum(e['lp_charge_kwh'] for e in plan)
    total_buy = sum(e['lp_grid_buy_kwh'] for e in plan)
    total_dis = sum(e['lp_discharge_kwh'] for e in plan)
    print(f"\nTotal charge:    {total_ch:.2f} kWh")
    print(f"Total grid buy:  {total_buy:.2f} kWh")
    print(f"Total discharge: {total_dis:.2f} kWh")
    headroom_kwh = (strat.max_soc - initial_soc) / 100.0 * strat.battery_capacity
    print(f"Headroom:        {headroom_kwh:.2f} kWh raw")
    print(
        f"Max charge possible: {headroom_kwh / strat.charge_efficiency:.2f} "
        f"kWh from grid+solar"
    )
