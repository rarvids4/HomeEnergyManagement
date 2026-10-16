"""Tests for the buy-price contract and the expensive-day battery reserve.

Covers:
  - ``buy_price_floor_sek`` / ``buy_price_mode: monthly_average`` in the
    price analyzer (grid tariffs still added on top, spot left raw)
  - ``battery_reserve_soc`` / ``battery_reserve_spot_threshold_sek`` in
    the battery LP, and the min-SoC write that makes the inverter hold it
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from custom_components.home_energy_management.action_builder import ActionBuilder
from custom_components.home_energy_management.battery_strategy import (
    BatteryStrategy,
)
from custom_components.home_energy_management.optimizer import Optimizer
from custom_components.home_energy_management.price_analysis import (
    PriceAnalyzer,
    PriceWindow,
)

PEAK_TARIFF = 1.40
OFFPEAK_TARIFF = 0.831

TARIFF_PARAMS = {
    "planning_horizon_hours": 24,
    "grid_tariff_peak_sek": PEAK_TARIFF,
    "grid_tariff_offpeak_sek": OFFPEAK_TARIFF,
    "grid_tariff_peak_start": 6,
    "grid_tariff_peak_end": 22,
}

# Windy night with negative prices, expensive evening.
SPOT = [
    -0.10, -0.05, 0.02, 0.05, 0.08, 0.12,   # 00–06 off-peak
    0.25, 0.30, 0.35, 0.40, 0.30, 0.20,     # 06–12
    0.15, 0.12, 0.18, 0.25, 0.45, 0.80,     # 12–18
    1.20, 1.40, 1.10, 0.70,                 # 18–22
    0.30, 0.15,                             # 22–24 off-peak
]


def _tariff(hour: int) -> float:
    return PEAK_TARIFF if 6 <= hour < 22 else OFFPEAK_TARIFF


def _window(params: dict, spot: list[float] = SPOT) -> PriceWindow:
    return PriceAnalyzer(params).build_price_window({"today": spot}, 0)


# ------------------------------------------------------------------
# Buy price
# ------------------------------------------------------------------

def test_default_contract_is_hourly_spot_plus_tariff():
    pw = _window(TARIFF_PARAMS)
    assert pw.effective == pytest.approx(
        [s + _tariff(h) for h, s in enumerate(SPOT)]
    )
    assert pw.spot == SPOT


def test_floor_lifts_cheap_hours_only():
    pw = _window({**TARIFF_PARAMS, "buy_price_floor_sek": 0.49})
    assert pw.effective == pytest.approx(
        [max(s, 0.49) + _tariff(h) for h, s in enumerate(SPOT)]
    )
    # Spot stays raw — it still drives the sell price and export block.
    assert pw.spot == SPOT


def test_monthly_average_is_flat_plus_tariff():
    pw = _window({
        **TARIFF_PARAMS,
        "buy_price_mode": "monthly_average",
        "buy_price_monthly_avg_sek": 0.80,
        "buy_price_floor_sek": 0.49,
    })
    assert pw.effective == pytest.approx([0.80 + _tariff(h) for h in range(24)])
    assert pw.spread == pytest.approx(PEAK_TARIFF - OFFPEAK_TARIFF)


def test_monthly_average_never_below_floor():
    pw = _window({
        **TARIFF_PARAMS,
        "buy_price_mode": "monthly_average",
        "buy_price_monthly_avg_sek": 0.30,
        "buy_price_floor_sek": 0.49,
    })
    assert pw.effective == pytest.approx([0.49 + _tariff(h) for h in range(24)])


def test_monthly_average_estimated_from_known_prices_when_unset():
    tomorrow = [1.0] * 24
    analyzer = PriceAnalyzer({**TARIFF_PARAMS, "buy_price_mode": "monthly_average"})
    pw = analyzer.build_price_window({"today": SPOT, "tomorrow": tomorrow}, 0)
    estimate = (sum(SPOT) + sum(tomorrow)) / 48
    assert pw.effective == pytest.approx([estimate + _tariff(h) for h in range(24)])


def test_extended_ev_entries_use_the_same_buy_price():
    analyzer = PriceAnalyzer({
        **TARIFF_PARAMS,
        "buy_price_mode": "monthly_average",
        "buy_price_monthly_avg_sek": 0.80,
    })
    entries = analyzer.build_extended_plan_entries(
        {"today": SPOT, "tomorrow": [2.0] * 24}, 0, 24,
    )
    assert len(entries) == 24
    for e in entries:
        assert e["price"] == pytest.approx(0.80 + _tariff(e["hour"]))
        assert e["spot_price"] == 2.0


def test_import_price_stays_positive_at_negative_spot():
    """The surplus controller must not see 'paid to import' under a floor."""
    outputs = {"sungrow": {}}
    prices = {"today": SPOT}

    hourly = Optimizer(dict(TARIFF_PARAMS), outputs)
    assert hourly.buy_energy_price(-0.10, prices) == -0.10

    floored = Optimizer({**TARIFF_PARAMS, "buy_price_floor_sek": 0.49}, outputs)
    assert floored.buy_energy_price(-0.10, prices) == 0.49

    monthly = Optimizer(
        {**TARIFF_PARAMS, "buy_price_mode": "monthly_average"}, outputs,
    )
    monthly.buy_price_monthly_avg = 0.75   # as set from an input helper
    assert monthly.buy_energy_price(-0.10, prices) == 0.75


# ------------------------------------------------------------------
# Battery behaviour under the monthly-average contract
# ------------------------------------------------------------------

OUTPUTS = {
    "sungrow": {
        "min_soc": 10,
        "max_soc": 100,
        "capacity_kwh": 24.06,
        "set_forced_power": {"max": 5000},
        "set_min_soc": {
            "service": "input_number.set_value",
            "entity_id": "input_number.test_min_soc",
        },
    }
}

CONTRACT_PARAMS = {
    **TARIFF_PARAMS,
    "enable_battery_control": True,
    "buy_price_mode": "monthly_average",
    "buy_price_monthly_avg_sek": 0.60,
    "buy_price_floor_sek": 0.49,
}


def test_solar_surplus_is_stored_not_sold():
    """Flat buy price ≫ sell price → the LP keeps solar for the house."""
    strat = BatteryStrategy(CONTRACT_PARAMS, OUTPUTS)
    pw = _window(CONTRACT_PARAMS)
    consumption = [1.0] * 24
    solar = [0.0] * 9 + [3.0] * 6 + [0.0] * 9   # 09–15, 12 kWh surplus

    plan = strat.plan_battery(pw, consumption, 20.0, predicted_solar=solar)

    assert sum(e["lp_grid_sell_kwh"] for e in plan) == pytest.approx(0.0, abs=0.01)
    solar_charge = sum(e["lp_charge_kwh"] for e in plan[9:15])
    assert solar_charge == pytest.approx(12.0, abs=0.05)


def test_reserve_disabled_by_default():
    strat = BatteryStrategy(CONTRACT_PARAMS, OUTPUTS)
    strat.plan_battery(_window(CONTRACT_PARAMS), [1.0] * 24, 60.0)
    assert strat.active_min_soc == 10


def test_reserve_inactive_on_a_normal_day():
    params = {
        **CONTRACT_PARAMS,
        "battery_reserve_soc": 40,
        "battery_reserve_spot_threshold_sek": 2.0,   # SPOT peaks at 1.40
    }
    strat = BatteryStrategy(params, OUTPUTS)
    plan = strat.plan_battery(_window(params), [1.5] * 24, 60.0)

    assert strat.active_min_soc == 10
    assert min(e["lp_soc_after"] for e in plan) < 40


def test_reserve_holds_soc_on_an_expensive_day():
    params = {
        **CONTRACT_PARAMS,
        "battery_reserve_soc": 40,
        "battery_reserve_spot_threshold_sek": 1.0,   # SPOT peaks at 1.40
    }
    strat = BatteryStrategy(params, OUTPUTS)
    plan = strat.plan_battery(_window(params), [1.5] * 24, 60.0)

    assert strat.active_min_soc == 40
    assert all("lp_soc_after" in e for e in plan), "LP must solve, not fall back"
    assert min(e["lp_soc_after"] for e in plan) >= 40 - 0.1
    # The battery still cycles above the reserve.
    assert sum(e["lp_discharge_kwh"] for e in plan) > 1.0


def test_reserve_below_current_soc_never_forces_a_charge():
    """Starting under the reserve: hold what we have, stay feasible."""
    params = {
        **CONTRACT_PARAMS,
        "battery_reserve_soc": 40,
        "battery_reserve_spot_threshold_sek": 1.0,
    }
    strat = BatteryStrategy(params, OUTPUTS)
    plan = strat.plan_battery(_window(params), [1.5] * 24, 25.0)

    assert strat.active_min_soc == 25
    assert all("lp_soc_after" in e for e in plan), "LP must solve, not fall back"
    assert min(e["lp_soc_after"] for e in plan) >= 25 - 0.1


# ------------------------------------------------------------------
# Inverter min-SoC write
# ------------------------------------------------------------------

def _min_soc_writes(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a.get("entity_id") == "input_number.test_min_soc"]


def test_action_builder_writes_min_soc_only_when_given_a_setpoint():
    ab = ActionBuilder({"enable_charger_control": False}, OUTPUTS)

    without = ab.build_immediate_actions("self_consumption", ev_connected=False)
    assert _min_soc_writes(without) == []

    with_setpoint = ab.build_immediate_actions(
        "self_consumption", ev_connected=False, min_soc_setpoint=40.7,
    )
    assert _min_soc_writes(with_setpoint) == [{
        "service": "input_number.set_value",
        "entity_id": "input_number.test_min_soc",
        "data": {"value": 40},
    }]


@pytest.fixture
def midnight():
    with patch(
        "custom_components.home_energy_management.optimizer.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = datetime(2025, 1, 6, 0, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        yield


def _optimize(params: dict) -> list[dict]:
    result = Optimizer(params, OUTPUTS).optimize(
        prices={"today": SPOT, "currency": "SEK"},
        predicted_consumption=[1.5] * 24,
        battery_soc=60.0,
        ev_connected=False,
    )
    return _min_soc_writes(result["immediate_actions"])


def test_optimizer_leaves_inverter_min_soc_alone_without_a_reserve(midnight):
    assert _optimize(CONTRACT_PARAMS) == []


def test_optimizer_sets_reserve_on_expensive_day_and_restores_after(midnight):
    params = {**CONTRACT_PARAMS, "battery_reserve_soc": 40}

    expensive = _optimize({**params, "battery_reserve_spot_threshold_sek": 1.0})
    assert [a["data"]["value"] for a in expensive] == [40]

    normal = _optimize({**params, "battery_reserve_spot_threshold_sek": 2.0})
    assert [a["data"]["value"] for a in normal] == [10]


def test_normal_day_restores_configured_min_soc_below_hard_floor(midnight):
    """Inverter at 4 % stays at 4 % on normal days; planning still uses 8 %."""
    outputs = {"sungrow": {**OUTPUTS["sungrow"], "min_soc": 4}}
    params = {**CONTRACT_PARAMS, "battery_reserve_soc": 20,
              "battery_reserve_spot_threshold_sek": 3.0}   # SPOT peaks at 1.40
    opt = Optimizer(params, outputs)
    result = opt.optimize(
        prices={"today": SPOT, "currency": "SEK"},
        predicted_consumption=[1.5] * 24, battery_soc=60.0, ev_connected=False,
    )
    assert [a["data"]["value"] for a in _min_soc_writes(result["immediate_actions"])] == [4]
    assert opt._battery.active_min_soc == 8

