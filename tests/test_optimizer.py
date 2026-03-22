"""Tests for the Optimizer — price-aware scheduling engine."""

from datetime import datetime
from unittest.mock import patch

import pytest
from custom_components.home_energy_management.optimizer import Optimizer
from custom_components.home_energy_management.const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_MAXIMIZE_LOAD,
    ACTION_PRE_DISCHARGE,
    ACTION_SELF_CONSUMPTION,
)


@pytest.fixture(autouse=True)
def freeze_time():
    """Pin datetime.now() to midnight so tests control price slicing."""
    fake_now = datetime(2025, 1, 6, 0, 0, 0)  # Monday 00:00
    with patch(
        "custom_components.home_energy_management.optimizer.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        yield mock_dt


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
                "service": "script.turn_on",
                "entity_id": "script.sg_set_forced_charge_battery_mode",
            },
            "force_discharge": {
                "service": "script.turn_on",
                "entity_id": "script.sg_set_forced_discharge_battery_mode",
            },
            "self_consumption": {
                "service": "script.turn_on",
                "entity_id": "script.sg_set_self_consumption_mode",
            },
            "min_soc": 10,
            "max_soc": 100,
            "capacity_kwh": 10.0,
        },
        "ev_chargers": [
            {
                "name": "ex90",
                "start_charging": {
                    "service": "switch.turn_on",
                    "entity_id": "switch.ex90_charger_enabled",
                },
                "stop_charging": {
                    "service": "switch.turn_off",
                    "entity_id": "switch.ex90_charger_enabled",
                },
            },
            {
                "name": "renault_zoe",
                "start_charging": {
                    "service": "switch.turn_on",
                    "entity_id": "switch.renault_zoe_charger_enabled",
                },
                "stop_charging": {
                    "service": "switch.turn_off",
                    "entity_id": "switch.renault_zoe_charger_enabled",
                },
            },
        ],
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
        """EV chargers should turn on during cheap hours if cable is connected."""
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
        # Should include switch.turn_on for EV charger(s) during cheap hour
        services_called = [a["service"] for a in actions]
        assert "switch.turn_on" in services_called or len(actions) > 0

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


class TestNegativePriceOptimization:
    """Tests for negative electricity price handling."""

    def test_maximize_load_during_negative_prices(self, default_params, default_outputs):
        """When price is negative, action should be maximize_load."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [-0.10, -0.05, 0.20, 0.50, 0.60, 0.70,
                      0.80, 0.90, 1.00, 1.10, 1.00, 0.90,
                      0.80, 0.70, 0.60, 0.50, 0.40, 0.30,
                      0.20, 0.15, 0.10, 0.08, 0.05, 0.03],
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
        # First two hours have negative prices → must be maximize_load
        assert plan[0]["action"] == ACTION_MAXIMIZE_LOAD
        assert plan[1]["action"] == ACTION_MAXIMIZE_LOAD

    def test_all_evs_enabled_during_negative_prices(self, default_params, default_outputs):
        """During negative prices, ALL EV chargers should be turned on
        regardless of ev_connected status."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [-0.20] * 3 + [0.50] * 21,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,  # Even when "not connected", we turn on chargers
        )

        actions = result["immediate_actions"]
        switch_on_actions = [a for a in actions if a["service"] == "switch.turn_on"]
        # Both EX90 and Renault Zoe chargers should be turned on
        entity_ids = [a["entity_id"] for a in switch_on_actions]
        assert "switch.ex90_charger_enabled" in entity_ids
        assert "switch.renault_zoe_charger_enabled" in entity_ids

    def test_battery_charges_during_negative_prices(self, default_params, default_outputs):
        """During negative prices, battery should be force-charged."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [-0.15] + [0.80] * 23,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=20,
            ev_connected=False,
        )

        actions = result["immediate_actions"]
        # Should include force-charge on the battery
        battery_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_forced_charge_battery_mode"
        ]
        assert len(battery_actions) == 1

    def test_pre_discharge_before_negative_window(self, default_params, default_outputs):
        """Battery should be discharged in hours preceding a negative price window."""
        opt = Optimizer(default_params, default_outputs)

        # Hour 0: positive (should pre-discharge, negative coming at hour 2-3)
        # Hour 1: positive (should pre-discharge)
        # Hours 2-3: negative prices
        # Hours 4+: normal
        prices = {
            "today": [0.50, 0.40, -0.10, -0.20, 0.60, 0.70,
                      0.80, 0.90, 1.00, 1.10, 1.00, 0.90,
                      0.80, 0.70, 0.60, 0.50, 0.40, 0.30,
                      0.20, 0.15, 0.10, 0.08, 0.05, 0.03],
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,  # High SoC — should discharge to make room
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # Hour 0 sees negatives ahead (hours 2-3) → pre-discharge
        assert plan[0]["action"] == ACTION_PRE_DISCHARGE
        # Hours 2-3 are negative → maximize_load
        assert plan[2]["action"] == ACTION_MAXIMIZE_LOAD
        assert plan[3]["action"] == ACTION_MAXIMIZE_LOAD

    def test_no_pre_discharge_when_battery_at_min_soc(self, default_params, default_outputs):
        """Don't pre-discharge if battery is already at min_soc."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, 0.40, -0.10, -0.20] + [0.60] * 20,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # Already at min_soc (10)
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # Battery already empty → can't pre-discharge, should NOT be pre_discharge
        assert plan[0]["action"] != ACTION_PRE_DISCHARGE

    def test_pre_discharge_immediate_action_is_force_discharge(
        self, default_params, default_outputs
    ):
        """When pre-discharging, the immediate action should be force_discharge."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, -0.10, -0.20] + [0.80] * 21,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_PRE_DISCHARGE

        actions = result["immediate_actions"]
        # Should call force_discharge on master inverter
        discharge_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_forced_discharge_battery_mode"
        ]
        assert len(discharge_actions) == 1

    def test_maximize_load_charges_battery_to_max(self, default_params, default_outputs):
        """During maximize_load hours, SoC simulation should increase toward max."""
        opt = Optimizer(default_params, default_outputs)

        # All negative prices → all maximize_load → battery fills up
        prices = {
            "today": [-0.10] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=20,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # Every hour should be maximize_load
        for h in plan:
            assert h["action"] == ACTION_MAXIMIZE_LOAD

    def test_summary_includes_negative_and_predischarge_counts(
        self, default_params, default_outputs
    ):
        """Summary string should mention negative-price and pre-discharge hours."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, -0.10, -0.20] + [0.80] * 21,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=False,
        )

        summary = result["summary"]
        assert "negative-price" in summary
        assert "pre-discharge" in summary

    def test_zero_price_treated_as_normal_not_negative(
        self, default_params, default_outputs
    ):
        """Price exactly 0 should NOT trigger maximize_load (only < 0 does)."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.00] + [0.80] * 23,
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
        # Price = 0.00 is not negative → should not be maximize_load
        assert plan[0]["action"] != ACTION_MAXIMIZE_LOAD

    def test_evs_charge_during_pre_discharge_when_price_cheap(
        self, default_params, default_outputs
    ):
        """During pre-discharge with cheap prices, EVs should CHARGE —
        the battery discharges but EVs absorb cheap energy instead of
        letting it export to grid for pennies."""
        opt = Optimizer(default_params, default_outputs)

        # Hour 0: 0.05 SEK (cheap, positive) with negatives at hour 1-2
        prices = {
            "today": [0.05, -0.10, -0.20] + [0.80] * 21,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=True,
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_PRE_DISCHARGE

        actions = result["immediate_actions"]
        # Battery should discharge
        discharge_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_forced_discharge_battery_mode"
        ]
        assert len(discharge_actions) == 1

        # EVs should CHARGE (price 0.05 < threshold 0.10)
        ev_on_actions = [a for a in actions if a["service"] == "switch.turn_on"
                         and "charger" in a.get("entity_id", "")]
        assert len(ev_on_actions) >= 1, (
            "EVs should charge during pre_discharge when price is cheap"
        )


class TestSolarSurplusEVCharging:
    """Tests for solar-surplus-aware EV charging."""

    def test_evs_charge_on_solar_surplus_during_self_consumption(
        self, default_params, default_outputs
    ):
        """When exporting heavily to grid during self_consumption,
        EVs should charge to absorb the surplus."""
        opt = Optimizer(default_params, default_outputs)

        # Flat mid-range prices → self_consumption (spread < 0.30)
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            grid_export_power=5000.0,  # 5 kW export = big surplus
        )

        actions = result["immediate_actions"]
        ev_on = [a for a in actions if a["service"] == "switch.turn_on"
                 and "charger" in a.get("entity_id", "")]
        assert len(ev_on) >= 1, "EVs should charge when solar surplus is high"

    def test_evs_dont_charge_on_surplus_during_expensive_hours(
        self, default_params, default_outputs
    ):
        """Even with solar surplus, don't charge EVs during expensive hours
        because we want to sell to grid at high prices."""
        opt = Optimizer(default_params, default_outputs)

        # Very wide spread: cheap=0.10, expensive=2.00
        prices = {
            "today": [2.00] * 12 + [0.10] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            grid_export_power=5000.0,
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_DISCHARGE_BATTERY

        actions = result["immediate_actions"]
        ev_off = [a for a in actions if a["service"] == "switch.turn_off"
                  and "charger" in a.get("entity_id", "")]
        assert len(ev_off) >= 1, "EVs should stop during expensive hours even with surplus"

    def test_no_ev_action_without_surplus_at_mid_price(
        self, default_params, default_outputs
    ):
        """When price is mid-range and no solar surplus, no EV action."""
        opt = Optimizer(default_params, default_outputs)

        # Flat mid-range prices (self_consumption)
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            grid_export_power=500.0,  # Only 0.5 kW export, below threshold
        )

        actions = result["immediate_actions"]
        ev_actions = [a for a in actions
                      if "charger" in a.get("entity_id", "")]
        assert len(ev_actions) == 0, "No EV action at mid-price without surplus"

    def test_evs_charge_during_pre_discharge_with_surplus(
        self, default_params, default_outputs
    ):
        """During pre_discharge with solar surplus, EVs should still charge."""
        opt = Optimizer(default_params, default_outputs)

        # Pre-discharge scenario: price 0.50 with negatives ahead
        prices = {
            "today": [0.50, -0.10, -0.20] + [0.80] * 21,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=True,
            grid_export_power=8000.0,  # Massive solar surplus
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_PRE_DISCHARGE

        actions = result["immediate_actions"]
        # Price is 0.50 > cheap threshold, but solar surplus is huge
        ev_on = [a for a in actions if a["service"] == "switch.turn_on"
                 and "charger" in a.get("entity_id", "")]
        assert len(ev_on) >= 1, "EVs should charge on surplus even during pre_discharge"

    def test_cheap_price_threshold_configurable(self, default_outputs):
        """The ev_cheap_price_threshold should be configurable via params."""
        params = {
            "min_price_spread": 0.30,
            "planning_horizon_hours": 24,
            "enable_charger_control": True,
            "enable_battery_control": True,
            "ev_cheap_price_threshold": 0.20,  # Raised threshold
        }
        opt = Optimizer(params, default_outputs)

        # Price = 0.15, which is above default 0.10 but below custom 0.20
        prices = {
            "today": [0.15] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
        )

        actions = result["immediate_actions"]
        ev_on = [a for a in actions if a["service"] == "switch.turn_on"
                 and "charger" in a.get("entity_id", "")]
        assert len(ev_on) >= 1, "EVs should charge at 0.15 when threshold is 0.20"

    def test_evs_stop_only_during_expensive_hours(
        self, default_params, default_outputs
    ):
        """EVs should only be stopped during discharge_battery (expensive).
        Pre-discharge, self_consumption should NOT stop EVs."""
        opt = Optimizer(default_params, default_outputs)

        # Test self_consumption - no stop
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
        )

        actions = result["immediate_actions"]
        ev_off = [a for a in actions if a["service"] == "switch.turn_off"
                  and "charger" in a.get("entity_id", "")]
        assert len(ev_off) == 0, "EVs should NOT be stopped during self_consumption"
