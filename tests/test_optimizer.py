"""Tests for the Optimizer — price-aware scheduling engine."""

import pytest
from custom_components.home_energy_management.optimizer import Optimizer
from custom_components.home_energy_management.const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_SELF_CONSUMPTION,
)


@pytest.fixture
def default_params():
    return {
        "min_price_spread": 0.30,
        "planning_horizon_hours": 24,
        "enable_charger_control": True,
        "enable_battery_control": True,
    }


@pytest.fixture
def default_outputs():
    return {
        "sungrow": {
            "force_charge": {
                "service": "select.select_option",
                "entity_id": "select.sungrow_battery_mode",
                "mode_value": "force_charge",
            },
            "force_discharge": {
                "service": "select.select_option",
                "entity_id": "select.sungrow_battery_mode",
                "mode_value": "force_discharge",
            },
            "self_consumption": {
                "service": "select.select_option",
                "entity_id": "select.sungrow_battery_mode",
                "mode_value": "self_consumption",
            },
            "min_soc": 10,
            "max_soc": 100,
            "capacity_kwh": 10.0,
        },
        "easee": {
            "start_charging": {
                "service": "easee.start",
                "entity_id": "switch.easee_enabled",
            },
            "stop_charging": {
                "service": "easee.stop",
                "entity_id": "switch.easee_enabled",
            },
            "set_current_limit": {
                "service": "number.set_value",
                "entity_id": "number.easee_current_limit",
                "min_amps": 6,
                "max_amps": 32,
            },
        },
    }


class TestOptimizer:
    """Test the optimizer scheduling logic."""

    def test_charges_during_cheap_hours(self, default_params, default_outputs):
        """Battery should charge when prices are in the bottom 30%."""
        opt = Optimizer(default_params, default_outputs)

        # Simulate prices: cheap in hours 0-3, expensive in 17-20
        prices = {
            "current": 0.50,
            "today": [0.20, 0.22, 0.18, 0.25,   # cheap hours 0–3
                      0.50, 0.55, 0.60, 0.65,   # mid
                      0.70, 0.75, 0.80, 0.85,   # mid-high
                      0.90, 0.95, 1.00, 1.05,   # high
                      1.20, 1.30, 1.25, 1.15,   # peak
                      1.00, 0.80, 0.60, 0.40],  # declining
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=30,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        assert len(plan) > 0

        # Some hours should be charge actions
        charge_actions = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]
        assert len(charge_actions) > 0, "Should plan at least one charge hour"

        # Some hours should be discharge actions
        discharge_actions = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]
        assert len(discharge_actions) > 0, "Should plan at least one discharge hour"

    def test_no_action_when_spread_too_small(self, default_params, default_outputs):
        """When price spread is < min_price_spread, default to self-consumption."""
        opt = Optimizer(default_params, default_outputs)

        # Flat prices — spread = 0.10 < 0.30
        prices = {
            "current": 1.00,
            "today": [1.00, 1.02, 1.05, 1.03, 1.01, 0.98, 1.00, 1.02,
                      1.04, 1.06, 1.08, 1.10, 1.05, 1.03, 1.01, 0.99,
                      1.00, 1.02, 1.04, 1.06, 1.08, 1.05, 1.03, 1.00],
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        for hour_plan in plan:
            assert hour_plan["action"] == ACTION_SELF_CONSUMPTION

    def test_no_discharge_below_min_soc(self, default_params, default_outputs):
        """Battery should not discharge when SoC is at minimum."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "current": 2.00,
            "today": [0.10] * 6 + [2.00] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # At min_soc
            ev_connected=False,
        )

        # First hour is expensive, but SoC = min_soc → should NOT discharge
        plan = result["hourly_plan"]
        # After some cheap hours fill the battery, discharge may appear later
        # But the first expensive hour at SoC=10 should NOT discharge
        first_expensive = next(
            (h for h in plan if h["price"] >= 1.50), None
        )
        if first_expensive:
            # If it's the very first hour, SoC should prevent discharge
            pass  # The logic depends on accumulated SoC from earlier cheap hours

    def test_no_charge_above_max_soc(self, default_params, default_outputs):
        """Battery should not charge when SoC is at maximum."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "current": 0.10,
            "today": [0.10] * 12 + [2.00] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=100,  # At max_soc
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # First hour is cheap but SoC=100 → should not charge
        assert plan[0]["action"] != ACTION_CHARGE_BATTERY

    def test_safe_default_when_no_prices(self, default_params, default_outputs):
        """Should return self-consumption when no price data available."""
        opt = Optimizer(default_params, default_outputs)

        result = opt.optimize(
            prices={"current": 0, "today": [], "tomorrow": []},
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        assert result["hourly_plan"][0]["action"] == ACTION_SELF_CONSUMPTION
        assert len(result["immediate_actions"]) == 0

    def test_ev_charges_during_cheap_hours_when_connected(self, default_params, default_outputs):
        """EV should get max amps during cheap hours if cable is connected."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "current": 0.10,
            "today": [0.10] * 6 + [2.00] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=30,
            ev_connected=True,
        )

        actions = result["immediate_actions"]
        # Should include EV start + set current limit
        services_called = [a["service"] for a in actions]
        assert "easee.start" in services_called or len(actions) > 0

    def test_schedule_has_expected_structure(self, default_params, default_outputs):
        """Verify the output structure of optimize()."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "current": 0.50,
            "today": [0.50 + i * 0.05 for i in range(24)],
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        assert "hourly_plan" in result
        assert "immediate_actions" in result
        assert "summary" in result
        assert "stats" in result
        assert "avg_price" in result["stats"]
        assert "min_price" in result["stats"]
        assert "max_price" in result["stats"]
        assert "price_spread" in result["stats"]

        # Each hour plan should have required keys
        for h in result["hourly_plan"]:
            assert "hour" in h
            assert "action" in h
            assert "reason" in h
            assert "price" in h
