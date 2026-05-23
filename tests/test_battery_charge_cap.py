"""Tests for the dynamic battery charge-cap controller.

The cap is recomputed every cycle from the LP's target SoC and the
actual SoC, mirroring the closed-loop control already used for the
EV chargers.
"""

from custom_components.home_energy_management.action_builder import (
    ActionBuilder,
    _BATT_CHARGE_CAP_MIN_W,
    _BATT_CHARGE_CAP_STEP_W,
)
from custom_components.home_energy_management.const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_MAXIMIZE_LOAD,
    ACTION_PRE_DISCHARGE,
    ACTION_SELF_CONSUMPTION,
    OUTPUT_SUNGROW,
)


def _make_ab(cap_max: int = 5000, cap_min: int = 0, capacity_kwh: float = 24.06):
    params = {"enable_battery_control": True, "enable_charger_control": False}
    outputs = {
        OUTPUT_SUNGROW: {
            "capacity_kwh": capacity_kwh,
            "max_soc": 100,
            "min_soc": 5,
            "self_consumption": {"service": "script.turn_on", "entity_id": "script.sc"},
            "force_charge": {"service": "script.turn_on", "entity_id": "script.fc"},
            "force_discharge": {"service": "script.turn_on", "entity_id": "script.fd"},
            "set_forced_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.forced_power",
                "min": 0, "max": cap_max,
            },
            "set_charge_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.max_charge",
                "min": cap_min, "max": cap_max,
            },
            "set_discharge_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.max_discharge",
                "min": 0, "max": cap_max,
            },
        }
    }
    return ActionBuilder(params, outputs), outputs


def _charge_cap_value(actions):
    """Extract the value written to the max_charge input_number."""
    for a in actions:
        if a.get("entity_id") == "input_number.max_charge":
            return a["data"]["value"]
    return None


# ── compute_charge_cap unit tests ─────────────────────────────────────

def test_charge_cap_max_during_maximize_load():
    ab, outputs = _make_ab(cap_max=5000)
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_MAXIMIZE_LOAD,
        current_soc=50, target_soc=50,
    )
    assert cap == 5000


def test_charge_cap_min_during_pre_discharge():
    ab, outputs = _make_ab()
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_PRE_DISCHARGE,
        current_soc=60, target_soc=80,
    )
    assert cap == _BATT_CHARGE_CAP_MIN_W


def test_charge_cap_self_consumption_no_delta_uses_min():
    """target_soc == current_soc → no need to charge, fall to min."""
    ab, outputs = _make_ab()
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_SELF_CONSUMPTION,
        current_soc=65, target_soc=65,
    )
    assert cap == _BATT_CHARGE_CAP_MIN_W


def test_charge_cap_self_consumption_negative_delta_uses_min():
    """Plan wants SoC to drop → no charging, min cap."""
    ab, outputs = _make_ab()
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_SELF_CONSUMPTION,
        current_soc=80, target_soc=70,
    )
    assert cap == _BATT_CHARGE_CAP_MIN_W


def test_charge_cap_rounds_up_to_kw_step():
    """1% delta × 24.06 kWh = 240.6 W → rounds up to 1 kW step."""
    ab, outputs = _make_ab(capacity_kwh=24.06)
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_SELF_CONSUMPTION,
        current_soc=65, target_soc=66,
    )
    assert cap == 1000  # ceil(240.6 / 1000) * 1000


def test_charge_cap_matches_lp_midday_refill():
    """LP wants 12 % SoC rise this hour on 24.06 kWh = 2887 W → 3 kW cap."""
    ab, outputs = _make_ab(capacity_kwh=24.06)
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_SELF_CONSUMPTION,
        current_soc=56, target_soc=68,
    )
    assert cap == 3000


def test_charge_cap_clamps_to_max():
    """Huge requested delta clamps to set_charge_power.max."""
    ab, outputs = _make_ab(cap_max=5000, capacity_kwh=24.06)
    cap = ab._compute_charge_cap(
        outputs[OUTPUT_SUNGROW], ACTION_CHARGE_BATTERY,
        current_soc=0, target_soc=100,
    )
    assert cap == 5000


def test_charge_cap_step_granularity():
    """Step is 1 kW so cap values are always multiples of 1000."""
    ab, outputs = _make_ab()
    for delta_pct in (1, 2, 3, 5, 8, 12, 20):
        cap = ab._compute_charge_cap(
            outputs[OUTPUT_SUNGROW], ACTION_SELF_CONSUMPTION,
            current_soc=20, target_soc=20 + delta_pct,
        )
        assert cap % _BATT_CHARGE_CAP_STEP_W == 0, f"delta={delta_pct} → {cap}"


# ── End-to-end via build_immediate_actions ────────────────────────────

def test_immediate_actions_writes_charge_cap_every_cycle():
    """Every action path must emit a max-charge-cap write."""
    ab, _ = _make_ab()
    for action in (
        ACTION_SELF_CONSUMPTION,
        ACTION_DISCHARGE_BATTERY,
        ACTION_CHARGE_BATTERY,
        ACTION_PRE_DISCHARGE,
        ACTION_MAXIMIZE_LOAD,
    ):
        actions = ab.build_immediate_actions(
            action=action,
            ev_connected=False,
            current_soc=60,
            target_soc=70,
            predicted_consumption=0.5,
            predicted_solar=3.0,
        )
        assert _charge_cap_value(actions) is not None, f"missing cap for {action}"


def test_immediate_actions_self_consumption_caps_at_lp_trajectory():
    """SELF_CONSUMPTION with 10% delta on 24.06 kWh → 2407 W → 3 kW."""
    ab, _ = _make_ab(capacity_kwh=24.06)
    actions = ab.build_immediate_actions(
        action=ACTION_SELF_CONSUMPTION,
        ev_connected=False,
        current_soc=60,
        target_soc=70,
        predicted_consumption=0.5,
        predicted_solar=3.0,
    )
    assert _charge_cap_value(actions) == 3000


def test_immediate_actions_self_consumption_idle_caps_at_min():
    """When LP says don't charge (target=current), cap = min."""
    ab, _ = _make_ab()
    actions = ab.build_immediate_actions(
        action=ACTION_SELF_CONSUMPTION,
        ev_connected=False,
        current_soc=65,
        target_soc=65,
        predicted_consumption=0.0,
        predicted_solar=6.5,
    )
    assert _charge_cap_value(actions) == _BATT_CHARGE_CAP_MIN_W


def test_immediate_actions_respects_input_number_min_bound():
    """If input_number.min > _BATT_CHARGE_CAP_MIN_W, cap is raised to min."""
    ab, _ = _make_ab(cap_min=100)
    actions = ab.build_immediate_actions(
        action=ACTION_SELF_CONSUMPTION,
        ev_connected=False,
        current_soc=65, target_soc=65,
    )
    # _BATT_CHARGE_CAP_MIN_W is 100, input_number min is 100 → exactly 100
    assert _charge_cap_value(actions) == 100
