"""Service handlers for Home Energy Management."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_register_services(hass: HomeAssistant) -> None:
    """Register custom services."""

    async def handle_force_replan(call: ServiceCall) -> None:
        """Force the optimizer to re-run immediately."""
        _LOGGER.info("Force replan requested")
        for entry_data in hass.data.get(DOMAIN, {}).values():
            coordinator = entry_data.get("coordinator")
            if coordinator:
                await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "force_replan", handle_force_replan)
