"""Time-series integration tests — solar surplus, dynamic EV current, export limit.

Generic version — no hardware-specific entity IDs.  Works for any user of the
integration regardless of inverter brand, EV model, or charger service.

Scenario
--------
* 12 ticks (10-min interval) over 2 hours of simulated solar + house load data.
* Solar production varies 3–7 kW with white Gaussian noise (seed fixed for
  reproducibility).
* Ticks 0–5  — positive spot price (+0.50 SEK/kWh).
* Ticks 6–11 — negative spot price (−0.10 SEK/kWh).
* One EV is connected at 40 % SoC with a target of 100 %.

Three behaviours are verified:

1. **Dynamic current** — ``build_surplus_current_update`` emits a current-limit
   action that scales proportionally with available export power.

2. **Surplus charging activation** — ``build_immediate_actions`` turns the EV
   charger on and arms ``surplus_charger_name`` whenever export ≥ viable threshold.

3. **Export limit set / reset** — a negative spot price enables the inverter
   export cap; a subsequent positive price disables it.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

import pytest

from custom_components.home_energy_management.action_builder import ActionBuilder
from custom_components.home_energy_management.const import (
    ACTION_MAXIMIZE_LOAD,
    ACTION_SELF_CONSUMPTION,
)

# ---------------------------------------------------------------------------
# Time-series parameters
# ---------------------------------------------------------------------------

_SEED = 42
_BASE_SOLAR_KW = [3.0, 4.0, 5.0, 6.0, 7.0, 6.5, 6.0, 5.5, 5.0, 4.0, 4.5, 3.5]
_HOUSE_LOAD_KW = 1.5
_NOISE_STD_KW = 0.30
_SURPLUS_THRESHOLD_W = 2_000
_NEGATIVE_TICK_START = 6          # ticks 0-5 = positive; 6-11 = negative

# Generic entity / service names — not tied to any real hardware
_CHARGER_ON_SERVICE   = "switch.turn_on"
_CHARGER_ON_ENTITY    = "switch.ev_charger_1_enabled"
_CHARGER_OFF_SERVICE  = "switch.turn_off"
_CHARGER_OFF_ENTITY   = "switch.ev_charger_1_enabled"
_DYN_LIMIT_SERVICE    = "ev_integration.set_charger_dynamic_limit"
_DYN_LIMIT_DEVICE     = "ev_charger_device_1"
_EXPORT_LIMIT_ENTITY  = "input_number.inverter_export_power_limit"
_EXPORT_MODE_ENTITY   = "input_select.inverter_export_power_limit_mode"
_SC_ENTITY            = "script.inverter_set_self_consumption_mode"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timeseries() -> list[dict[str, float]]:
    rng = random.Random(_SEED)
    series = []
    for i, base in enumerate(_BASE_SOLAR_KW):
        solar_kw = max(0.0, base + rng.gauss(0, _NOISE_STD_KW))
        export_w = max(0.0, (solar_kw - _HOUSE_LOAD_KW) * 1_000)
        spot = -0.10 if i >= _NEGATIVE_TICK_START else 0.50
        series.append({"tick": i, "solar_kw": solar_kw, "grid_export_w": export_w, "spot_price": spot})
    return series


def _dyn_limit_actions(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a.get("service") == _DYN_LIMIT_SERVICE]


def _export_limit_value_actions(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a.get("entity_id") == _EXPORT_LIMIT_ENTITY]


def _export_mode_actions(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a.get("entity_id") == _EXPORT_MODE_ENTITY]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def timeseries():
    return _make_timeseries()


@pytest.fixture
def generic_outputs() -> dict[str, Any]:
    """Generic output mapping — replace entity IDs with real values in production."""
    return {
        "sungrow": {
            "force_charge": {
                "service": "script.turn_on",
                "entity_id": "script.inverter_set_forced_charge_mode",
            },
            "force_discharge": {
                "service": "script.turn_on",
                "entity_id": "script.inverter_set_forced_discharge_mode",
            },
            "self_consumption": {
                "service": "script.turn_on",
                "entity_id": _SC_ENTITY,
            },
            "set_export_limit": {
                "service": "input_number.set_value",
                "entity_id": _EXPORT_LIMIT_ENTITY,
                "mode_entity_id": _EXPORT_MODE_ENTITY,
                "mode_enabled": "Enabled",
                "mode_disabled": "Disabled",
                "max": 10_000,
                "min": 0,
                "negative_price_limit": 0,
            },
            "min_soc": 10,
            "max_soc": 100,
            "capacity_kwh": 10.0,
        },
        "ev_chargers": [
            {
                "name": "ev_charger_1",
                "start_charging": {
                    "service": _CHARGER_ON_SERVICE,
                    "entity_id": _CHARGER_ON_ENTITY,
                },
                "stop_charging": {
                    "service": _CHARGER_OFF_SERVICE,
                    "entity_id": _CHARGER_OFF_ENTITY,
                },
                "set_dynamic_limit": {
                    "service": _DYN_LIMIT_SERVICE,
                    "device_id": _DYN_LIMIT_DEVICE,
                    "voltage": 230,
                    "phases": 3,
                    "min_current": 6,
                    "max_current": 32,
                },
            }
        ],
    }


@pytest.fixture
def generic_params() -> dict[str, Any]:
    return {
        "solar_surplus_threshold_w": _SURPLUS_THRESHOLD_W,
        "surplus_safety_margin_w": 200,
        "surplus_grid_import_grace_seconds": 60,
        "enable_battery_control": True,
        "enable_charger_control": True,
        "neg_price_ev_export_limit_w": 2_100,
    }


@pytest.fixture
def ev_vehicles() -> list[dict[str, Any]]:
    return [
        {
            "name": "ev_charger_1",
            "connected": True,
            "vehicle_soc": 40.0,
            "vehicle_target_soc": 100,
            "power_w": 0.0,
        }
    ]


def _build_ab(generic_params, generic_outputs) -> ActionBuilder:
    return ActionBuilder(generic_params, generic_outputs)


# ---------------------------------------------------------------------------
# 1. Dynamic current
# ---------------------------------------------------------------------------

class TestDynamicCurrent:
    """Dynamic charger current must track available solar export power."""

    def test_current_scales_with_export_sweep(self, generic_params, generic_outputs):
        """Amperage must increase as export power rises (deterministic sweep)."""
        ab = _build_ab(generic_params, generic_outputs)
        ab.surplus_charger_name = "ev_charger_1"
        ab.surplus_charger_cfg = generic_outputs["ev_chargers"][0]
        ab._surplus_deficit_since = None

        voltage, phases, margin = 230, 3, 200
        prev_amps = None
        for export_w in [3_000, 4_000, 5_000, 6_000, 7_000]:
            actions = ab.build_surplus_current_update(
                grid_export_w=export_w, current_ev_power_w=0.0
            )
            dyn = _dyn_limit_actions(actions)
            assert dyn, f"No dynamic limit action at {export_w} W export"

            amps = dyn[0]["data"]["current"]
            assert 6 <= amps <= 32, f"Amps {amps} out of [6, 32] range"

            expected = max(6, min(32, int((export_w - margin) / (voltage * phases))))
            assert amps == expected, f"export={export_w} W → expected {expected} A, got {amps}"

            if prev_amps is not None:
                assert amps >= prev_amps, (
                    f"Amps fell from {prev_amps} to {amps} as export rose to {export_w} W"
                )
            prev_amps = amps

    def test_current_tracks_noisy_timeseries(self, timeseries, generic_params, generic_outputs):
        """Fast-loop updates over the noisy series must always stay within bounds
        and never decrease when export power increases."""
        ab = _build_ab(generic_params, generic_outputs)
        ab.surplus_charger_name = "ev_charger_1"
        ab.surplus_charger_cfg = generic_outputs["ev_chargers"][0]
        ab._surplus_deficit_since = None

        surplus_ticks = [t for t in timeseries if t["grid_export_w"] >= _SURPLUS_THRESHOLD_W]
        assert len(surplus_ticks) >= 3

        prev_amps, prev_export = None, None
        for tick in surplus_ticks:
            actions = ab.build_surplus_current_update(
                grid_export_w=tick["grid_export_w"], current_ev_power_w=0.0
            )
            dyn = _dyn_limit_actions(actions)
            assert dyn, f"Tick {tick['tick']}: no dynamic limit action"
            amps = dyn[0]["data"]["current"]
            assert 6 <= amps <= 32

            if prev_amps is not None and prev_export is not None:
                if tick["grid_export_w"] > prev_export:
                    assert amps >= prev_amps, (
                        f"Tick {tick['tick']}: export rose but amps fell "
                        f"({prev_amps}→{amps} A)"
                    )
            prev_amps, prev_export = amps, tick["grid_export_w"]


# ---------------------------------------------------------------------------
# 2. Surplus charging activation
# ---------------------------------------------------------------------------

class TestSurplusChargingActivation:
    """EV charger must start and dynamic current must be set when export is sufficient."""

    def test_charger_starts_above_threshold(self, generic_params, generic_outputs, ev_vehicles):
        ab = _build_ab(generic_params, generic_outputs)
        now = datetime(2025, 6, 15, 11, 0)

        actions = ab.build_immediate_actions(
            action=ACTION_SELF_CONSUMPTION,
            ev_connected=True,
            current_price=0.50,
            spot_price=0.50,
            avg_price=0.50,
            min_price=0.40,
            price_spread=0.10,
            grid_export_w=6_000.0,
            ev_vehicles=ev_vehicles,
            ev_charge_plan=None,
            now=now,
            predicted_consumption=_HOUSE_LOAD_KW,
            predicted_solar=7.5,
        )

        ev_on = [a for a in actions if a.get("service") == _CHARGER_ON_SERVICE]
        assert ev_on, "EV charger must be turned on when export exceeds threshold"
        assert ab.surplus_charger_name == "ev_charger_1"

        dyn = _dyn_limit_actions(actions)
        assert dyn, "set_dynamic_limit action expected alongside charger start"
        assert 6 <= dyn[0]["data"]["current"] <= 32

    def test_charger_stays_off_below_threshold(self, generic_params, generic_outputs, ev_vehicles):
        """Charger must NOT start when export is below the viable minimum."""
        ab = _build_ab(generic_params, generic_outputs)
        now = datetime(2025, 6, 15, 8, 0)

        actions = ab.build_immediate_actions(
            action=ACTION_SELF_CONSUMPTION,
            ev_connected=True,
            current_price=0.50,
            spot_price=0.50,
            avg_price=0.50,
            min_price=0.40,
            price_spread=0.10,
            grid_export_w=1_500.0,   # below 2 kW threshold
            ev_vehicles=ev_vehicles,
            ev_charge_plan=None,
            now=now,
            predicted_consumption=_HOUSE_LOAD_KW,
            predicted_solar=3.0,
        )

        ev_on = [a for a in actions if a.get("service") == _CHARGER_ON_SERVICE]
        assert not ev_on, "EV charger must NOT start below surplus threshold"
        assert ab.surplus_charger_name is None

    def test_charger_activates_across_noisy_ticks(
        self, timeseries, generic_params, generic_outputs, ev_vehicles
    ):
        """For every positive-price tick with enough export, charger must start."""
        ab = _build_ab(generic_params, generic_outputs)
        now = datetime(2025, 6, 15, 10, 0)
        min_viable_w = max(_SURPLUS_THRESHOLD_W, 6 * 230 * 3)  # 4140 W

        for tick in timeseries:
            if tick["spot_price"] < 0:
                continue
            actions = ab.build_immediate_actions(
                action=ACTION_SELF_CONSUMPTION,
                ev_connected=True,
                current_price=tick["spot_price"],
                spot_price=tick["spot_price"],
                avg_price=0.50,
                min_price=0.40,
                price_spread=0.10,
                grid_export_w=tick["grid_export_w"],
                ev_vehicles=ev_vehicles,
                ev_charge_plan=None,
                now=now,
                predicted_consumption=_HOUSE_LOAD_KW,
                predicted_solar=tick["solar_kw"],
            )
            if tick["grid_export_w"] >= min_viable_w:
                ev_on = [a for a in actions if a.get("service") == _CHARGER_ON_SERVICE]
                assert ev_on, (
                    f"Tick {tick['tick']}: export={tick['grid_export_w']:.0f} W "
                    f">= viable={min_viable_w} W but charger not started"
                )


# ---------------------------------------------------------------------------
# 3. Export limit set / reset
# ---------------------------------------------------------------------------

class TestExportLimit:
    """Export cap must be Enabled during negative prices and Disabled otherwise."""

    def test_limit_enabled_during_negative_price(
        self, generic_params, generic_outputs, ev_vehicles
    ):
        ab = _build_ab(generic_params, generic_outputs)
        actions = ab.build_immediate_actions(
            action=ACTION_MAXIMIZE_LOAD,
            ev_connected=True,
            current_price=-0.10,
            spot_price=-0.10,
            avg_price=0.30,
            min_price=-0.10,
            price_spread=0.60,
            grid_export_w=5_500.0,
            ev_vehicles=ev_vehicles,
            ev_charge_plan=None,
            now=datetime(2025, 6, 15, 13, 0),
            predicted_consumption=_HOUSE_LOAD_KW,
            predicted_solar=7.0,
        )

        mode = _export_mode_actions(actions)
        assert mode, "Export limit mode action expected during negative price"
        assert mode[0]["data"]["option"] == "Enabled"

        limit = _export_limit_value_actions(actions)
        assert limit, "Export limit value action expected during negative price"
        assert 0 <= limit[0]["data"]["value"] <= 2_100

    def test_limit_disabled_during_positive_price(
        self, generic_params, generic_outputs, ev_vehicles
    ):
        ab = _build_ab(generic_params, generic_outputs)
        actions = ab.build_immediate_actions(
            action=ACTION_SELF_CONSUMPTION,
            ev_connected=True,
            current_price=0.50,
            spot_price=0.50,
            avg_price=0.50,
            min_price=0.40,
            price_spread=0.10,
            grid_export_w=4_000.0,
            ev_vehicles=ev_vehicles,
            ev_charge_plan=None,
            now=datetime(2025, 6, 15, 14, 0),
            predicted_consumption=_HOUSE_LOAD_KW,
            predicted_solar=5.5,
        )

        mode = _export_mode_actions(actions)
        assert mode, "Export limit mode action expected during positive price"
        assert mode[0]["data"]["option"] == "Disabled"

    def test_limit_toggles_correctly_across_full_timeseries(
        self, timeseries, generic_params, generic_outputs, ev_vehicles
    ):
        """Walk all 12 ticks: Enabled during negative price, Disabled during positive."""
        ab = _build_ab(generic_params, generic_outputs)
        now = datetime(2025, 6, 15, 10, 0)

        for tick in timeseries:
            spot = tick["spot_price"]
            action = ACTION_MAXIMIZE_LOAD if spot < 0 else ACTION_SELF_CONSUMPTION
            actions = ab.build_immediate_actions(
                action=action,
                ev_connected=True,
                current_price=spot,
                spot_price=spot,
                avg_price=0.30,
                min_price=-0.10,
                price_spread=0.60,
                grid_export_w=tick["grid_export_w"],
                ev_vehicles=ev_vehicles,
                ev_charge_plan=None,
                now=now,
                predicted_consumption=_HOUSE_LOAD_KW,
                predicted_solar=tick["solar_kw"],
            )

            mode = _export_mode_actions(actions)
            assert mode, f"Tick {tick['tick']}: no export limit mode action"
            expected = "Disabled" if spot >= 0 else "Enabled"
            assert mode[0]["data"]["option"] == expected, (
                f"Tick {tick['tick']} (spot={spot}): "
                f"expected '{expected}', got '{mode[0]['data']['option']}'"
            )

            if spot < 0:
                limit = _export_limit_value_actions(actions)
                assert limit, f"Tick {tick['tick']}: no export limit value action"
                assert 0 <= limit[0]["data"]["value"] <= 2_100


# ---------------------------------------------------------------------------
# Combined smoke test
# ---------------------------------------------------------------------------

class TestFullScenario:
    """All three behaviours must appear at least once across the 2-hour series."""

    def test_all_behaviours_observed(
        self, timeseries, generic_params, generic_outputs, ev_vehicles
    ):
        ab = _build_ab(generic_params, generic_outputs)
        now = datetime(2025, 6, 15, 10, 0)

        surplus_activations = 0
        dynamic_limit_calls = 0
        enabled_ticks = 0
        disabled_ticks = 0

        for tick in timeseries:
            spot = tick["spot_price"]
            action = ACTION_MAXIMIZE_LOAD if spot < 0 else ACTION_SELF_CONSUMPTION
            actions = ab.build_immediate_actions(
                action=action,
                ev_connected=True,
                current_price=spot,
                spot_price=spot,
                avg_price=0.30,
                min_price=-0.10,
                price_spread=0.60,
                grid_export_w=tick["grid_export_w"],
                ev_vehicles=ev_vehicles,
                ev_charge_plan=None,
                now=now,
                predicted_consumption=_HOUSE_LOAD_KW,
                predicted_solar=tick["solar_kw"],
            )

            if ab.surplus_charger_name:
                surplus_activations += 1
            dynamic_limit_calls += len(_dyn_limit_actions(actions))
            for m in _export_mode_actions(actions):
                if m["data"]["option"] == "Enabled":
                    enabled_ticks += 1
                else:
                    disabled_ticks += 1

        positive_ticks = len(timeseries) - _NEGATIVE_TICK_START
        negative_ticks = _NEGATIVE_TICK_START

        assert surplus_activations >= 1, "Surplus charging must activate at least once"
        assert dynamic_limit_calls >= 1, "Dynamic current limit must be set at least once"
        assert enabled_ticks == negative_ticks, (
            f"Export limit Enabled expected {negative_ticks}× (neg-price ticks), "
            f"got {enabled_ticks}"
        )
        assert disabled_ticks == positive_ticks, (
            f"Export limit Disabled expected {positive_ticks}× (pos-price ticks), "
            f"got {disabled_ticks}"
        )
