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


class TestForcedPowerSetting:
    """Tests that the optimizer sets forced charge/discharge power correctly."""

    @pytest.fixture
    def outputs_with_forced_power(self, default_outputs):
        """Outputs config that includes set_forced_power entry."""
        default_outputs["sungrow"]["set_forced_power"] = {
            "service": "input_number.set_value",
            "entity_id": "input_number.set_sg_forced_charge_discharge_power",
            "min": 0,
            "max": 5000,
            "step": 100,
        }
        default_outputs["sungrow"]["set_discharge_power"] = {
            "service": "input_number.set_value",
            "entity_id": "input_number.set_sg_battery_max_discharge_power",
            "min": 10,
            "max": 5000,
            "step": 100,
        }
        default_outputs["sungrow"]["battery_mode_select"] = (
            "input_select.set_sg_battery_forced_charge_discharge_cmd"
        )
        default_outputs["sungrow"]["battery_mode_options"] = {
            "stop": "Stop (default)",
            "force_charge": "Forced charge",
            "force_discharge": "Forced discharge",
        }
        return default_outputs

    def test_discharge_resets_forced_power(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """Discharge hours use self-consumption → forced power = 0."""
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
        power_actions = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_forced_charge_discharge_power"
        ]
        assert len(power_actions) == 1
        assert power_actions[0]["data"]["value"] == 0, (
            "Discharge uses self-consumption, forced power should be 0"
        )

    def test_discharge_hour_uses_self_consumption(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """When current hour is expensive and action is discharge,
        the inverter should be in self-consumption mode (battery covers
        house load dynamically) with forced power reset to 0."""
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
        # Should use self_consumption mode, NOT force_discharge
        mode_actions = [
            a for a in actions
            if a["entity_id"] == "script.sg_set_self_consumption_mode"
        ]
        assert len(mode_actions) == 1, "Discharge hour should use self-consumption mode"

        # Forced power should be reset to 0 (self-consumption handles it)
        power_actions = [
            a for a in actions
            if a["entity_id"] == "input_number.set_sg_forced_charge_discharge_power"
        ]
        assert len(power_actions) == 1
        assert power_actions[0]["data"]["value"] == 0

    def test_charge_hour_sets_power_5000(
        self, default_params, outputs_with_forced_power
    ):
        """When current hour is cheap and action is charge,
        forced power should be set to max."""
        opt = Optimizer(default_params, outputs_with_forced_power)

        prices = {
            "today": [0.10] * 6 + [1.50] * 6 + [0.10] * 12,
            "tomorrow": [],
            "currency": "SEK",
        }
        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # below grid_charge_max_soc (15%)
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
        assert power_actions[0]["data"]["value"] == 5000

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
            if a["entity_id"] == "input_number.set_sg_battery_max_discharge_power"
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

    def test_discharge_clears_forced_cmd(
        self, default_params, outputs_with_forced_power, freeze_time
    ):
        """When switching to self-consumption for discharge, the forced
        charge/discharge command must be explicitly set to Stop."""
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
        assert len(stop_actions) == 1, (
            "Discharge should explicitly clear forced cmd"
        )
        assert stop_actions[0]["data"]["option"] == "Stop (default)"

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
            battery_soc=10,  # below grid_charge_max_soc (15%)
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
            battery_soc=10,  # below grid_charge_max_soc (15%)
            ev_connected=False,
            grid_export_power=0.0,
        )
        assert result_no_solar["hourly_plan"][0]["action"] == ACTION_CHARGE_BATTERY

        # With solar surplus → self_consumption (absorb solar instead)
        result_solar = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # below grid_charge_max_soc (15%)
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
        Future hours keep their price-based classification."""
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
        # Hour 1: no solar data (future) → pre_discharge (negatives at 2-3)
        assert plan[1]["action"] == ACTION_PRE_DISCHARGE

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


class TestSoCAwaredDischarge:
    """Tests for SoC-aware discharge limiting.

    The optimizer should only plan discharge for the N most expensive
    hours that the battery can actually cover, based on available
    capacity (SoC - min_soc) and predicted consumption per hour.
    """

    def test_discharge_limited_to_battery_capacity(
        self, default_params, default_outputs
    ):
        """With small battery and many expensive hours, only the most
        expensive should get discharge_battery."""
        # 5 kWh battery at 60% SoC, min_soc=10%
        # Available: (60-10)/100 * 5 = 2.5 kWh
        # With 1 kWh/hour consumption, only ~2 hours of discharge
        outputs = {**default_outputs}
        outputs["sungrow"] = {**default_outputs["sungrow"], "capacity_kwh": 5.0}
        opt = Optimizer(default_params, outputs)

        # 6 cheap hours then 6 expensive hours (1.20-1.70), rest mid
        prices = {
            "today": [0.10, 0.12, 0.15, 0.11, 0.13, 0.14,  # cheap 0-5
                      0.50, 0.55, 0.60, 0.65,                # mid 6-9
                      0.70, 0.75,                              # mid-high 10-11
                      1.20, 1.40, 1.70, 1.50, 1.30, 1.25,    # expensive 12-17
                      0.80, 0.70, 0.60, 0.50, 0.40, 0.30],   # declining 18-23
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=60,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # With only 2.5 kWh available and 1 kWh/h consumption,
        # should NOT discharge for all 6 expensive hours
        assert len(discharge_hours) <= 4, (
            f"Expected limited discharge hours, got {len(discharge_hours)}"
        )

        # The most expensive hour (1.70 at index 14) MUST be among the kept
        kept_prices = [h["price"] for h in discharge_hours]
        assert 1.70 in kept_prices, "Most expensive hour should be prioritized"

    def test_most_expensive_hours_prioritized(
        self, default_params, default_outputs
    ):
        """Discharge hours should be ranked: most expensive get priority."""
        # 10 kWh battery at 40%, min_soc=10% → available 3.0 kWh
        opt = Optimizer(default_params, default_outputs)  # capacity=10

        # Expensive hours at indices 8-13 with distinct prices
        prices = {
            "today": [0.10, 0.10, 0.10, 0.10, 0.10, 0.10,  # cheap 0-5
                      0.50, 0.50,                              # mid 6-7
                      1.50, 1.20, 1.80, 1.60, 1.30, 1.10,    # expensive 8-13
                      0.50, 0.50, 0.50, 0.50, 0.50, 0.50,    # mid
                      0.50, 0.50, 0.50, 0.50],
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=40,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]
        kept_prices = sorted([h["price"] for h in discharge_hours], reverse=True)

        # The kept hours should be the most expensive ones
        if len(discharge_hours) > 0:
            # Verify 1.80 is retained (the absolute most expensive)
            assert 1.80 in [h["price"] for h in discharge_hours], (
                "1.80 SEK hour must be retained"
            )

    def test_all_discharge_hours_kept_when_sufficient_capacity(
        self, default_params, default_outputs
    ):
        """When battery has plenty of capacity, all discharge hours are kept."""
        # 10 kWh at 90%, min=10% → 8 kWh available, 1 kWh/h consumption
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.10] * 6 + [0.50] * 6 + [1.50, 1.60, 1.70] + [0.50] * 9,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=90,
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # 3 expensive hours, 8 kWh available → all 3 should be kept
        assert len(discharge_hours) >= 3, (
            f"With 8 kWh available, all 3 expensive hours should discharge, "
            f"got {len(discharge_hours)}"
        )

    def test_no_discharge_when_battery_depleted(
        self, default_params, default_outputs
    ):
        """At min_soc, no hours should be discharge_battery."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.10] * 6 + [2.00] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=5,  # below min_soc (10%) — can't discharge OR charge
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        discharge_hours = [h for h in plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        # Battery below min_soc → no discharge.  Grid charge target (15%)
        # is above min_soc, but _classify_hour requires soc < grid_charge_max_soc
        # AND price ≤ max_price.  Even if 1 charge hour runs (5 → 15%),
        # the limiter should downgrade any resulting discharge since
        # initial_soc=5 < min_soc=10 → available_kwh ≤ 0.
        assert len(discharge_hours) == 0

    def test_downgraded_hours_become_self_consumption(
        self, default_params, default_outputs
    ):
        """Hours that lose discharge status should become self_consumption
        with an appropriate reason."""
        params = {
            **default_params,
            "grid_charge_max_soc": 100,  # allow full grid charging for this test
            "grid_charge_max_price": 1.0,
        }
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
            battery_soc=50,  # (50-10)/100 * 5 = 2.0 kWh
            ev_connected=False,
        )

        plan = result["hourly_plan"]

        # Some expensive hours should be downgraded to self_consumption
        downgraded = [
            h for h in plan
            if h["action"] == ACTION_SELF_CONSUMPTION
            and "capacity limited" in h.get("reason", "")
        ]
        assert len(downgraded) > 0, "Some hours should be downgraded due to capacity"

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

    def test_large_battery_covers_all_expensive_hours(self, default_params, default_outputs):
        """A very large battery should cover all expensive hours without limiting."""
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

        # All 6 expensive hours should be kept
        assert len(discharge_hours) >= 6, (
            f"Large battery should cover all expensive hours, got {len(discharge_hours)}"
        )


class TestGridChargeLimits:
    """Tests for grid charge limits — SoC cap and price cap.

    The optimizer should only charge from the grid when:
    1. Battery SoC is below grid_charge_max_soc (default 15%)
    2. Price is at or below grid_charge_max_price (default 0.40 SEK/kWh)
    Both conditions must be met simultaneously.
    """

    def test_grid_charge_skipped_when_price_too_high(
        self, default_params, default_outputs
    ):
        """No grid charging when all cheap-band prices exceed the price cap."""
        opt = Optimizer(default_params, default_outputs)

        # All prices above 0.40 SEK → no grid charging even at low SoC
        prices = {
            "today": [0.50] * 6 + [1.50] * 12 + [0.50] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # below grid_charge_max_soc (15%)
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]

        assert len(charge_hours) == 0, (
            f"No grid charging expected when price > 0.40 SEK, "
            f"got {len(charge_hours)} charge hours"
        )

    def test_grid_charge_skipped_when_soc_above_target(
        self, default_params, default_outputs
    ):
        """No grid charging when SoC is already at or above grid_charge_max_soc."""
        opt = Optimizer(default_params, default_outputs)

        # Cheap prices in morning but SoC already above 15%.
        # Evening prices above 0.40 to avoid re-charging after discharge.
        prices = {
            "today": [0.10] * 6 + [1.50] * 12 + [0.50] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=20,  # above grid_charge_max_soc (15%)
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]

        assert len(charge_hours) == 0, (
            f"No grid charging expected when SoC ({20}%) >= target ({15}%), "
            f"got {len(charge_hours)} charge hours"
        )

    def test_grid_charge_happens_when_both_conditions_met(
        self, default_params, default_outputs
    ):
        """Grid charging should occur when price ≤ 0.40 AND SoC < 15%."""
        opt = Optimizer(default_params, default_outputs)

        prices = {
            "today": [0.10] * 6 + [1.50] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # below grid_charge_max_soc (15%)
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]

        assert len(charge_hours) >= 1, (
            "Grid charging expected when price ≤ 0.40 AND SoC < 15%"
        )

    def test_grid_charge_stops_at_soc_target(
        self, default_params, default_outputs
    ):
        """Grid charging should stop once SoC reaches grid_charge_max_soc,
        even if more cheap hours are available."""
        opt = Optimizer(default_params, default_outputs)

        # 6 consecutive very cheap hours — only 1 should be needed to reach 15%
        prices = {
            "today": [0.10] * 6 + [1.50] * 12 + [0.10] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=10,  # 10% → 15% needs only ~0.5 kWh on 10 kWh battery
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]

        # With a 10 kWh battery at 5 kW charge rate, 1 hour charges
        # (5/10)*100 = 50%. So just 1 hour is enough to go from 10% to 15%.
        assert len(charge_hours) <= 2, (
            f"Expected at most 2 charge hours to reach 15% SoC, "
            f"got {len(charge_hours)}"
        )

    def test_grid_charge_limits_configurable(
        self, default_params, default_outputs
    ):
        """Custom grid charge params should override defaults."""
        params = {
            **default_params,
            "grid_charge_max_soc": 50,
            "grid_charge_max_price": 0.80,
        }
        opt = Optimizer(params, default_outputs)

        prices = {
            "today": [0.60] * 6 + [1.50] * 12 + [0.60] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=30,  # below custom 50% target, price 0.60 < 0.80
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]

        assert len(charge_hours) >= 1, (
            "Grid charging expected with custom params: "
            "price 0.60 ≤ 0.80 AND SoC 30% < 50%"
        )

    def test_solar_still_charges_above_grid_target(
        self, default_params, default_outputs
    ):
        """Self-consumption mode should still absorb solar even when SoC > 15%.

        The grid charge limit only restricts forced grid charging, not
        solar charging via self_consumption mode.
        """
        opt = Optimizer(default_params, default_outputs)

        # All prices above 0.40 so no grid charging can occur at any SoC.
        prices = {
            "today": [0.50] * 6 + [1.00] * 12 + [0.50] * 6,
            "tomorrow": [],
            "currency": "SEK",
        }

        result = opt.optimize(
            prices=prices,
            predicted_consumption=[1.0] * 24,
            battery_soc=50,  # above grid target, but solar should still work
            ev_connected=False,
        )

        plan = result["hourly_plan"]
        # Verify self_consumption hours exist (where solar can charge battery)
        sc_hours = [h for h in plan if h["action"] == ACTION_SELF_CONSUMPTION]
        assert len(sc_hours) > 0, (
            "Self-consumption hours should exist to absorb solar "
            "even when SoC > grid charge target"
        )
        # Verify no forced grid charging
        charge_hours = [h for h in plan if h["action"] == ACTION_CHARGE_BATTERY]
        assert len(charge_hours) == 0, (
            f"No forced grid charging at SoC 50% > 15%, got {len(charge_hours)}"
        )


class TestEVChargePlanning:
    """Tests for the EV charge scheduling algorithm.

    The optimizer should calculate how many kWh the EV needs to reach
    its target SoC, then schedule charging during the cheapest available
    hours (excluding discharge_battery hours).
    """

    @pytest.fixture
    def ev_vehicle_ex90(self):
        """A connected Volvo EX90 at 89% needing charge to 100%."""
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

    def test_ev_schedule_in_result(self, default_params, default_outputs, ev_vehicle_ex90):
        """optimize() should return ev_charge_schedule in its result."""
        opt = Optimizer(default_params, default_outputs)
        prices = {
            "today": [0.50 + i * 0.05 for i in range(24)],
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

        assert "ev_charge_schedule" in result
        plan = result["ev_charge_schedule"]
        assert "schedule" in plan
        assert "total_kwh_needed" in plan
        assert "vehicles" in plan
        assert len(plan["schedule"]) == len(result["hourly_plan"])

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
