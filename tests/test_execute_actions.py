"""Tests for the coordinator's action executor."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.home_energy_management.coordinator import (
    EnergyManagementCoordinator,
)


def _run(actions):
    """Execute ``actions`` on a minimal stand-in coordinator; return calls."""
    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=AsyncMock()),
        states=SimpleNamespace(get=lambda _eid: None),
    )
    fake = SimpleNamespace(
        hass=hass,
        params={},
        _apply_charger_overrides=lambda a: (a, set()),
    )
    asyncio.run(EnergyManagementCoordinator._execute_actions(
        fake, {"immediate_actions": actions},
    ))
    return [c.args for c in hass.services.async_call.call_args_list]


def test_modbus_write_is_executed():
    """Modbus writes carry no entity/device id — they must still run."""
    calls = _run([{
        "service": "modbus.write_register",
        "data": {"hub": "SungrowSHx", "slave": 1, "address": 33046, "value": 700},
    }])
    assert calls == [(
        "modbus", "write_register",
        {"hub": "SungrowSHx", "slave": 1, "address": 33046, "value": 700},
    )]


def test_entity_action_still_executed():
    calls = _run([{
        "service": "input_number.set_value",
        "entity_id": "input_number.max_charge",
        "data": {"value": 7000},
    }])
    assert calls == [(
        "input_number", "set_value",
        {"value": 7000, "entity_id": "input_number.max_charge"},
    )]


def test_untargeted_non_modbus_action_skipped():
    assert _run([{"service": "script.turn_on", "data": {}}]) == []
