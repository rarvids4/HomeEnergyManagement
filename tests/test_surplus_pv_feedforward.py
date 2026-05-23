"""Tests for the battery-independent (PV feed-forward) surplus surplus calc.

Background
==========
Live observation 23 May 2026: the LP discharged the battery at 5 kW to
offset house load on an expensive hour, while PV produced only 2.6 kW.
With the legacy ``grid_export - grid_import + ev_power`` formula the
SurplusController saw ~8 kW of "surplus" (because the battery was
propping up the grid balance) and let both cars charge full-tilt,
draining the battery.

These tests pin the corrected behaviour: when ``pv_power_w`` and
``house_load_w`` are supplied, the controller uses
``pv - (load - ev)`` instead of grid balance, and the cars only see
the true solar headroom.
"""

from __future__ import annotations

import time

from custom_components.home_energy_management.surplus_controller import (
    SurplusController, SurplusState,
)


def _cfg(name: str, device_id: str, phases: int = 1) -> dict:
    return {
        "name": name,
        "start_charging": {
            "service": "switch.turn_on", "entity_id": f"switch.{name}_on",
        },
        "stop_charging": {
            "service": "switch.turn_off", "entity_id": f"switch.{name}_off",
        },
        "set_dynamic_limit": {
            "service": "easee.set_charger_dynamic_limit",
            "device_id": device_id,
            "voltage": 230, "phases": phases,
            "min_current": 6, "max_current": 32,
        },
    }


def _make_sc(chargers: list[dict]) -> SurplusController:
    params = {
        "solar_surplus_threshold_w": 2000,
        "surplus_activation_delay_s": 60,
        "surplus_deficit_timeout_s": 60,
        "surplus_safety_margin_w": 200,
    }
    return SurplusController(params=params, ev_chargers_cfg=chargers)


def _make_active(sc: SurplusController, names: list[str]) -> None:
    sc.state = SurplusState.ACTIVE
    sc._state_entered = time.monotonic()
    sc._active_charger_names = list(names)


def _amps_for(actions: list[dict], device_id: str) -> int | None:
    for a in actions:
        if a.get("device_id") == device_id and "current" in a.get("data", {}):
            return a["data"]["current"]
    return None


# ── _compute_solar_surplus_w (the new pure function) ────────────────

def test_compute_solar_surplus_fallback_when_pv_missing():
    """Legacy callers (no PV/load) get the old grid-balance result."""
    s = SurplusController._compute_solar_surplus_w(
        grid_export_w=2000, grid_import_w=0,
        pv_power_w=None, house_load_w=None, ev_vehicles=[],
    )
    assert s == 2000.0


def test_compute_solar_surplus_pv_feedforward_battery_idle():
    """PV 5 kW, house 1 kW, no cars → 4 kW surplus (batteries irrelevant)."""
    s = SurplusController._compute_solar_surplus_w(
        grid_export_w=0, grid_import_w=0,  # values don't matter
        pv_power_w=5000, house_load_w=1000, ev_vehicles=[],
    )
    assert s == 4000.0


def test_compute_solar_surplus_ignores_battery_discharge():
    """The whole point: battery flow MUST NOT inflate the surplus.

    Scenario from the live bug: PV=2.6 kW, house load=1 kW,
    battery discharging 5 kW (so grid balance shows 6.6 kW going
    somewhere). True solar headroom is only 1.6 kW.
    """
    s = SurplusController._compute_solar_surplus_w(
        grid_export_w=6600, grid_import_w=0,  # battery is faking export
        pv_power_w=2600, house_load_w=1000, ev_vehicles=[],
    )
    assert s == 1600.0  # the real, battery-independent number


def test_compute_solar_surplus_subtracts_only_non_ev_load():
    """EV power inside house_load_w is added back so the calc is invariant
    to whether the meter reads the cars before or after."""
    evs = [{"name": "ex90", "power_w": 4000}]
    s = SurplusController._compute_solar_surplus_w(
        grid_export_w=0, grid_import_w=0,
        pv_power_w=6000, house_load_w=5000,  # 1 kW house + 4 kW car
        ev_vehicles=evs,
    )
    # non_ev_load = 5000 - 4000 = 1000 ; surplus = 6000 - 1000 = 5000
    assert s == 5000.0


def test_compute_solar_surplus_clamps_non_ev_load_nonneg():
    """If sensor timing makes ev_power > house_load momentarily, don't
    over-credit the cars by treating the difference as 'extra solar'."""
    evs = [{"name": "ex90", "power_w": 9000}]
    s = SurplusController._compute_solar_surplus_w(
        grid_export_w=0, grid_import_w=0,
        pv_power_w=3000, house_load_w=8000,  # car reads higher than load
        ev_vehicles=evs,
    )
    # non_ev_load clamped to 0 ; surplus = 3000 - 0 = 3000
    assert s == 3000.0


# ── End-to-end: tick() honours PV feed-forward when active ──────────

def test_active_state_uses_pv_not_grid_balance():
    """During battery discharge: cars get amps matching true solar only."""
    sc = _make_sc([_cfg("ex90", "dev_ex90", phases=1)])
    _make_active(sc, ["ex90"])

    # Live-bug scenario: grid balance looks like 5 kW exporting because
    # battery is discharging. True PV is only 3 kW.
    actions = sc.tick(
        grid_export_w=5000, grid_import_w=0,
        ev_vehicles=[{"name": "ex90", "power_w": 0, "connected": True}],
        ev_connected=True,
        pv_power_w=3000, house_load_w=500,
    )
    amps = _amps_for(actions, "dev_ex90")
    # available = (pv - non_ev_load) - margin = (3000 - 500) - 200 = 2300 W
    # 2300 / (230*1) = 10 A  (instead of the 21 A the legacy formula would give)
    assert amps == 10, f"expected 10 A from PV feed-forward, got {amps}"


def test_active_state_legacy_call_still_uses_grid_balance():
    """Backward compat: callers without pv/load args see old behaviour."""
    sc = _make_sc([_cfg("ex90", "dev_ex90", phases=1)])
    _make_active(sc, ["ex90"])
    actions = sc.tick(
        grid_export_w=5000, grid_import_w=0,
        ev_vehicles=[{"name": "ex90", "power_w": 0, "connected": True}],
        ev_connected=True,
        # NO pv_power_w / house_load_w
    )
    amps = _amps_for(actions, "dev_ex90")
    # Legacy formula: (export - import + ev) - margin = 5000 - 200 = 4800
    # 4800 / 230 = 20 A
    assert amps == 20, f"expected 20 A from legacy formula, got {amps}"


def test_battery_discharge_triggers_stopping():
    """Cars currently drawing surplus → battery starts discharging →
    true solar drops below min_viable → STOPPING within one tick."""
    sc = _make_sc([_cfg("ex90", "dev_ex90", phases=1)])
    _make_active(sc, ["ex90"])

    # Grid still shows export thanks to battery, but PV crashed.
    sc.tick(
        grid_export_w=4000, grid_import_w=0,
        ev_vehicles=[{"name": "ex90", "power_w": 4000, "connected": True}],
        ev_connected=True,
        pv_power_w=500, house_load_w=500,  # surplus = 0 W
    )
    assert sc.state == SurplusState.STOPPING


def test_dual_charger_split_uses_pv_only():
    """With both cars active, each gets half of true PV surplus."""
    sc = _make_sc([
        _cfg("ex90", "dev_ex90", phases=3),
        _cfg("zoe",  "dev_zoe",  phases=1),
    ])
    _make_active(sc, ["ex90", "zoe"])

    actions = sc.tick(
        grid_export_w=8000, grid_import_w=0,  # battery contributing heavily
        ev_vehicles=[
            {"name": "ex90", "power_w": 4000, "connected": True},
            {"name": "zoe",  "power_w": 4000, "connected": True},
        ],
        ev_connected=True,
        pv_power_w=6000, house_load_w=8500,  # non_ev_load = 500
    )
    # surplus = 6000 - 500 - 200 = 5300 W ; per charger = 2650 W
    # ex90 (3-phase): 2650 / 690 = 3 → clamped to min 6 A
    # zoe  (1-phase): 2650 / 230 = 11 A
    ex = _amps_for(actions, "dev_ex90")
    zo = _amps_for(actions, "dev_zoe")
    assert ex == 6,  f"ex90 should clamp to min 6 A, got {ex}"
    assert zo == 11, f"zoe should get 11 A, got {zo}"
