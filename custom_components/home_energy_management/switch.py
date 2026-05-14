"""Switch platform for Home Energy Management.

Exposes one HEM-owned override switch per configured EV charger.
When ON, the coordinator skips ALL service calls (start/stop) that
target that charger — control is handed to the user. Toggle OFF to
release control back to HEM.

State is restored across HA restarts via RestoreEntity.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INPUT_EV_CHARGERS
from .coordinator import EnergyManagementCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one override switch per EV charger from the mapping."""
    coordinator: EnergyManagementCoordinator = (
        hass.data[DOMAIN][entry.entry_id]["coordinator"]
    )

    chargers = coordinator.inputs.get(INPUT_EV_CHARGERS, []) or []
    entities: list[SwitchEntity] = []
    for ch in chargers:
        name = ch.get("name")
        if not name:
            continue
        friendly = ch.get("friendly_name") or name
        entities.append(ChargerOverrideSwitch(coordinator, entry, name, friendly))

    if entities:
        async_add_entities(entities)


class ChargerOverrideSwitch(CoordinatorEntity, SwitchEntity, RestoreEntity):
    """Per-charger HEM override.

    ON  → HEM stops issuing start/stop for this charger.
    OFF → HEM controls the charger as planned.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:hand-back-right"

    def __init__(
        self,
        coordinator: EnergyManagementCoordinator,
        entry: ConfigEntry,
        charger_name: str,
        friendly_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_name = charger_name
        self._attr_unique_id = f"{entry.entry_id}_override_{charger_name}"
        # With has_entity_name=True the device name is prepended automatically,
        # so just use "Override" as the entity name. Final entity friendly name
        # becomes e.g. "Volvo EX90 Override".
        self._attr_name = "Override"
        # Per-charger sub-device, linked to the main HEM hub device via_device.
        # This makes the HA UI render a separate card per car under the
        # Home Energy Management integration page.
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id, "charger", charger_name)},
            "name": friendly_name,
            "manufacturer": "Home Energy Management",
            "model": "EV Charger",
            "via_device": (DOMAIN, entry.entry_id),
        }
        # Default OFF until restored
        self.coordinator.charger_overrides.setdefault(charger_name, False)

    async def async_added_to_hass(self) -> None:
        """Restore previous state."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator.charger_overrides[self._charger_name] = True
        else:
            self.coordinator.charger_overrides.setdefault(self._charger_name, False)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.charger_overrides.get(self._charger_name, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.charger_overrides[self._charger_name] = True
        _LOGGER.info("HEM override ENABLED for %s", self._charger_name)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.charger_overrides[self._charger_name] = False
        _LOGGER.info("HEM override DISABLED for %s", self._charger_name)
        self.async_write_ha_state()
