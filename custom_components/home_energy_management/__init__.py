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
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Energy Management from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register services (including write_local_config)
    await async_register_services(hass)

    # Load variable mapping — prefer local override, fall back to bundled template
    # Local override lives at <HA config root>/variable_mapping.local.yaml
    local_path = os.path.join(hass.config.config_dir, LOCAL_MAPPING_PATH)
    # Default mapping bundled inside the component package
    component_dir = os.path.dirname(os.path.abspath(__file__))
    mapping_path = entry.data.get(CONF_MAPPING_PATH, DEFAULT_MAPPING_PATH)
    if not os.path.isabs(mapping_path):
        mapping_path = os.path.join(component_dir, mapping_path)

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

    # Apply any saved options (tariffs) to the optimizer
    _apply_options(coordinator, entry.options)

    # Listen for options changes (tariff edits from the UI)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Forward setup to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _apply_options(coordinator: EnergyManagementCoordinator, options: dict) -> None:
    """Apply config entry options (tariffs) to the optimizer."""
    if not options:
        return
    opt = coordinator.optimizer
    if "grid_tariff_peak_sek" in options:
        opt.grid_tariff_peak = options["grid_tariff_peak_sek"]
    if "grid_tariff_offpeak_sek" in options:
        opt.grid_tariff_offpeak = options["grid_tariff_offpeak_sek"]
    if "grid_tariff_peak_start" in options:
        opt.grid_tariff_peak_start = options["grid_tariff_peak_start"]
    if "grid_tariff_peak_end" in options:
        opt.grid_tariff_peak_end = options["grid_tariff_peak_end"]
    _LOGGER.info(
        "Applied tariff options: peak=%.3f offpeak=%.3f hours=%d-%d",
        opt.grid_tariff_peak, opt.grid_tariff_offpeak,
        opt.grid_tariff_peak_start, opt.grid_tariff_peak_end,
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — apply new tariffs and trigger replan."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    if coordinator:
        _apply_options(coordinator, entry.options)
        await coordinator.async_request_refresh()


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
