"""Home Energy Management — Custom Integration for Home Assistant.

Optimises charging/discharging of EV chargers and Sungrow battery
based on Nordpool energy prices and predicted consumption patterns.
"""

from __future__ import annotations

import logging
import os

import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MAPPING_PATH,
    DEFAULT_MAPPING_PATH,
    DOMAIN,
    LOCAL_MAPPING_PATH,
    PLATFORMS,
)
from .coordinator import EnergyManagementCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Energy Management from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Load variable mapping — prefer local override, fall back to template
    mapping_path = entry.data.get(CONF_MAPPING_PATH, DEFAULT_MAPPING_PATH)
    local_path = LOCAL_MAPPING_PATH

    # Resolve relative paths
    if not os.path.isabs(mapping_path):
        mapping_path = os.path.join(hass.config.config_dir, mapping_path)
    if not os.path.isabs(local_path):
        local_path = os.path.join(hass.config.config_dir, local_path)

    # Try local first, then template
    mapping = None
    if os.path.exists(local_path):
        mapping = await hass.async_add_executor_job(_load_mapping, local_path)
        if mapping:
            _LOGGER.info("Loaded LOCAL variable mapping from %s", local_path)

    if mapping is None:
        mapping = await hass.async_add_executor_job(_load_mapping, mapping_path)
        if mapping:
            _LOGGER.info("Loaded variable mapping from %s", mapping_path)

    if mapping is None:
        _LOGGER.error(
            "Failed to load variable mapping from %s or %s",
            local_path,
            mapping_path,
        )
        return False

    _LOGGER.info(
        "Variable mapping: %d inputs, %d outputs",
        len(mapping.get("inputs", {})),
        len(mapping.get("outputs", {})),
    )

    # Create the coordinator
    coordinator = EnergyManagementCoordinator(hass, entry, mapping)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "mapping": mapping,
    }

    # Forward setup to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _load_mapping(path: str) -> dict | None:
    """Load the variable mapping YAML file (runs in executor)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        _LOGGER.error("Variable mapping file not found: %s", path)
        return None
    except yaml.YAMLError as exc:
        _LOGGER.error("Error parsing variable mapping YAML: %s", exc)
        return None
