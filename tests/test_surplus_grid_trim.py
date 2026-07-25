"""Tests for the Rule 3 closed-loop grid trim.

Background
==========
Live incident 24 Jul 2026: the Renault Zoe's ``set_dynamic_limit`` config
said ``phases: 1`` while the Easee actually delivered three phases
(``state_outputPhase = 30``).  The PV feed-forward loop computed ~4.4 kW
of available surplus, commanded ~19 A believing that was 4.4 kW, and the
charger drew 19 A × 230 V × 3 = ~13 kW.  The grid covered the ~8 kW
difference for ten minutes and *nothing in the loop noticed*, because a
pure feed-forward setpoint never looks at the meter.

The trim closes that loop: measured net grid import is integrated back
out of the setpoint, so any scale error (watts-per-amp, gross-DC vs AC
PV, inverter loss) or competing consumer (the battery charging off the
same solar) costs a tick or two of overdraw instead of importing
indefinitely.

These tests pin: the trim converges, decays, doesn't wind up while
idle, respects negative prices, and escalates to STOPPING rather than
growing without bound.
"""

from __future__ import annotations

import time

from custom_components.home_energy_management.surplus_controller import (
    SurplusController, SurplusState,
)


def _cfg(name: str, device_id: str, phases: int = 3) -> dict:
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


def _make_sc(chargers: list[dict], **overrides) -> SurplusController:
    params = {
        "solar_surplus_threshold_w": 2000,
        "surplus_activation_delay_s": 60,
        "surplus_deficit_timeout_s": 60,
        "surplus_safety_margin_w": 200,
        "surplus_grid_trim_gain": 0.5,
        "surplus_grid_trim_deadband_w": 100,
    }
    params.update(overrides)
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


# ── Defaults ────────────────────────────────────────────────────────

def test_trim_defaults_present():
    """Trim tuning falls back to const.py when the mapping omits it."""
    sc = SurplusController(params={}, ev_chargers_cfg=[])
    assert sc.grid_trim_gain > 0
    assert sc.grid_trim_deadband_w > 0
    assert sc._grid_trim_w == 0.0
    assert sc.status()["grid_trim_w"] == 0.0


# ── Convergence ─────────────────────────────────────────────────────

def test_import_reduces_available_power_next_tick():
    """Measured import must lower the commanded current."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    ev = [{"name": "zoe", "power_w": 6900, "connected": True}]

    # Tick 1 — balanced grid, no correction yet.
    first = sc.tick(
        grid_export_w=0, grid_import_w=0, ev_vehicles=ev, ev_connected=True,
        pv_power_w=8000, house_load_w=7900,  # non_ev_load = 1000
    )
    # available = (8000 - 1000) - 200 = 6800 → 6800 / 690 = 9 A
    assert _amps_for(first, "dev_zoe") == 9
    assert sc._grid_trim_w == 0.0

    # Tick 2 — the meter says we're importing 3 kW.
    second = sc.tick(
        grid_export_w=0, grid_import_w=3000, ev_vehicles=ev, ev_connected=True,
        pv_power_w=8000, house_load_w=7900,
    )
    # trim = 0.5 × 3000 = 1500 → available = 6800 - 1500 = 5300 → 7 A
    assert sc._grid_trim_w == 1500.0
    assert _amps_for(second, "dev_zoe") == 7


def test_trim_converges_on_scale_error():
    """The 24 Jul failure mode: charger draws 3× the assumed power.

    Simulates the real loop — the car's draw follows the commanded amps
    at 690 W/A while the controller is told 230 W/A — and asserts the
    trim walks the setpoint down until the grid stops importing.
    """
    sc = _make_sc([_cfg("zoe", "dev_zoe", phases=1)])  # WRONG constant
    _make_active(sc, ["zoe"])

    pv_w, house_base_w = 4666.0, 700.0
    ev_power_w = 0.0
    imports: list[float] = []

    for _ in range(12):
        site_load_w = house_base_w + ev_power_w
        net_w = pv_w - site_load_w
        export_w = max(0.0, net_w)
        import_w = max(0.0, -net_w)
        imports.append(import_w)

        actions = sc.tick(
            grid_export_w=export_w, grid_import_w=import_w,
            ev_vehicles=[{
                "name": "zoe", "power_w": ev_power_w, "connected": True,
            }],
            ev_connected=True,
            pv_power_w=pv_w, house_load_w=site_load_w,
        )
        amps = _amps_for(actions, "dev_zoe")
        if amps is None:      # controller gave up (STOPPING/INACTIVE)
            ev_power_w = 0.0
            continue
        # Reality: three phases, whatever the config claims.
        ev_power_w = amps * 230 * 3

    # It overdrew at first (that's the uncorrected feed-forward)...
    assert max(imports) > 3000, "expected initial overdraw to be visible"
    # ...but the loop must not still be importing many kW at the end.
    assert imports[-1] <= 1000, (
        f"trim failed to converge — still importing {imports[-1]:.0f} W "
        f"(history: {[round(i) for i in imports]})"
    )


def test_trim_decays_when_exporting_again():
    """Once solar recovers the trim must give the power back."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    sc._grid_trim_w = 2000.0
    ev = [{"name": "zoe", "power_w": 4140, "connected": True}]

    sc.tick(
        grid_export_w=1200, grid_import_w=0, ev_vehicles=ev, ev_connected=True,
        pv_power_w=9000, house_load_w=5000,
    )
    # trim = 2000 + 0.5 × (0 - 1200) = 1400
    assert sc._grid_trim_w == 1400.0

    for _ in range(6):
        sc.tick(
            grid_export_w=1200, grid_import_w=0,
            ev_vehicles=ev, ev_connected=True,
            pv_power_w=9000, house_load_w=5000,
        )
    assert sc._grid_trim_w == 0.0, "trim must decay fully, not stick"


# ── Wind-up guards ──────────────────────────────────────────────────

def test_no_windup_while_inactive():
    """House import with no cars under control must not arm the trim."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    for _ in range(5):
        sc.tick(
            grid_export_w=0, grid_import_w=4000,
            ev_vehicles=[{"name": "zoe", "power_w": 0, "connected": True}],
            ev_connected=True,
            pv_power_w=0, house_load_w=4000,
        )
    assert sc.state is SurplusState.INACTIVE
    assert sc._grid_trim_w == 0.0


def test_deadband_ignores_meter_noise():
    """Small net flows must not move the setpoint at all."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    sc.tick(
        grid_export_w=0, grid_import_w=80,   # < 100 W deadband
        ev_vehicles=[{"name": "zoe", "power_w": 4140, "connected": True}],
        ev_connected=True,
        pv_power_w=9000, house_load_w=5000,
    )
    assert sc._grid_trim_w == 0.0


def test_negative_price_bypasses_trim():
    """Negative spot price → importing is deliberate, don't fight it."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    sc._grid_trim_w = 3000.0
    sc.tick(
        grid_export_w=0, grid_import_w=6000,
        ev_vehicles=[{"name": "zoe", "power_w": 6900, "connected": True}],
        ev_connected=True,
        current_price=-0.05,
        pv_power_w=2000, house_load_w=8000,
    )
    assert sc._grid_trim_w == 0.0


def test_trim_clamped_to_surplus_and_escalates_to_stopping():
    """A persistent deficit ends in STOPPING, not an unbounded trim."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    ev = [{"name": "zoe", "power_w": 12800, "connected": True}]

    for _ in range(6):
        sc.tick(
            grid_export_w=0, grid_import_w=9000,
            ev_vehicles=ev, ev_connected=True,
            pv_power_w=4666, house_load_w=13500,
        )
        if sc.state is SurplusState.STOPPING:
            break

    assert sc.state is SurplusState.STOPPING
    # Clamped at the feed-forward surplus (pv - non_ev_load = 4666 - 700).
    assert sc._grid_trim_w <= 4666.0


def test_no_windup_while_chargers_saturated_at_min():
    """Anti-windup: a 3-phase charger's 6 A floor is ~4.1 kW, so some
    import can be irreducible.  The trim must stop growing there instead
    of winding up and delaying the later recovery to ACTIVE."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    ev = [{"name": "zoe", "power_w": 4140, "connected": True}]

    # Drive it to the min-current floor with a small residual import.
    for _ in range(3):
        sc.tick(
            grid_export_w=0, grid_import_w=174,
            ev_vehicles=ev, ev_connected=True,
            pv_power_w=4666, house_load_w=4840,
        )
    assert sc._at_min_current
    pinned_w = sc._grid_trim_w

    for _ in range(5):
        sc.tick(
            grid_export_w=0, grid_import_w=174,
            ev_vehicles=ev, ev_connected=True,
            pv_power_w=4666, house_load_w=4840,
        )
    assert sc._grid_trim_w == pinned_w, "trim wound up against a saturated actuator"

    # Sun returns → the correction must still decay.
    sc.tick(
        grid_export_w=1500, grid_import_w=0,
        ev_vehicles=ev, ev_connected=True,
        pv_power_w=9000, house_load_w=4840,
    )
    assert sc._grid_trim_w < pinned_w


def test_trim_cleared_on_deactivation():
    """Rule 5 cleanup resets the correction for the next activation."""
    sc = _make_sc([_cfg("zoe", "dev_zoe")])
    _make_active(sc, ["zoe"])
    sc._grid_trim_w = 2500.0
    sc._enter_inactive(sc._active_charger_configs())
    assert sc.state is SurplusState.INACTIVE
    assert sc._grid_trim_w == 0.0


# ── Watts-per-amp scaling (the mapping half of the fix) ─────────────

def test_three_phase_scaling_matches_available_power():
    """Commanded amps × voltage × phases must not exceed the surplus.

    Guards the mapping error directly: with 690 W/A the controller may
    only ask for ~6 A of the 4.5 kW that was available on 24 Jul, not
    the 19 A that 230 W/A produced.
    """
    sc = _make_sc([_cfg("zoe", "dev_zoe", phases=3)])
    _make_active(sc, ["zoe"])
    actions = sc.tick(
        grid_export_w=4466, grid_import_w=0,
        ev_vehicles=[{"name": "zoe", "power_w": 0, "connected": True}],
        ev_connected=True,
        pv_power_w=4666, house_load_w=0,
    )
    amps = _amps_for(actions, "dev_zoe")
    assert amps == 6, f"expected 6 A at 690 W/A, got {amps}"
    assert amps * 230 * 3 <= 4466 + 690, "commanded power exceeds surplus"
