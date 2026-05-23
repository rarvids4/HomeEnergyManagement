"""Tests for WriteArbiter — min-arbitration across multiple writers."""

import time

import pytest

from custom_components.home_energy_management.write_arbiter import WriteArbiter


def test_empty_arbiter_returns_none():
    arb = WriteArbiter("test")
    assert arb.arbitrate() is None


def test_single_source_returns_its_value():
    arb = WriteArbiter("test")
    arb.propose("lp", 2000)
    assert arb.arbitrate() == 2000


def test_min_across_two_sources():
    arb = WriteArbiter("test")
    arb.propose("lp", 2000)
    arb.propose("surplus", 500)
    assert arb.arbitrate() == 500


def test_repeat_propose_overwrites_same_source():
    arb = WriteArbiter("test")
    arb.propose("lp", 2000)
    arb.propose("lp", 100)
    assert arb.arbitrate() == 100


def test_stale_proposal_ignored(monkeypatch):
    arb = WriteArbiter("test")
    arb.propose("lp", 100, max_age_s=10)        # will go stale
    arb.propose("surplus", 500, max_age_s=120)  # stays fresh
    # Advance monotonic clock past max_age for "lp" only
    base = time.monotonic()
    monkeypatch.setattr(
        "custom_components.home_energy_management.write_arbiter.time.monotonic",
        lambda: base + 20,
    )
    assert arb.arbitrate() == 500


def test_all_stale_returns_none(monkeypatch):
    arb = WriteArbiter("test")
    arb.propose("lp", 100, max_age_s=5)
    base = time.monotonic()
    monkeypatch.setattr(
        "custom_components.home_energy_management.write_arbiter.time.monotonic",
        lambda: base + 10,
    )
    assert arb.arbitrate() is None


def test_withdraw_removes_source():
    arb = WriteArbiter("test")
    arb.propose("lp", 100)
    arb.propose("surplus", 500)
    arb.withdraw("lp")
    assert arb.arbitrate() == 500
    arb.withdraw("surplus")
    assert arb.arbitrate() is None


def test_withdraw_missing_source_noop():
    arb = WriteArbiter("test")
    arb.withdraw("nobody")  # should not raise


def test_diagnostics_shape():
    arb = WriteArbiter("battery_max_charge_power_w")
    arb.propose("lp", 2000, max_age_s=120)
    arb.propose("surplus", 500, max_age_s=30)
    diag = arb.diagnostics()
    assert diag["name"] == "battery_max_charge_power_w"
    assert diag["arbitrated"] == 500
    assert set(diag["sources"].keys()) == {"lp", "surplus"}
    for src in diag["sources"].values():
        assert "value" in src
        assert "age_s" in src
        assert "stale" in src
        assert "max_age_s" in src
        assert src["stale"] is False


def test_three_sources_picks_lowest():
    arb = WriteArbiter("test")
    arb.propose("a", 3000)
    arb.propose("b", 1500)
    arb.propose("c", 2000)
    assert arb.arbitrate() == 1500


# ── Integration with ActionBuilder ────────────────────────────────────

def _build_ab():
    from custom_components.home_energy_management.action_builder import ActionBuilder
    from custom_components.home_energy_management.const import OUTPUT_SUNGROW
    params = {"enable_battery_control": True, "enable_charger_control": False}
    outputs = {
        OUTPUT_SUNGROW: {
            "capacity_kwh": 24.06,
            "max_soc": 100, "min_soc": 5,
            "self_consumption": {"service": "script.turn_on", "entity_id": "script.sc"},
            "force_charge": {"service": "script.turn_on", "entity_id": "script.fc"},
            "force_discharge": {"service": "script.turn_on", "entity_id": "script.fd"},
            "set_forced_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.forced_power",
                "min": 0, "max": 5000,
            },
            "set_charge_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.max_charge",
                "min": 0, "max": 5000,
                "modbus": {
                    "hub": "SungrowSHx", "slave": 1,
                    "address": 33046, "scale": 10,
                },
            },
            "set_discharge_power": {
                "service": "input_number.set_value",
                "entity_id": "input_number.max_discharge",
                "min": 0, "max": 5000,
            },
        }
    }
    return ActionBuilder(params, outputs)


def _extract_modbus_writes(actions, address):
    return [
        a for a in actions
        if a.get("service") == "modbus.write_register"
        and a.get("data", {}).get("address") == address
    ]


def test_action_builder_emits_direct_modbus_write():
    from custom_components.home_energy_management.const import ACTION_SELF_CONSUMPTION
    ab = _build_ab()
    actions = ab.build_immediate_actions(
        action=ACTION_SELF_CONSUMPTION,
        ev_connected=False,
        current_soc=60, target_soc=70,  # 10% × 24.06 kWh ≈ 2.4 kW → 3 kW cap
    )
    writes = _extract_modbus_writes(actions, 33046)
    assert len(writes) == 1
    # 3000 W / scale 10 = 300
    assert writes[0]["data"]["value"] == 300
    assert writes[0]["data"]["hub"] == "SungrowSHx"
    assert writes[0]["data"]["slave"] == 1


def test_action_builder_external_proposal_lowers_cap():
    """External source proposing a lower value wins via min-arbiter."""
    from custom_components.home_energy_management.const import ACTION_SELF_CONSUMPTION
    ab = _build_ab()
    # External component (e.g. future surplus) proposes 500 W BEFORE
    # build_immediate_actions runs.
    ab.charge_cap_arbiter.propose("surplus", 500, max_age_s=60)
    actions = ab.build_immediate_actions(
        action=ACTION_SELF_CONSUMPTION,
        ev_connected=False,
        current_soc=60, target_soc=70,  # LP would want 3000 W
    )
    writes = _extract_modbus_writes(actions, 33046)
    assert len(writes) == 1
    # min(3000, 500) = 500 → reg value 50
    assert writes[0]["data"]["value"] == 50


def test_action_builder_stale_external_proposal_ignored(monkeypatch):
    from custom_components.home_energy_management.const import ACTION_SELF_CONSUMPTION
    ab = _build_ab()
    ab.charge_cap_arbiter.propose("surplus", 500, max_age_s=10)
    base = time.monotonic()
    monkeypatch.setattr(
        "custom_components.home_energy_management.write_arbiter.time.monotonic",
        lambda: base + 20,
    )
    actions = ab.build_immediate_actions(
        action=ACTION_SELF_CONSUMPTION,
        ev_connected=False,
        current_soc=60, target_soc=70,
    )
    writes = _extract_modbus_writes(actions, 33046)
    # stale proposal ignored → LP's 3000 W wins
    assert writes[0]["data"]["value"] == 300
