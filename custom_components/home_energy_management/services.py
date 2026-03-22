"""Service handlers for Home Energy Management."""

from __future__ import annotations

import logging
import os

import yaml
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, LOCAL_MAPPING_PATH

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

    async def handle_write_local_config(call: ServiceCall) -> None:
        """Write local variable mapping to HA config root.

        Accepts YAML content as a string and writes it to
        <config_dir>/variable_mapping.local.yaml.
        Call this service once to deploy your real entity IDs,
        then restart HA or call force_replan.
        """
        content = call.data.get("content", "")
        if not content:
            _LOGGER.error("write_local_config called with empty content")
            return

        # Validate YAML before writing
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            _LOGGER.error("Invalid YAML in write_local_config: %s", exc)
            return

        target_path = os.path.join(hass.config.config_dir, LOCAL_MAPPING_PATH)

        def _write():
            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            _LOGGER.info(
                "Local variable mapping written to %s (%d bytes)",
                target_path,
                len(content),
            )

        await hass.async_add_executor_job(_write)

    hass.services.async_register(DOMAIN, "force_replan", handle_force_replan)
    hass.services.async_register(
        DOMAIN, "write_local_config", handle_write_local_config
    )
