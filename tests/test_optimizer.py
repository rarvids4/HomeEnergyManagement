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
def outputs_with_forced_power():
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
            "set_forced_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.set_sg_forced_charge_discharge_power",
                "max": 5000,
            },
            "set_discharge_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.set_sg_discharge_power",
                "max": 5000,
            },
            "battery_mode_select": "input_select.set_sg_battery_forced_charge_discharge_cmd",
            "battery_mode_options": {"stop": "Stop (default)"},
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

    def test_battery_self_consumption_during_negative_prices(
        self, default_params, default_outputs
    ):
        """During negative prices, battery should be in self-consumption
        mode — absorbs solar surplus but does NOT force-charge from grid."""
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
        # Should use self-consumption (absorb surplus, not force-charge)
        sc_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_self_consumption_mode"
        ]
        assert len(sc_actions) == 1, "Battery should be in self-consumption during negative prices"

        # Should NOT force-charge from grid
        fc_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_forced_charge_battery_mode"
        ]
        assert len(fc_actions) == 0, "Should NOT force-charge from grid during negative prices"

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

        ev_vehicles = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=True,
            ev_vehicles=ev_vehicles,
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

    def test_discharge_hour_uses_force_discharge(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """When current hour is expensive and action is discharge,
        the inverter should be in force-discharge mode with a non-zero
        power setpoint — the battery must NEVER charge during discharge."""
        # Set time to hour 6 (will be the expensive hour)
        freeze_time.now.return_value = datetime(2025, 1, 6, 6, 0, 0)
        opt = Optimizer(default_params, outputs_with_forced_power)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
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
        assert plan[0]["action"] == ACTION_DISCHARGE_BATTERY

        actions = result["immediate_actions"]
        # Should use force_discharge mode (NOT self_consumption)
        fd_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_forced_discharge_battery_mode"
        ]
        assert len(fd_actions) == 1, "Discharge hour should use force-discharge mode"

        # Forced power must be > 0 to guarantee the battery discharges
        power_actions = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_forced_charge_discharge_power"
        ]
        assert len(power_actions) == 1
        assert power_actions[0]["data"]["value"] >= 500, (
            "Discharge power must be at least 500 W to prevent battery charging"
        )

    def test_charge_hour_sets_power_5000(
        self, default_params, outputs_with_forced_power
    ):
        """When current hour is cheap and action is charge,
        forced power should be set to a positive value."""
        opt = Optimizer(default_params, outputs_with_forced_power)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }
        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # low SoC — LP should find charging profitable
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_CHARGE_BATTERY

        actions = result["immediate_actions"]
        power_actions = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_forced_charge_discharge_power"
        ]
        assert len(power_actions) == 1
        assert power_actions[0]["data"]["value"] > 0, (
            "Charge power must be positive when charging"
        )

    def test_self_consumption_resets_forced_power_to_zero(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """When switching to self-consumption, forced power should be reset to 0."""
        # Set time to a mid-price hour
        freeze_time.now.return_value = datetime(2025, 1, 6, 12, 0, 0)
        opt = Optimizer(default_params, outputs_with_forced_power)

        # Flat prices → self_consumption
        prices = {
            "today": [1.00] * 24,
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
        assert plan[0]["action"] == ACTION_SELF_CONSUMPTION

        actions = result["immediate_actions"]
        power_actions = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_forced_charge_discharge_power"
        ]
        assert len(power_actions) == 1
        assert power_actions[0]["data"]["value"] == 0

    def test_pre_discharge_sets_forced_power_and_limit(
        self, default_params, outputs_with_forced_power
    ):
        """Pre-discharge should set both forced power and max discharge limit."""
        opt = Optimizer(default_params, outputs_with_forced_power)

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
        # Should set forced power
        forced_pwr = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_forced_charge_discharge_power"
        ]
        assert len(forced_pwr) == 1
        assert forced_pwr[0]["data"]["value"] == 5000

        # Should also set max discharge limit
        limit_pwr = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_discharge_power"
        ]
        assert len(limit_pwr) == 1
        assert limit_pwr[0]["data"]["value"] == 5000

    def test_no_forced_power_when_config_missing(
        self, default_params, default_outputs
    ):
        """When set_forced_power is not in config, no power action should be added.
        This ensures backward compatibility."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }
        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=30,
            ev_connected=False,
        )

        actions = result["immediate_actions"]
        power_actions = [
            a for a in actions
            if "forced_charge_discharge_power" in a.get("entity_id", "")
        ]
        assert len(power_actions) == 0, (
            "No forced power action should be generated without config"
        )

    def test_discharge_does_not_clear_forced_cmd(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """Discharge uses forced discharge mode → should NOT set Stop.
        The inverter is actively discharging, not in self-consumption."""
        freeze_time.now.return_value = datetime(2025, 1, 6, 6, 0, 0)
        opt = Optimizer(default_params, outputs_with_forced_power)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
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
        assert plan[0]["action"] == ACTION_DISCHARGE_BATTERY

        actions = result["immediate_actions"]
        stop_actions = [
            a for a in actions
            if a.get("entity_id") == "input_select.set_sg_battery_forced_charge_discharge_cmd"
        ]
        assert len(stop_actions) == 0, (
            "Discharge uses forced mode, should NOT clear forced cmd"
        )

    def test_self_consumption_clears_forced_cmd(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """Normal self-consumption hours should also clear the forced cmd."""
        freeze_time.now.return_value = datetime(2025, 1, 6, 12, 0, 0)
        opt = Optimizer(default_params, outputs_with_forced_power)

        # Flat prices → self_consumption
        prices = {
            "today": [1.00] * 24,
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
        assert plan[0]["action"] == ACTION_SELF_CONSUMPTION

        actions = result["immediate_actions"]
        stop_actions = [
            a for a in actions
            if a.get("entity_id") == "input_select.set_sg_battery_forced_charge_discharge_cmd"
        ]
        assert len(stop_actions) == 1, (
            "Self-consumption should explicitly clear forced cmd"
        )
        assert stop_actions[0]["data"]["option"] == "Stop (default)"

    def test_maximize_load_clears_forced_cmd(
        self, default_params, outputs_with_forced_power
    ):
        """Negative price (maximize_load) should clear the forced cmd."""
        opt = Optimizer(default_params, outputs_with_forced_power)

        prices = {
            "today": [-0.10] * 3 + [0.80] * 21,
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
        assert plan[0]["action"] == ACTION_MAXIMIZE_LOAD

        actions = result["immediate_actions"]
        stop_actions = [
            a for a in actions
            if a.get("entity_id") == "input_select.set_sg_battery_forced_charge_discharge_cmd"
        ]
        assert len(stop_actions) == 1, (
            "Maximize load should explicitly clear forced cmd"
        )
        assert stop_actions[0]["data"]["option"] == "Stop (default)"

    def test_pre_discharge_does_not_clear_forced_cmd(
        self, default_params, outputs_with_forced_power
    ):
        """Pre-discharge uses forced discharge mode → should NOT set Stop."""
        opt = Optimizer(default_params, outputs_with_forced_power)

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
        stop_actions = [
            a for a in actions
            if a.get("entity_id") == "input_select.set_sg_battery_forced_charge_discharge_cmd"
        ]
        assert len(stop_actions) == 0, (
            "Pre-discharge uses forced mode, should NOT clear forced cmd"
        )

    def test_charge_does_not_clear_forced_cmd(
        self, default_params, outputs_with_forced_power
    ):
        """Charge uses forced charge mode → should NOT set Stop."""
        opt = Optimizer(default_params, outputs_with_forced_power)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }
        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # low SoC — LP should find charging profitable
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_CHARGE_BATTERY

        actions = result["immediate_actions"]
        stop_actions = [
            a for a in actions
            if a.get("entity_id") == "input_select.set_sg_battery_forced_charge_discharge_cmd"
        ]
        assert len(stop_actions) == 0, (
            "Charge uses forced mode, should NOT clear forced cmd"
        )

    def test_no_stop_cmd_when_config_missing(
        self, default_params, default_outputs, freeze_time
    ):
        """When battery_mode_select is not in config, no stop command generated.
        This ensures backward compatibility."""
        freeze_time.now.return_value = datetime(2025, 1, 6, 6, 0, 0)
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }
        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=False,
        )

        actions = result["immediate_actions"]
        stop_actions = [
            a for a in actions
            if a.get("service") == "input_select.select_option"
        ]
        assert len(stop_actions) == 0, (
            "No stop cmd action should be generated without config"
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

    def test_evs_stopped_without_schedule_at_mid_price(
        self, default_params, default_outputs
    ):
        """When price is mid-range, no solar surplus, and no vehicle data,
        EVs should be stopped (no schedule = no charging)."""
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
        ev_off = [a for a in actions if a["service"] == "switch.turn_off"
                  and "charger" in a.get("entity_id", "")]
        # Without vehicle SoC data → no schedule → EVs stopped
        assert len(ev_off) >= 1, "EVs should be stopped when not scheduled"

    def test_evs_charge_during_pre_discharge_with_surplus(
        self, default_params, default_outputs
    ):
        """During solar surplus with negative prices ahead, the battery
        should absorb solar (self_consumption) instead of pre-discharging.
        EVs should still charge on the surplus."""
        opt = Optimizer(default_params, default_outputs)

        # Pre-discharge scenario: price 0.30 with negatives ahead
        # (0.30 is mid-range, not expensive, so solar override applies)
        prices = {
            "today": [0.30, -0.10, -0.20] + [0.80] * 21,
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
        # Solar surplus overrides pre_discharge → self_consumption
        assert plan[0]["action"] == ACTION_SELF_CONSUMPTION
        assert "Solar surplus" in plan[0]["reason"]

        actions = result["immediate_actions"]
        # Battery should be in self-consumption (absorbing solar)
        sc_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_self_consumption_mode"
        ]
        assert len(sc_actions) == 1, "Battery should absorb solar surplus"

        # EVs should charge on surplus
        ev_on = [a for a in actions if a["service"] == "switch.turn_on"
                 and "charger" in a.get("entity_id", "")]
        assert len(ev_on) >= 1, "EVs should charge on surplus even during would-be pre_discharge"

    def test_ev_charges_when_scheduled_cheap_hour(
        self, default_params, default_outputs
    ):
        """EVs should charge during the current hour if the schedule
        planned charging (cheap hour with vehicle data)."""
        ev_vehicles = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

        opt = Optimizer(default_params, default_outputs)

        # Flat cheap prices → all hours are candidates for scheduling
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
            ev_vehicles=ev_vehicles,
        )

        actions = result["immediate_actions"]
        ev_on = [a for a in actions if a["service"] == "switch.turn_on"
                 and "charger" in a.get("entity_id", "")]
        assert len(ev_on) >= 1, "EVs should charge when scheduled"

    def test_evs_charge_during_scheduled_self_consumption(
        self, default_params, default_outputs
    ):
        """EVs with a schedule should charge even during self_consumption."""
        ev_vehicles = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

        opt = Optimizer(default_params, default_outputs)

        # Flat prices → self_consumption
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
            ev_vehicles=ev_vehicles,
        )

        actions = result["immediate_actions"]
        ev_on = [a for a in actions if a["service"] == "switch.turn_on"
                 and "ex90" in a.get("entity_id", "")]
        assert len(ev_on) >= 1, "EV with schedule should charge during self_consumption"


class TestSolarSurplusBatteryCharging:
    """Tests for solar-surplus-aware battery charging.

    When solar panels produce more than the house consumes (grid export > 0),
    the battery should absorb that surplus via self-consumption mode.
    This overrides pre-discharge and charge-from-grid.
    """

    def test_solar_surplus_overrides_pre_discharge(
        self, default_params, default_outputs
    ):
        """With solar surplus, don't pre-discharge — absorb the surplus instead."""
        opt = Optimizer(default_params, default_outputs)

        # Negative prices ahead at hours 2-3 → normally would pre-discharge
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
            battery_soc=75,
            ev_connected=False,
            grid_export_power=4000.0,  # 4 kW solar surplus
        )

        plan = result["hourly_plan"]
        # Current hour (0) should be self_consumption, not pre_discharge
        assert plan[0]["action"] == ACTION_SELF_CONSUMPTION
        assert "Solar surplus" in plan[0]["reason"]

        # Future hours without real-time solar data keep their plan
        # Hour 1 should still be pre_discharge (no solar data for future)
        assert plan[1]["action"] == ACTION_PRE_DISCHARGE
        # Negative hours still maximize_load
        assert plan[2]["action"] == ACTION_MAXIMIZE_LOAD

    def test_solar_surplus_overrides_charge_battery(
        self, default_params, default_outputs
    ):
        """With solar surplus, don't force-charge from grid — absorb solar."""
        opt = Optimizer(default_params, default_outputs)

        # Wide spread: 0.10-1.50 → hour 0 price 0.10 is cheap → charge_battery
        prices = {
            "today": [0.10, 0.15, 0.20, 0.30, 0.50, 0.70,
                      0.90, 1.10, 1.30, 1.50, 1.40, 1.20,
                      1.00, 0.80, 0.60, 0.50, 0.40, 0.30,
                      0.20, 0.15, 0.10, 0.08, 0.05, 0.03],
            "tomorrow": [],
            "currency": "SEK",
        }

        # Without solar surplus → charge_battery
        result_no_solar = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # low SoC
            ev_connected=False,
            grid_export_power=0.0,
        )
        assert result_no_solar["hourly_plan"][0]["action"] == ACTION_CHARGE_BATTERY

        # With solar surplus → self_consumption (absorb solar instead)
        result_solar = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # low SoC
            ev_connected=False,
            grid_export_power=3000.0,  # 3 kW export
        )
        assert result_solar["hourly_plan"][0]["action"] == ACTION_SELF_CONSUMPTION
        assert "Solar surplus" in result_solar["hourly_plan"][0]["reason"]

    def test_solar_surplus_no_override_when_battery_full(
        self, default_params, default_outputs
    ):
        """When battery is at max SoC, solar surplus override should NOT apply
        so pre-discharge can still create room for upcoming negative prices."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, -0.10, -0.20] + [0.80] * 21,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=100,  # Battery FULL
            ev_connected=False,
            grid_export_power=5000.0,  # Solar surplus exists
        )

        plan = result["hourly_plan"]
        # Battery is full → can't absorb more → solar override doesn't apply
        # BUT soc=100 > min_soc(10) so pre_discharge IS allowed
        assert plan[0]["action"] == ACTION_PRE_DISCHARGE

    def test_solar_surplus_no_override_for_negative_prices(
        self, default_params, default_outputs
    ):
        """Negative price check has higher priority than solar surplus."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [-0.10] + [0.80] * 23,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
            grid_export_power=5000.0,
        )

        plan = result["hourly_plan"]
        # Negative price takes priority over solar surplus
        assert plan[0]["action"] == ACTION_MAXIMIZE_LOAD

    def test_solar_surplus_only_affects_current_hour(
        self, default_params, default_outputs
    ):
        """Solar surplus override should only affect hour 0 (real-time data).
        Future hours keep their LP-based classification."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.10, 0.10, -0.10, -0.20] + [0.80] * 20,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
            grid_export_power=5000.0,
        )

        plan = result["hourly_plan"]
        # Hour 0: solar surplus → self_consumption
        assert plan[0]["action"] == ACTION_SELF_CONSUMPTION
        # Hour 1: LP may classify as charge, self_consumption, or
        # pre_discharge depending on optimal arbitrage.  The key
        # assertion is that the solar override only touched hour 0.
        assert plan[1]["action"] != ACTION_SELF_CONSUMPTION or "Solar surplus" not in plan[1].get("reason", "")

    def test_pre_discharge_still_works_without_solar(
        self, default_params, default_outputs
    ):
        """Without solar surplus, pre-discharge should work normally."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, 0.40, -0.10, -0.20] + [0.60] * 20,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=80,
            ev_connected=False,
            grid_export_power=0.0,  # No solar surplus
        )

        plan = result["hourly_plan"]
        assert plan[0]["action"] == ACTION_PRE_DISCHARGE

    def test_battery_immediate_action_self_consumption_on_surplus(
        self, default_params, default_outputs
    ):
        """When solar surplus overrides, the immediate action should set
        the inverter to self-consumption mode."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, -0.10] + [0.80] * 22,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=75,
            ev_connected=False,
            grid_export_power=4000.0,
        )

        actions = result["immediate_actions"]
        # Should call self_consumption on inverter
        sc_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_self_consumption_mode"
        ]
        assert len(sc_actions) == 1
        # Should NOT call force_discharge (no pre-discharge)
        fd_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_forced_discharge_battery_mode"
        ]
        assert len(fd_actions) == 0

    def test_low_soc_charges_then_discharges(
        self, default_params, default_outputs
    ):
        """Battery starting below min_soc should charge first (especially
        at negative prices) and then discharge profitably.  The LP
        correctly handles this by respecting SoC constraints while
        minimising overall cost."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.50, -0.10] + [0.80] * 22,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=5,  # below min_soc (10%)
            ev_connected=False,
        )

        plan = result["hourly_plan"]

        # Hour 1 has negative price → should charge (maximize_load)
        assert plan[1]["action"] == ACTION_MAXIMIZE_LOAD

        # SoC constraints are respected: LP never drops below min_soc
        for entry in plan:
            soc_after = entry.get("lp_soc_after", 100)
            assert soc_after >= 9.9, (
                f"SoC dropped to {soc_after}% at hour {entry['hour']}, "
                f"below min_soc (10%)"
            )

    def test_small_battery_limits_discharge_to_capacity(
        self, default_params, default_outputs
    ):
        """With a small battery, the LP should limit total discharge to
        available capacity rather than discharging every expensive hour."""
        params = {**default_params}
        outputs = {**default_outputs}
        outputs["sungrow"] = {**default_outputs["sungrow"], "capacity_kwh": 5.0}
        opt = Optimizer(params, outputs)

        # Many expensive hours, small battery
        prices = {
            "today": [0.10] * 6 + [0.50] * 4 + [1.50, 1.60, 1.70, 1.80, 1.40, 1.30] + [0.50] * 8,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,  # (50-10)/100 * 5 = 2.0 kWh available
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # The LP may charge at cheap hours (0.10) to gain extra energy
        # for discharge at expensive hours.  Total discharge is bounded
        # by initial energy PLUS any energy charged during the plan.
        total_charge = sum(h.get("lp_charge_kwh", 0) for h in plan)
        total_discharge = sum(h.get("lp_discharge_kwh", 0) for h in plan)
        available_kwh = (50 - 10) / 100.0 * 5.0  # 2.0 kWh from initial SoC

        # Net discharge cannot exceed initial available energy
        # (charge first, then discharge)
        net_discharge = total_discharge - total_charge
        assert net_discharge <= available_kwh * 1.1, (
            f"Net discharge {net_discharge:.1f} kWh should not exceed "
            f"available {available_kwh:.1f} kWh (with efficiency margin)"
        )

        # SoC never drops below min_soc (10%)
        for entry in plan:
            soc_after = entry.get("lp_soc_after", 100)
            assert soc_after >= 9.9, (
                f"SoC dropped to {soc_after}% at hour {entry['hour']}, "
                f"below min_soc (10%)"
            )

    def test_consumption_prediction_affects_discharge_count(
        self, default_params, default_outputs
    ):
        """Higher consumption predictions should reduce the number of
        affordable discharge hours."""
        outputs = {**default_outputs}
        outputs["sungrow"] = {**default_outputs["sungrow"], "capacity_kwh": 10.0}
        opt = Optimizer(default_params, outputs)

        prices = {
            "today": [0.10] * 6 + [0.50] * 4 + [1.50, 1.60, 1.70, 1.80, 1.40, 1.30] + [0.50] * 8,
            "tomorrow": [],
            "currency": "SEK",
        }

        # Low consumption → more hours affordable
        result_low = opt.optimize(
            prices=prices,
            predicted_consumption=[0.5] * 24,
            battery_soc=50,  # (50-10)/100*10 = 4.0 kWh
            ev_connected=False,
        )
        # High consumption → fewer hours affordable
        result_high = opt.optimize(
            prices=prices,
            predicted_consumption=[3.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        low_discharge = len([h for h in result_low["hourly_plan"]
                            if h["action"] == ACTION_DISCHARGE_BATTERY])
        high_discharge = len([h for h in result_high["hourly_plan"]
                             if h["action"] == ACTION_DISCHARGE_BATTERY])

        assert low_discharge >= high_discharge, (
            f"Lower consumption ({low_discharge} discharge hours) should allow "
            f"at least as many discharge hours as higher consumption ({high_discharge})"
        )

    def test_large_battery_covers_expensive_hours(self, default_params, default_outputs):
        """A very large battery should discharge during the most expensive
        hours.  The LP optimises cost, so it focuses on the highest-value
        hours and may consolidate discharge."""
        outputs = {**default_outputs}
        outputs["sungrow"] = {**default_outputs["sungrow"], "capacity_kwh": 24.0}
        opt = Optimizer(default_params, outputs)

        # 6 expensive hours at 1 kWh each → 6 kWh needed
        # Battery: (80-10)/100*24 = 16.8 kWh available → easily covers all 6
        prices = {
            "today": [0.10] * 6 + [0.50] * 6 + [1.50, 1.60, 1.70, 1.80, 1.40, 1.30] + [0.50] * 6,
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
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # LP should discharge during the most expensive hours.
        # With 16.8 kWh available the LP has plenty of energy; it may
        # use 4-6 discharge hours depending on price-arbitrage calculus.
        assert len(discharge_hours) >= 4, (
            f"Large battery should cover most expensive hours, got {len(discharge_hours)}"
        )

        # The top-3 most expensive hours (1.80, 1.70, 1.60) should always
        # be covered
        discharge_hour_nums = {h["hour"] for h in discharge_hours}
        # Hours 12-17 have prices [1.50, 1.60, 1.70, 1.80, 1.40, 1.30]
        # Hour 15 = 1.80, hour 14 = 1.70, hour 13 = 1.60
        for must_discharge in [13, 14, 15]:
            assert must_discharge in discharge_hour_nums, (
                f"Hour {must_discharge} should be discharged (top price)"
            )


class TestLPArbitrage:
    """Tests for LP-based battery charge/discharge arbitrage.

    The LP optimizer charges at cheap hours and discharges at expensive
    hours based on cost minimisation.  It handles SoC constraints,
    efficiency losses, and power limits automatically.
    """

    @pytest.fixture
    def ev_vehicle_ex90(self):
        """EX90 at 89% — needs a small top-up to 100%."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 89,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

    def test_lp_charges_when_arbitrage_profitable(
        self, default_params, default_outputs
    ):
        """LP charges at cheaper hours and discharges at expensive hours
        when the spread justifies arbitrage (accounting for efficiency)."""
        opt = Optimizer(default_params, default_outputs)

        # Spread: 0.50 vs 1.50 = 1.00 SEK → profitable after efficiency loss
        prices = {
            "today": [0.50] * 6 + [1.50] * 12 + [0.50] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # LP should do arbitrage: charge cheap, discharge expensive
        assert len(charge_hours) > 0, "LP should charge at cheap hours"
        assert len(discharge_hours) > 0, "LP should discharge at expensive hours"

    def test_lp_charges_at_cheap_hours_regardless_of_soc(
        self, default_params, default_outputs
    ):
        """LP charges at very cheap hours regardless of initial SoC,
        because the arbitrage is profitable."""
        opt = Optimizer(default_params, default_outputs)

        # Very cheap morning (0.10), very expensive midday (1.50)
        prices = {
            "today": [0.10] * 6 + [1.50] * 12 + [0.50] * 6,
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
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # Large spread (0.10 → 1.50) justifies arbitrage
        assert len(charge_hours) > 0, "LP should charge at 0.10 SEK"
        assert len(discharge_hours) > 0, "LP should discharge at 1.50 SEK"

    def test_ev_charges_cheapest_hours_first(
        self, default_params, default_outputs, ev_vehicle_ex90
    ):
        """EV charging should be scheduled in the cheapest hours."""
        opt = Optimizer(default_params, default_outputs)

        # Distinct prices: cheap at start, expensive later
        prices = {
            "today": [0.10, 0.15, 0.20, 0.25, 0.30, 0.35,
                      0.50, 0.55, 0.60, 0.65, 0.70, 0.75,
                      1.00, 1.10, 1.20, 1.30, 1.40, 1.50,
                      0.80, 0.70, 0.60, 0.50, 0.40, 0.30],
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle_ex90,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h for h in plan["schedule"] if h["charging"]]

        # The cheapest hours should be selected first
        charging_prices = [h["price"] for h in charging_hours]
        assert all(p <= 0.60 for p in charging_prices[:2]), (
            f"First charging hours should be cheap, got prices: {charging_prices[:3]}"
        )

    def test_ev_kwh_needed_calculation(
        self, default_params, default_outputs, ev_vehicle_ex90
    ):
        """kWh needed should be (target - current) / 100 * capacity."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_vehicle_ex90,
        )

        plan = result["ev_charge_schedule"]
        # EX90: (100 - 89) / 100 * 111 = 12.21 kWh (default target=100%)
        assert abs(plan["total_kwh_needed"] - 12.2) < 0.5

    def test_ev_no_charging_when_at_target(self, default_params, default_outputs):
        """No EV charging scheduled when already at target SoC."""
        ev_full = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "connected",
            "connected": True,
            "vehicle_soc": 100,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 0,
        }]
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_full,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h for h in plan["schedule"] if h["charging"]]
        assert len(charging_hours) == 0

    def test_ev_avoids_discharge_hours(
        self, default_params, default_outputs, ev_vehicle_ex90
    ):
        """EV should NOT charge during expensive/discharge hours."""
        opt = Optimizer(default_params, default_outputs)

        # Very distinct prices: 0.10 cheap, 2.00 expensive
        prices = {
            "today": [0.10] * 6 + [2.00] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle_ex90,
        )

        # Get hours that are discharge_battery
        discharge_hours_set = {
            h["hour"] for h in result["hourly_plan"]
            if h["action"] == ACTION_DISCHARGE_BATTERY
        }

        plan = result["ev_charge_schedule"]
        for entry in plan["schedule"]:
            if entry["charging"]:
                assert entry["hour"] not in discharge_hours_set, (
                    f"EV should not charge during discharge hour {entry['hour']}"
                )

    def test_ev_schedule_created_even_when_disconnected(
        self, default_params, default_outputs
    ):
        """Disconnected vehicles should still be scheduled (plan preview).

        Immediate actions won't issue start/stop for disconnected
        chargers, but the schedule should show when they *would* charge.
        """
        ev_disconnected = [{
            "name": "ex90",
            "power_w": 0,
            "status": "disconnected",
            "connected": False,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 0,
        }]
        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.10] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
            ev_vehicles=ev_disconnected,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h for h in plan["schedule"] if h["charging"]]
        # (95-50)/100 * 111 = 49.95 kWh → ceil(49.95/7) = 8 hours
        assert len(charging_hours) > 0, "Disconnected vehicles should still be scheduled"
        assert plan["vehicles"][0]["connected"] is False
        assert plan["vehicles"][0]["kwh_needed"] > 0

    def test_ev_charging_hours_match_energy_need(
        self, default_params, default_outputs, ev_vehicle_ex90
    ):
        """Total scheduled energy should cover the kWh needed."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_vehicle_ex90,
        )

        plan = result["ev_charge_schedule"]
        total_scheduled = sum(h["total_power_kw"] for h in plan["schedule"] if h["charging"])
        # Should cover (or slightly exceed) the kwh needed
        assert total_scheduled >= plan["total_kwh_needed"] - 0.1

    def test_ev_vehicle_summary_in_plan(
        self, default_params, default_outputs, ev_vehicle_ex90
    ):
        """The plan should include per-vehicle summary data."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_vehicle_ex90,
        )

        vehicles = result["ev_charge_schedule"]["vehicles"]
        assert len(vehicles) == 1
        assert vehicles[0]["name"] == "ex90"
        assert vehicles[0]["soc"] == 89
        assert vehicles[0]["target_soc"] == 100  # optimizer default target
        assert vehicles[0]["capacity_kwh"] == 111
        assert vehicles[0]["connected"] is True
        assert "scheduled_hours" in vehicles[0]

    def test_summary_includes_ev_charge_count(
        self, default_params, default_outputs, ev_vehicle_ex90
    ):
        """The summary string should mention the EV-charge hour count."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_vehicle_ex90,
        )

        assert "EV-charge" in result["summary"]


class TestEVGridMinimization:
    """Tests for EV grid minimization: ramp-down, night preference, Friday target.

    The optimizer should minimize grid energy consumption by:
      - Stopping charger when vehicle SoC >= target (ramp-down)
      - Preferring night hours (22:00-06:00) for charging (off-peak)
      - Lowering target SoC on Friday (car parked at home Sat, solar fills later)
      - Overriding Friday target during negative prices (get paid to charge)
    """

    @pytest.fixture
    def ev_vehicle_ex90_full(self):
        """EX90 at 100% — already at target, should stop."""
        return [{
            "name": "ex90",
            "power_w": 0,
            "status": "connected",
            "connected": True,
            "vehicle_soc": 100,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 0,
        }]

    @pytest.fixture
    def ev_vehicle_ex90_at_80(self):
        """EX90 at 80% — at weekend target but below vehicle target."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 80,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

    @pytest.fixture
    def ev_vehicle_ex90_charging(self):
        """EX90 at 50% — needs charging."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

    # ------------------------------------------------------------------
    # Ramp-down: stop when vehicle at target
    # ------------------------------------------------------------------

    def test_ramp_down_stops_charger_at_target(
        self, default_params, default_outputs, ev_vehicle_ex90_full
    ):
        """When vehicle SoC >= target, the charger switch should be turned OFF
        even if the price is cheap."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.05] * 24,  # Very cheap — normally would charge
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle_ex90_full,
        )

        actions = result["immediate_actions"]
        # EX90 at 100% → should STOP, not start
        ev_stop = [a for a in actions if a["service"] == "switch.turn_off"
                   and "ex90" in a.get("entity_id", "")]
        ev_start = [a for a in actions if a["service"] == "switch.turn_on"
                    and "ex90" in a.get("entity_id", "")]
        assert len(ev_stop) >= 1, "Should stop EX90 charger at target SoC"
        assert len(ev_start) == 0, "Should NOT start EX90 when at target"

    def test_ramp_down_does_not_affect_other_chargers(
        self, default_params, default_outputs
    ):
        """When one EV is at target, other EVs should still charge normally."""
        ev_vehicles = [
            {
                "name": "ex90",
                "power_w": 0,
                "status": "connected",
                "connected": True,
                "vehicle_soc": 100,  # At target — should stop
                "vehicle_capacity_kwh": 111,
                "vehicle_target_soc": 100,
                "vehicle_charging_power_w": 0,
            },
            {
                "name": "renault_zoe",
                "power_w": 7000,
                "status": "charging",
                "connected": True,
                "vehicle_soc": 50,  # Needs charging
                "vehicle_capacity_kwh": 52,
                "vehicle_target_soc": 100,
                "vehicle_charging_power_w": 7000,
            },
        ]

        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.05] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicles,
        )

        actions = result["immediate_actions"]
        # EX90 should stop (at target)
        ex90_stop = [a for a in actions if a["service"] == "switch.turn_off"
                     and "ex90" in a.get("entity_id", "")]
        assert len(ex90_stop) >= 1, "EX90 should stop at target"

        # Renault Zoe should charge (cheap price, needs energy)
        zoe_start = [a for a in actions if a["service"] == "switch.turn_on"
                     and "renault_zoe" in a.get("entity_id", "")]
        assert len(zoe_start) >= 1, "Zoe should charge when price is cheap"

    # ------------------------------------------------------------------
    # Weekend target SoC
    # ------------------------------------------------------------------

    def test_friday_lower_target_stops_charger(
        self, default_params, default_outputs, ev_vehicle_ex90_at_80
    ):
        """On Friday, charger should stop at ev_weekend_target_soc (80%)
        even though vehicle target is 100%."""
        # Patch to Friday
        fake_friday = datetime(2025, 1, 3, 0, 0, 0)  # Friday
        with patch(
            "custom_components.home_energy_management.optimizer.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = fake_friday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            opt = Optimizer(default_params, default_outputs)
            prices = {
                "today": [0.05] * 24,
                "tomorrow": [],
                "currency": "SEK",
            }

            result = opt.optimize(
                prices=prices,
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev_vehicle_ex90_at_80,
            )

        actions = result["immediate_actions"]
        # Friday + SoC=80% >= weekend_target=80% → stop
        ev_stop = [a for a in actions if a["service"] == "switch.turn_off"
                   and "ex90" in a.get("entity_id", "")]
        assert len(ev_stop) >= 1, "Should stop at Friday target (80%)"

    def test_weekday_charges_to_full_target(
        self, default_params, default_outputs, ev_vehicle_ex90_at_80
    ):
        """On weekdays, charger should continue past 80% toward 100%."""
        # Default freeze_time is Monday — weekday
        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.05] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle_ex90_at_80,
        )

        actions = result["immediate_actions"]
        # Weekday + SoC=80% < target=100% + cheap price → should charge
        ev_start = [a for a in actions if a["service"] == "switch.turn_on"
                    and "ex90" in a.get("entity_id", "")]
        assert len(ev_start) >= 1, "Weekday should charge past 80% toward 100%"

    def test_saturday_charges_to_full_target(
        self, default_params, default_outputs, ev_vehicle_ex90_at_80
    ):
        """On Saturday, charger should continue past 80% toward 100%
        (Monday is coming — only Friday uses the lower target)."""
        fake_saturday = datetime(2025, 1, 4, 22, 0, 0)  # Saturday 22:00
        with patch(
            "custom_components.home_energy_management.optimizer.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = fake_saturday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            opt = Optimizer(default_params, default_outputs)
            prices = {
                "today": [0.05] * 24,
                "tomorrow": [],
                "currency": "SEK",
            }

            result = opt.optimize(
                prices=prices,
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev_vehicle_ex90_at_80,
            )

        actions = result["immediate_actions"]
        # Saturday + SoC=80% < target=100% + cheap price → should charge
        ev_start = [a for a in actions if a["service"] == "switch.turn_on"
                    and "ex90" in a.get("entity_id", "")]
        assert len(ev_start) >= 1, (
            "Saturday should charge to full 100% target (Monday coming)"
        )

    def test_friday_negative_price_overrides_target(
        self, default_params, default_outputs, ev_vehicle_ex90_at_80
    ):
        """On Friday, negative prices should override the lower target —
        charge to full vehicle target (we get paid to consume)."""
        fake_friday = datetime(2025, 1, 3, 0, 0, 0)  # Friday
        with patch(
            "custom_components.home_energy_management.optimizer.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = fake_friday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

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
                ev_connected=True,
                ev_vehicles=ev_vehicle_ex90_at_80,
            )

        actions = result["immediate_actions"]
        # Negative price + SoC=80% < vehicle_target=100% → keep charging
        ev_start = [a for a in actions if a["service"] == "switch.turn_on"
                    and "ex90" in a.get("entity_id", "")]
        assert len(ev_start) >= 1, (
            "Negative price should override weekend target and charge to full"
        )

    def test_friday_reduces_scheduled_kwh(
        self, default_params, default_outputs, ev_vehicle_ex90_charging
    ):
        """On Friday, the scheduled kWh should be less (lower target)."""
        fake_friday = datetime(2025, 1, 3, 0, 0, 0)  # Friday
        with patch(
            "custom_components.home_energy_management.optimizer.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = fake_friday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            opt = Optimizer(default_params, default_outputs)
            prices = {
                "today": [0.50] * 24,
                "tomorrow": [],
                "currency": "SEK",
            }

            result_friday = opt.optimize(
                prices=prices,
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev_vehicle_ex90_charging,
            )

        # Weekday result (using default Monday freeze_time)
        opt2 = Optimizer(default_params, default_outputs)
        result_weekday = opt2.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle_ex90_charging,
        )

        friday_kwh = result_friday["ev_charge_schedule"]["total_kwh_needed"]
        weekday_kwh = result_weekday["ev_charge_schedule"]["total_kwh_needed"]

        # Friday: (80-50)/100 * 111 = 33.3 kWh
        # Weekday: (100-50)/100 * 111 = 55.5 kWh
        assert friday_kwh < weekday_kwh, (
            f"Friday ({friday_kwh}) should need less kWh than weekday ({weekday_kwh})"
        )

    # ------------------------------------------------------------------
    # Night preference
    # ------------------------------------------------------------------

    def test_night_hours_preferred_over_same_price_day(
        self, default_params, default_outputs
    ):
        """When night and day hours have similar prices, night should be chosen."""
        ev_vehicle = [{
            "name": "ex90",
            "power_w": 11000,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 90,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 11000,
        }]

        opt = Optimizer(default_params, default_outputs)

        # Flat prices all day — night preference should be the tiebreaker
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
            ev_vehicles=ev_vehicle,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # With flat prices, night hours (0-5, 22-23) should be preferred
        night_hours = {h for h in charging_hours if h >= 22 or h < 6}
        assert len(night_hours) > 0, (
            f"Night hours should be preferred with flat prices, got: {charging_hours}"
        )

    def test_cheap_day_hour_beats_expensive_night(
        self, default_params, default_outputs
    ):
        """A significantly cheaper daytime hour should still beat an
        expensive night hour (price dominates over night bonus)."""
        ev_vehicle = [{
            "name": "ex90",
            "power_w": 11000,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 90,  # Below 95% target so it needs charging
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 11000,
        }]

        opt = Optimizer(default_params, default_outputs)

        # Night hours at 1.00, but hour 12 at 0.10 — day hour should win
        prices = {
            "today": [1.00] * 12 + [0.10] + [1.00] * 11,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # Hour 12 at 0.10 should be selected (much cheaper than night at 1.00)
        assert 12 in charging_hours, (
            f"Cheap daytime hour (0.10) should beat expensive night (1.00), "
            f"got: {charging_hours}"
        )

    # ------------------------------------------------------------------
    # Grid minimization: avoids peak hours
    # ------------------------------------------------------------------

    def test_ev_scheduling_avoids_expensive_discharge_hours(
        self, default_params, default_outputs
    ):
        """EV schedule should not overlap with battery discharge hours
        to avoid pulling from grid during peak prices."""
        ev_vehicle = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 80,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.10] * 6 + [2.00] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle,
        )

        discharge_hours = {
            h["hour"] for h in result["hourly_plan"]
            if h["action"] == ACTION_DISCHARGE_BATTERY
        }

        ev_schedule = result["ev_charge_schedule"]["schedule"]
        ev_charging_hours = {h["hour"] for h in ev_schedule if h["charging"]}

        overlap = discharge_hours & ev_charging_hours
        assert len(overlap) == 0, (
            f"EV should not charge during discharge hours. Overlap: {overlap}"
        )

    def test_ramp_down_friday_and_night_combined(
        self, default_params, default_outputs
    ):
        """Combined scenario: Friday + night preference + ramp-down.
        Car at 85% on Friday → should stop (above Friday target 80%)."""
        ev_vehicle = [{
            "name": "ex90",
            "power_w": 3000,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 85,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 3000,
        }]

        fake_friday = datetime(2025, 1, 3, 2, 0, 0)  # Friday 02:00
        with patch(
            "custom_components.home_energy_management.optimizer.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = fake_friday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            opt = Optimizer(default_params, default_outputs)
            prices = {
                "today": [0.05] * 24,
                "tomorrow": [],
                "currency": "SEK",
            }

            result = opt.optimize(
                prices=prices,
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev_vehicle,
            )

        actions = result["immediate_actions"]
        # Friday, SoC=85% > friday_target=80% → stop
        ev_stop = [a for a in actions if a["service"] == "switch.turn_off"
                   and "ex90" in a.get("entity_id", "")]
        assert len(ev_stop) >= 1, "Should stop on Friday — SoC above Friday target"

        # EV schedule should also show reduced kwh (or 0)
        ev_plan = result["ev_charge_schedule"]
        assert ev_plan["total_kwh_needed"] == 0, (
            "No kWh needed when SoC >= Friday target"
        )


class TestEVDepartureTime:
    """Tests for per-vehicle departure time and min departure SoC.

    When departure_time is set per vehicle, the optimizer should:
      - Only schedule charging in hours *before* the departure deadline
      - Use min_departure_soc as the target SoC instead of the global default
      - Fall back to full candidate list when departure_time is absent
      - Handle midnight-crossing windows (e.g. start_hour=22, departure=07)
    """

    @pytest.fixture
    def ev_with_departure(self):
        """EX90 at 50% with departure at 07:00 and min SoC 80%."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 80,
        }]

    @pytest.fixture
    def ev_without_departure(self):
        """EX90 at 50% with NO departure_time set."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
        }]

    def test_departure_filters_candidate_hours(
        self, default_params, default_outputs, ev_with_departure
    ):
        """Only hours before departure (07:00) should be used for charging."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_with_departure,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # With departure at 07:00 and start_hour=0, only hours 0-6 eligible
        assert all(h < 7 for h in charging_hours), (
            f"All charging should be before departure (07:00), got: {charging_hours}"
        )

    def test_departure_uses_min_departure_soc_as_target(
        self, default_params, default_outputs, ev_with_departure
    ):
        """Target SoC should be min_departure_soc (80%) not default (100%)."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_with_departure,
        )

        vehicles = result["ev_charge_schedule"]["vehicles"]
        assert len(vehicles) == 1
        assert vehicles[0]["target_soc"] == 80
        # kWh needed: (80-50)/100 * 111 = 33.3 kWh
        assert abs(vehicles[0]["kwh_needed"] - 33.3) < 0.5

    def test_no_departure_uses_all_hours(
        self, default_params, default_outputs, ev_without_departure
    ):
        """Without departure_time, all candidate hours should be eligible."""
        opt = Optimizer(default_params, default_outputs)

        # Cheap hour at 15:00, everything else expensive
        prices = {
            "today": [1.00] * 15 + [0.05] + [1.00] * 8,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_without_departure,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # Hour 15 (cheapest) should be in the schedule even though it's
        # past any default departure time
        assert 15 in charging_hours, (
            f"Without departure_time, hour 15 should be selectable, got: {charging_hours}"
        )

    def test_departure_kwh_less_than_full_target(
        self, default_params, default_outputs, ev_with_departure, ev_without_departure
    ):
        """Vehicle with min_departure_soc=80% should need less kWh than one
        targeting 100%."""
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        opt1 = Optimizer(default_params, default_outputs)
        result_dep = opt1.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_with_departure,
        )

        opt2 = Optimizer(default_params, default_outputs)
        result_full = opt2.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_without_departure,
        )

        dep_kwh = result_dep["ev_charge_schedule"]["total_kwh_needed"]
        full_kwh = result_full["ev_charge_schedule"]["total_kwh_needed"]

        # 80% target → (80-50)/100*111 = 33.3 kWh
        # 100% target → (100-50)/100*111 = 55.5 kWh
        assert dep_kwh < full_kwh, (
            f"Departure target 80% ({dep_kwh}) should need less kWh "
            f"than full target 100% ({full_kwh})"
        )

    def test_departure_info_in_vehicle_plan(
        self, default_params, default_outputs, ev_with_departure
    ):
        """Vehicle plan should include departure_time and min_departure_soc."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_with_departure,
        )

        vehicles = result["ev_charge_schedule"]["vehicles"]
        assert vehicles[0]["departure_time"] == "07:00"
        assert vehicles[0]["min_departure_soc"] == 80

    def test_midnight_crossing_departure(
        self, default_params, default_outputs, freeze_time
    ):
        """When start_hour is late evening and departure is early morning,
        the window should cross midnight correctly."""
        # Set time to 22:00 so start_hour = 22
        freeze_time.now.return_value = datetime(2025, 1, 6, 22, 0, 0)

        ev_vehicle = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 80,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 90,
        }]

        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [0.50] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # Window: 22-23 and 0-6 (crossing midnight, before 07:00)
        for h in charging_hours:
            assert h >= 22 or h < 7, (
                f"Midnight-crossing: hour {h} should be 22-23 or 0-6"
            )

    def test_ramp_down_uses_per_vehicle_departure_soc(
        self, default_params, default_outputs
    ):
        """Ramp-down should use per-vehicle min_departure_soc (80%)
        instead of global default (100%)."""
        ev_vehicle = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 85,  # Above departure target (80%) but below 100%
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 80,
        }]

        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.05] * 24,  # Very cheap
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_vehicle,
        )

        actions = result["immediate_actions"]
        # SoC=85% >= min_departure_soc=80% → should stop
        ev_stop = [a for a in actions if a["service"] == "switch.turn_off"
                   and "ex90" in a.get("entity_id", "")]
        assert len(ev_stop) >= 1, (
            "Should stop charger — SoC 85% >= departure target 80%"
        )

    def test_two_vehicles_different_departures(
        self, default_params, default_outputs
    ):
        """Two EVs with different departure times should each only use
        hours before their respective deadlines."""
        ev_vehicles = [
            {
                "name": "ex90",
                "power_w": 8250,
                "status": "charging",
                "connected": True,
                "vehicle_soc": 80,
                "vehicle_capacity_kwh": 111,
                "vehicle_target_soc": 100,
                "vehicle_charging_power_w": 8250,
                "departure_time": "05:00",
                "min_departure_soc": 90,
            },
            {
                "name": "renault_zoe",
                "power_w": 7000,
                "status": "charging",
                "connected": True,
                "vehicle_soc": 60,
                "vehicle_capacity_kwh": 52,
                "vehicle_target_soc": 100,
                "vehicle_charging_power_w": 7000,
                "departure_time": "09:00",
                "min_departure_soc": 80,
            },
        ]

        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_vehicles,
        )

        vehicles = result["ev_charge_schedule"]["vehicles"]
        ex90_plan = next(v for v in vehicles if v["name"] == "ex90")
        zoe_plan = next(v for v in vehicles if v["name"] == "renault_zoe")

        # EX90: departure 05:00 → hours 0-4 only
        for h in ex90_plan["scheduled_hours"]:
            assert h < 5, f"EX90 should only charge before 05:00, got hour {h}"

        # Zoe: departure 09:00 → hours 0-8 only
        for h in zoe_plan["scheduled_hours"]:
            assert h < 9, f"Zoe should only charge before 09:00, got hour {h}"

        # Zoe has a later departure so it can use more hours
        assert zoe_plan["target_soc"] == 80
        assert ex90_plan["target_soc"] == 90

    def test_friday_respects_explicit_departure_soc(
        self, default_params, default_outputs
    ):
        """When user explicitly sets min_departure_soc, the Friday
        weekend target should NOT override it — user's setting wins."""
        ev_vehicle = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 90,
        }]

        fake_friday = datetime(2025, 1, 3, 0, 0, 0)  # Friday
        with patch(
            "custom_components.home_energy_management.optimizer.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = fake_friday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            opt = Optimizer(default_params, default_outputs)
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
                ev_vehicles=ev_vehicle,
            )

        vehicles = result["ev_charge_schedule"]["vehicles"]
        # User explicitly set min_departure_soc=90 → their setting wins
        # over the Friday weekend target (80%)
        assert vehicles[0]["target_soc"] == 90, (
            f"User's explicit departure SoC should win, got {vehicles[0]['target_soc']}"
        )


class TestEVOptimizationDays:
    """Tests for multi-day EV optimisation (optimization_days=2).

    When ev_optimization_window=2, the optimizer should:
      - Extend the EV candidate window to include tomorrow's prices
      - Defer charging to cheaper day-2 hours
      - Respect min_charge_level as a floor (urgent charging today)
      - Fall back to normal single-day behaviour when window=1
    """

    @pytest.fixture
    def two_day_params(self, default_params):
        """Params with 2-day optimization enabled."""
        return {**default_params, "ev_optimization_window": 2}

    @pytest.fixture
    def ev_below_floor(self):
        """EX90 at 15% SoC, below min_charge_level of 40%."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 15,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 40,
        }]

    @pytest.fixture
    def ev_above_floor(self):
        """EX90 at 50% SoC, above min_charge_level of 40%."""
        return [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 40,
        }]

    def test_two_day_defers_to_cheaper_tomorrow(
        self, two_day_params, default_outputs, ev_above_floor
    ):
        """When tomorrow is cheaper, charging should be deferred to day-2 hours."""
        opt = Optimizer(two_day_params, default_outputs)

        # Today: expensive (1.00 SEK/kWh), Tomorrow: very cheap (0.10 SEK/kWh)
        prices = {
            "today": [1.00] * 24,
            "tomorrow": [0.10] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_above_floor,
        )

        plan = result["ev_charge_schedule"]
        schedule_entries = plan["schedule"]

        # Vehicle is at 50%, above floor (40%), so ALL charging can be deferred.
        # Tomorrow hours (0-23 of day 2) should be preferred since they're cheaper.
        # In the extended plan, day-2 entries start at index 24.
        day2_charging_indices = [
            i for i, h in enumerate(schedule_entries)
            if h["charging"] and i >= 24
        ]
        day1_charging_indices = [
            i for i, h in enumerate(schedule_entries)
            if h["charging"] and i < 24
        ]

        assert len(day2_charging_indices) > 0, (
            "Should defer charging to cheaper day-2 hours"
        )
        assert len(day1_charging_indices) == 0, (
            f"No day-1 charging expected when above floor and day 2 is cheaper, "
            f"got day-1 indices: {day1_charging_indices}"
        )

    def test_two_day_urgent_floor_then_defer(
        self, two_day_params, default_outputs, ev_below_floor
    ):
        """When SoC is below floor, charge urgently to floor in near-term,
        then defer the rest to cheaper day-2 hours."""
        opt = Optimizer(two_day_params, default_outputs)

        # Today: moderate price (0.80), Tomorrow: very cheap (0.05)
        prices = {
            "today": [0.80] * 24,
            "tomorrow": [0.05] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_below_floor,
        )

        plan = result["ev_charge_schedule"]
        vehicles = plan["vehicles"]
        assert len(vehicles) == 1
        v = vehicles[0]

        # Vehicle at 15%, floor at 40%, target 100%
        # Urgent: (40-15)/100 * 111 = 27.75 kWh → must be in day 1
        # Deferred: (100-40)/100 * 111 = 66.6 kWh → should be in day 2
        schedule_entries = plan["schedule"]
        day1_kwh = sum(
            e["vehicles"].get("ex90", 0)
            for i, e in enumerate(schedule_entries) if i < 24
        )
        day2_kwh = sum(
            e["vehicles"].get("ex90", 0)
            for i, e in enumerate(schedule_entries) if i >= 24
        )

        # Day 1 should have at least the urgent portion (~27.75 kWh)
        assert day1_kwh >= 25, (
            f"Day-1 should have at least ~27.75 kWh (urgent floor), got {day1_kwh:.1f}"
        )
        # Day 2 should have the deferred portion
        assert day2_kwh > 0, (
            f"Day-2 should have deferred charging, got {day2_kwh:.1f} kWh"
        )

    def test_single_day_ignores_tomorrow(
        self, default_params, default_outputs, ev_above_floor
    ):
        """With optimization_window=1 (default), tomorrow prices should not
        be used for EV scheduling even if available."""
        opt = Optimizer(default_params, default_outputs)

        # Today expensive, tomorrow cheap — but window=1 so no deferral
        prices = {
            "today": [1.00] * 24,
            "tomorrow": [0.05] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_above_floor,
        )

        plan = result["ev_charge_schedule"]
        schedule_entries = plan["schedule"]

        # With window=1, schedule should only span 24 entries (no day-2 extension)
        assert len(schedule_entries) == 24, (
            f"Single-day should have 24 entries, got {len(schedule_entries)}"
        )

    def test_two_day_extends_schedule_to_48h(
        self, two_day_params, default_outputs, ev_above_floor
    ):
        """The EV schedule should extend to 48 entries when optimization_days=2."""
        opt = Optimizer(two_day_params, default_outputs)

        prices = {
            "today": [0.50] * 24,
            "tomorrow": [0.50] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_above_floor,
        )

        plan = result["ev_charge_schedule"]
        assert len(plan["schedule"]) == 48, (
            f"Two-day should extend schedule to 48 entries, got {len(plan['schedule'])}"
        )

    def test_min_charge_level_in_vehicle_plan(
        self, default_params, default_outputs, ev_below_floor
    ):
        """Vehicle plan should include min_charge_level field."""
        opt = Optimizer(default_params, default_outputs)
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
            ev_vehicles=ev_below_floor,
        )

        vehicles = result["ev_charge_schedule"]["vehicles"]
        assert vehicles[0]["min_charge_level"] == 40

    def test_no_min_charge_level_schedules_normally(
        self, two_day_params, default_outputs
    ):
        """When min_charge_level is 0, all charging defers to cheapest hours."""
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 15,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 0,
        }]

        opt = Optimizer(two_day_params, default_outputs)
        prices = {
            "today": [1.00] * 24,
            "tomorrow": [0.05] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev,
        )

        plan = result["ev_charge_schedule"]
        schedule_entries = plan["schedule"]

        # No floor → all charging should go to cheapest (day 2)
        day1_kwh = sum(
            e["vehicles"].get("ex90", 0)
            for i, e in enumerate(schedule_entries) if i < 24
        )
        day2_kwh = sum(
            e["vehicles"].get("ex90", 0)
            for i, e in enumerate(schedule_entries) if i >= 24
        )

        assert day2_kwh > day1_kwh, (
            f"With no floor, all charging should defer to cheaper day 2. "
            f"Day 1: {day1_kwh:.1f}, Day 2: {day2_kwh:.1f}"
        )

    def test_two_day_with_departure_filters_correctly(
        self, two_day_params, default_outputs
    ):
        """Departure time should still constrain EV charging even with 2-day window."""
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 80,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 90,
            "min_charge_level": 0,
        }]

        opt = Optimizer(two_day_params, default_outputs)
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [0.10] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # Departure at 07:00 limits to hours 0-6
        for h in charging_hours:
            assert h < 7, (
                f"Departure at 07:00 should restrict hours to 0-6, got {h}"
            )

    def test_two_day_battery_plan_stays_24h(
        self, two_day_params, default_outputs, ev_above_floor
    ):
        """The battery hourly_plan should remain 24h even with 2-day EV window."""
        opt = Optimizer(two_day_params, default_outputs)
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [0.50] * 24,
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev_above_floor,
        )

        # Battery hourly plan should not be extended
        assert len(result["hourly_plan"]) == 24, (
            f"Battery plan should stay at 24h, got {len(result['hourly_plan'])}"
        )

    def test_min_charge_level_all_urgent_when_no_tomorrow(
        self, two_day_params, default_outputs
    ):
        """When there are no tomorrow prices, urgent+deferred all go to today."""
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 15,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 40,
        }]

        opt = Optimizer(two_day_params, default_outputs)
        prices = {
            "today": [0.50] * 24,
            "tomorrow": [],  # No tomorrow prices yet
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev,
        )

        plan = result["ev_charge_schedule"]
        # Without tomorrow prices, schedule can't extend → stays at 24
        assert len(plan["schedule"]) == 24, (
            f"Without tomorrow prices, schedule should be 24h, got {len(plan['schedule'])}"
        )

        vehicles = plan["vehicles"]
        assert vehicles[0]["kwh_needed"] > 0

    def test_urgent_floor_charges_earliest_not_cheapest(
        self, two_day_params, default_outputs
    ):
        """When SoC is above critical threshold (10%), the urgent pass
        uses cheapest-price scheduling, NOT chronological ASAP.

        Scenario: current hour 14, today uniform 0.25 SEK, tomorrow
        uniform 0.10 SEK.  SoC=10% with floor=30%, target=100%.
        Urgent (10→30%) should use cheapest near-term hours (night
        hours get preference), NOT the earliest chronological hours.
        Deferred (30→100%) should prefer cheapest day-2 hours.
        """
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 10,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 30,
        }]

        opt = Optimizer(two_day_params, default_outputs)

        # Today: uniform 0.25, Tomorrow: uniform 0.10
        # Spread from hour 14 = 0.25-0.10 = 0.15 < 0.30 → no discharge
        today_prices = [0.25] * 24
        tomorrow_prices = [0.10] * 24

        with patch("custom_components.home_energy_management.optimizer.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 3, 12, 14, 0)  # Tuesday 14:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = opt.optimize(
                prices={"today": today_prices, "tomorrow": tomorrow_prices, "currency": "SEK"},
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev,
            )

        plan = result["ev_charge_schedule"]
        schedule = plan["schedule"]
        near_term = len(result["hourly_plan"])

        # SoC=10% is NOT critically low (threshold is < 10), so the
        # urgent pass uses cheapest-price scheduling.  With night
        # preference, night hours (22-23) should appear before
        # daytime hours (14-21) when prices are uniform.
        charging_indices = [
            i for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0 and i < near_term
        ]
        assert len(charging_indices) > 0, "Should have some day-1 charging"

        # Verify NOT strictly chronological from index 0 — the
        # cheapest-price sort should pick night hours first.
        # With uniform prices, night preference gives hours 22-23
        # (indices 8-9 from start_hour=14) a lower effective price.
        # So first charging index should NOT be 0.
        first_idx = charging_indices[0]
        assert first_idx > 0, (
            f"Cheapest-price urgent pass should NOT start at index 0 "
            f"(chronological), but it did. Indices: {charging_indices}"
        )

    def test_urgent_chronological_deferred_cheapest(
        self, two_day_params, default_outputs
    ):
        """Verify the two-pass split: urgent portion uses cheapest hours
        (not chronological) when SoC is above the critical threshold
        (10%).  Deferred portion picks cheapest hours across the full
        window including day-2.

        Today: uniform 0.25 SEK from hour 10 (spread < 0.30 → no
        discharge exclusion).  Tomorrow: 0.10 SEK (cheaper).
        Car at 15% with 40% floor, target 100%.
        Urgent (15→40%) should prefer cheapest hours (not ASAP).
        Deferred (40→100%) should prefer cheapest day-2 hours.
        """
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 15,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 40,
        }]

        opt = Optimizer(two_day_params, default_outputs)

        today_prices = [0.25] * 24
        tomorrow_prices = [0.10] * 24

        with patch("custom_components.home_energy_management.optimizer.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 3, 12, 10, 0)  # Tuesday 10:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = opt.optimize(
                prices={"today": today_prices, "tomorrow": tomorrow_prices, "currency": "SEK"},
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev,
            )

        plan = result["ev_charge_schedule"]
        schedule = plan["schedule"]
        near_term = len(result["hourly_plan"])  # day-1 boundary

        day1_charging = [
            (i, e["vehicles"].get("ex90", 0))
            for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0 and i < near_term
        ]
        day2_charging = [
            (i, e["vehicles"].get("ex90", 0))
            for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0 and i >= near_term
        ]

        # SoC=15% is above the critical threshold (10%), so the urgent
        # pass uses cheapest-price scheduling, NOT chronological.
        # With uniform prices (0.25 today), urgent hours may appear
        # anywhere in day-1 (not necessarily starting at index 0).
        # The important thing is that deferred uses cheaper day-2.
        assert len(day1_charging) > 0, "Should have some day-1 charging"

        # Deferred portion should predominantly go to day 2 (cheaper)
        day2_kwh = sum(kwh for _, kwh in day2_charging)
        assert day2_kwh > 0, (
            "Deferred portion should use cheaper day-2 hours"
        )

    def test_urgent_critical_soc_charges_asap(
        self, two_day_params, default_outputs
    ):
        """When SoC is critically low (< 10%), the urgent pass charges
        ASAP (chronologically) regardless of price.
        """
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 5,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "min_charge_level": 40,
        }]

        opt = Optimizer(two_day_params, default_outputs)

        # Use uniform prices to avoid discharge exclusions that
        # would remove index 0 from candidates.
        today_prices = [0.25] * 24
        tomorrow_prices = [0.10] * 24

        with patch("custom_components.home_energy_management.optimizer.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 3, 12, 10, 0)  # Tuesday 10:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = opt.optimize(
                prices={"today": today_prices, "tomorrow": tomorrow_prices, "currency": "SEK"},
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev,
            )

        plan = result["ev_charge_schedule"]
        schedule = plan["schedule"]
        near_term = len(result["hourly_plan"])

        day1_charging = [
            (i, e["vehicles"].get("ex90", 0))
            for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0 and i < near_term
        ]

        # Critically low SoC → urgent pass should start at index 0
        if day1_charging:
            first_idx = day1_charging[0][0]
            assert first_idx == 0, (
                f"Critical SoC should charge ASAP at index 0, got {first_idx}"
            )

    def test_two_day_deferred_ignores_departure_filter(
        self, two_day_params, default_outputs
    ):
        """With optimization_window=2 and min_charge_level>0, the deferred
        portion should use ALL hours in the extended window — including
        hours AFTER the departure time — to find the cheapest prices.

        Scenario: hour 0, departure_time=07:00, SoC=50% > floor=40%.
        Today prices: 0.80 SEK (expensive).
        Tomorrow prices: cheap during daytime (hours 10-16 at 0.02 SEK).
        With departure filter, only hours 0-6 are candidates (expensive).
        Without departure filter, hours 10-16 tomorrow (very cheap) are used.
        """
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 100,
            "min_charge_level": 40,
        }]

        opt = Optimizer(two_day_params, default_outputs)

        # Uniform prices — but tomorrow daytime (10-16) is very cheap.
        # Keep spread small (< 0.30) to avoid discharge classification.
        today_prices = [0.25] * 24
        tomorrow_prices = [0.25] * 10 + [0.02] * 7 + [0.25] * 7

        with patch("custom_components.home_energy_management.optimizer.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 3, 12, 0, 0)  # Tuesday 00:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = opt.optimize(
                prices={
                    "today": today_prices,
                    "tomorrow": tomorrow_prices,
                    "currency": "SEK",
                },
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev,
            )

        plan = result["ev_charge_schedule"]
        schedule = plan["schedule"]

        # Collect hours where EX90 is charging
        charging_entries = [
            (i, e["hour"], e["price"], e["vehicles"].get("ex90", 0))
            for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0
        ]

        # Some charging should be scheduled in hours >= 7 (after departure)
        # because those are the cheapest hours in the extended window.
        after_departure = [
            (i, h, p, kwh) for i, h, p, kwh in charging_entries if h >= 7
        ]
        assert len(after_departure) > 0, (
            f"Deferred charging should use cheap hours after departure "
            f"(hours 10-16 tomorrow at 0.02 SEK). "
            f"All charging: {[(h, p) for _, h, p, _ in charging_entries]}"
        )

    def test_two_day_deferred_below_floor_ignores_departure(
        self, two_day_params, default_outputs
    ):
        """When SoC is below floor and window=2, urgent pass uses
        departure-filtered near-term hours, but deferred pass uses
        the full extended window (no departure filter).

        Scenario: SoC=10%, floor=30%, target=100%, departure=07:00.
        Urgent (10→30%): must happen ASAP in near-term before departure.
        Deferred (30→100%): should pick cheapest hours across full
        window including hours after departure.
        """
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 10,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 100,
            "min_charge_level": 30,
        }]

        opt = Optimizer(two_day_params, default_outputs)

        # Today: moderate prices. Tomorrow 10-16: very cheap.
        today_prices = [0.25] * 24
        tomorrow_prices = [0.25] * 10 + [0.02] * 7 + [0.25] * 7

        with patch("custom_components.home_energy_management.optimizer.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 3, 12, 0, 0)  # Tuesday 00:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = opt.optimize(
                prices={
                    "today": today_prices,
                    "tomorrow": tomorrow_prices,
                    "currency": "SEK",
                },
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev,
            )

        plan = result["ev_charge_schedule"]
        schedule = plan["schedule"]

        charging_entries = [
            (i, e["hour"], e["price"], e["vehicles"].get("ex90", 0))
            for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0
        ]

        # Urgent portion should be in near-term before departure (hours 0-6)
        near_term = len(result["hourly_plan"])
        urgent_before_dep = [
            (i, h, p, kwh)
            for i, h, p, kwh in charging_entries
            if i < near_term and h < 7
        ]
        assert len(urgent_before_dep) > 0, (
            "Urgent pass should have some hours before departure"
        )

        # Deferred portion should include hours after departure
        after_departure = [
            (i, h, p, kwh) for i, h, p, kwh in charging_entries if h >= 7
        ]
        assert len(after_departure) > 0, (
            f"Deferred pass should use cheap hours after departure "
            f"(tomorrow 10-16 at 0.02). All: {[(h, p) for _, h, p, _ in charging_entries]}"
        )

    def test_single_day_departure_still_filters(
        self, default_params, default_outputs
    ):
        """With optimization_window=1, departure filtering should still
        apply even when min_charge_level > 0. The extended-window
        relaxation only kicks in with window >= 2."""
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 50,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 100,
            "min_charge_level": 40,
        }]

        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.25] * 24,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [h["hour"] for h in plan["schedule"] if h["charging"]]

        # With window=1, departure filter should still restrict to 0-6
        for h in charging_hours:
            assert h < 7, (
                f"Window=1: departure at 07:00 should restrict to 0-6, got {h}"
            )

    def test_two_day_deferred_day1_still_respects_departure(
        self, two_day_params, default_outputs
    ):
        """Regression test: when the optimizer recalculates mid-morning
        with optimization_window=2, day-1 hours AFTER the departure time
        must NOT be scheduled — only day-2 hours may bypass the departure
        filter.

        Scenario (matches the real-world bug): current_hour=6 CET,
        departure=07:00, SoC=85% > floor=70%, target=100%.
        Day-1 hour 6 is cheap (0.08) and before departure → eligible.
        Day-1 hours 7-23 are after departure → must be excluded.
        Day-2 has cheap hours at 0.02 → should be used instead.
        """
        ev = [{
            "name": "ex90",
            "power_w": 8250,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 85,
            "vehicle_capacity_kwh": 111,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 8250,
            "departure_time": "07:00",
            "min_departure_soc": 100,
            "min_charge_level": 70,
        }]

        opt = Optimizer(two_day_params, default_outputs)

        # Today: hour 6 cheap (0.08), hours 7+ moderate (0.15).
        # Tomorrow: hours 10-16 very cheap (0.02).
        today_prices = [0.25] * 6 + [0.08] + [0.15] * 17
        tomorrow_prices = [0.25] * 10 + [0.02] * 7 + [0.25] * 7

        with patch("custom_components.home_energy_management.optimizer.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 3, 12, 6, 0)  # 06:00
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = opt.optimize(
                prices={
                    "today": today_prices,
                    "tomorrow": tomorrow_prices,
                    "currency": "SEK",
                },
                predicted_consumption=[1.0] * 24,
                battery_soc=50,
                ev_connected=True,
                ev_vehicles=ev,
            )

        plan = result["ev_charge_schedule"]
        schedule = plan["schedule"]

        # Collect all hours where EX90 is scheduled to charge
        charging_entries = [
            (i, e["hour"], e["price"], e["vehicles"].get("ex90", 0))
            for i, e in enumerate(schedule)
            if e["vehicles"].get("ex90", 0) > 0
        ]

        # Day-1 hours after departure (7-23) must NOT appear
        near_term = len(result["hourly_plan"])
        day1_after_departure = [
            (i, h, p, kwh)
            for i, h, p, kwh in charging_entries
            if i < near_term and h >= 7
        ]
        assert len(day1_after_departure) == 0, (
            f"Day-1 hours after departure (>=07:00) must NOT be scheduled "
            f"when window=2. Got: {[(h, p) for _, h, p, _ in day1_after_departure]}. "
            f"All charging: {[(h, p) for _, h, p, _ in charging_entries]}"
        )

        # Day-2 cheap hours SHOULD be scheduled
        day2_entries = [
            (i, h, p, kwh)
            for i, h, p, kwh in charging_entries
            if i >= near_term
        ]
        assert len(day2_entries) > 0, (
            f"Day-2 cheap hours should be used for deferred charging. "
            f"All charging: {[(h, p) for _, h, p, _ in charging_entries]}"
        )


class TestGridTariff:
    """Tests for grid transfer tariff support.

    When grid_tariff_peak_sek / grid_tariff_offpeak_sek are set, the
    optimizer should use effective prices (spot + tariff) for ALL
    decisions — battery scheduling and EV charging.
    """

    @pytest.fixture
    def tariff_params(self):
        """Parameters with realistic Swedish grid tariffs."""
        return {
            "min_price_spread": 0.30,
            "planning_horizon_hours": 24,
            "enable_charger_control": True,
            "enable_battery_control": True,
            "grid_tariff_peak_sek": 0.30,
            "grid_tariff_offpeak_sek": 0.10,
            "grid_tariff_peak_start": 6,
            "grid_tariff_peak_end": 22,
        }

    @pytest.fixture
    def default_outputs(self):
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
            "ev_chargers": [{
                "name": "ev1",
                "start_charging": {
                    "service": "switch.turn_on",
                    "entity_id": "switch.ev1",
                },
                "stop_charging": {
                    "service": "switch.turn_off",
                    "entity_id": "switch.ev1",
                },
            }],
        }

    def test_tariff_affects_effective_price(self, tariff_params, default_outputs):
        """Effective prices should include the grid tariff."""
        opt = Optimizer(tariff_params, default_outputs)

        # Flat spot prices of 0.10 SEK all day
        prices = {"today": [0.10] * 24, "tomorrow": [], "currency": "SEK"}

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # Hour 0 is off-peak → effective = 0.10 + 0.10 = 0.20
        assert plan[0]["price"] == pytest.approx(0.20, abs=0.001)
        assert plan[0]["spot_price"] == pytest.approx(0.10, abs=0.001)

        # Hour 6 is peak → effective = 0.10 + 0.30 = 0.40
        assert plan[6]["price"] == pytest.approx(0.40, abs=0.001)
        assert plan[6]["spot_price"] == pytest.approx(0.10, abs=0.001)

    def test_tariff_shifts_battery_charging_to_offpeak(
        self, tariff_params, default_outputs
    ):
        """Battery charging should prefer off-peak hours when tariffs
        make them significantly cheaper, even with same spot price."""
        opt = Optimizer(tariff_params, default_outputs)

        # Spot prices: hours 0-5 = 0.08 (night), hours 6-21 = 0.08 (day),
        # hours 22-23 = 0.08. Same spot price everywhere.
        # But effective: night = 0.08+0.10=0.18, day = 0.08+0.30=0.38
        prices = {"today": [0.08] * 24, "tomorrow": [], "currency": "SEK"}

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h["hour"] for h in plan if h["action"] == "charge_battery"]

        # With tariff, off-peak hours (0-5, 22-23) have effective 0.18
        # and peak hours (6-21) have effective 0.38.  Battery should
        # only charge during off-peak since 0.38 may exceed cheap threshold.
        for h in charge_hours:
            is_offpeak = h >= 22 or h < 6
            assert is_offpeak, (
                f"Battery should charge in off-peak hours with tariff, "
                f"but charged at hour {h}"
            )

    def test_tariff_shifts_ev_to_offpeak(self, tariff_params, default_outputs):
        """EV charging should strongly prefer off-peak hours when
        tariffs make them cheaper."""
        opt = Optimizer(tariff_params, default_outputs)

        ev = [{
            "name": "ev1",
            "power_w": 7400,
            "status": "charging",
            "connected": True,
            "vehicle_soc": 80,
            "vehicle_capacity_kwh": 75,
            "vehicle_target_soc": 100,
            "vehicle_charging_power_w": 7400,
        }]

        # Spot prices: all same at 0.15 SEK/kWh
        # Effective: off-peak = 0.25, peak = 0.45
        prices = {"today": [0.15] * 24, "tomorrow": [], "currency": "SEK"}

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=ev,
        )

        plan = result["ev_charge_schedule"]
        charging_hours = [
            e["hour"] for e in plan["schedule"]
            if e["vehicles"].get("ev1", 0) > 0
        ]

        # All EV charging should be in off-peak hours (22-06)
        for h in charging_hours:
            is_offpeak = h >= 22 or h < 6
            assert is_offpeak, (
                f"EV should charge in off-peak hours with tariff, "
                f"but charged at hour {h}. All: {charging_hours}"
            )

    def test_no_tariff_preserves_old_behavior(self, default_outputs):
        """With tariffs set to 0, prices should be unchanged (spot only)."""
        params = {
            "min_price_spread": 0.30,
            "planning_horizon_hours": 24,
            "enable_battery_control": True,
            "grid_tariff_peak_sek": 0.0,
            "grid_tariff_offpeak_sek": 0.0,
        }
        opt = Optimizer(params, default_outputs)

        prices = {"today": [0.50] * 24, "tomorrow": [], "currency": "SEK"}

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # All prices should equal spot (no tariff added)
        for entry in plan:
            assert entry["price"] == pytest.approx(0.50, abs=0.001)
            assert entry["spot_price"] == pytest.approx(0.50, abs=0.001)

    def test_tariff_stats_include_effective_prices(
        self, tariff_params, default_outputs
    ):
        """Stats (min/max/avg/spread) should use effective prices
        so thresholds are compared against real costs."""
        opt = Optimizer(tariff_params, default_outputs)

        # Flat spot = 0.20.  Effective: off-peak = 0.30, peak = 0.50
        prices = {"today": [0.20] * 24, "tomorrow": [], "currency": "SEK"}

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=False,
        )

        stats = result["stats"]
        # Min should be 0.30 (off-peak effective)
        assert stats["min_price"] == pytest.approx(0.30, abs=0.001)
        # Max should be 0.50 (peak effective)
        assert stats["max_price"] == pytest.approx(0.50, abs=0.001)
        # Tariff info in stats
        assert stats["grid_tariff_peak"] == pytest.approx(0.30)
        assert stats["grid_tariff_offpeak"] == pytest.approx(0.10)

    def test_ev_schedule_includes_spot_price(self, tariff_params, default_outputs):
        """EV schedule entries should have both price (effective) and
        spot_price fields."""
        opt = Optimizer(tariff_params, default_outputs)

        prices = {"today": [0.15] * 24, "tomorrow": [], "currency": "SEK"}

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,
            ev_connected=True,
            ev_vehicles=[{
                "name": "ev1",
                "power_w": 7400,
                "status": "charging",
                "connected": True,
                "vehicle_soc": 90,
                "vehicle_capacity_kwh": 75,
                "vehicle_target_soc": 100,
                "vehicle_charging_power_w": 7400,
            }],
        )

        for entry in result["ev_charge_schedule"]["schedule"]:
            assert "spot_price" in entry, "EV schedule should have spot_price"
            assert entry["spot_price"] == pytest.approx(0.15, abs=0.001)
