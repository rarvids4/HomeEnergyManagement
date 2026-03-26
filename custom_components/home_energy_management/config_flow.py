"""Config flow for Home Energy Management."""

from __future__ import annotations

import os

import voluptuous as vol
import yaml
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import CONF_MAPPING_PATH, DEFAULT_MAPPING_PATH, DOMAIN


async def _validate_mapping(hass: HomeAssistant, path: str) -> dict | None:
    """Validate that the mapping file exists and is valid YAML."""
    if os.path.isabs(path):
        abs_path = path
    else:
        # First try inside the component directory (bundled default)
        component_dir = os.path.dirname(os.path.abspath(__file__))
        abs_path = os.path.join(component_dir, path)
        if not os.path.exists(abs_path):
            # Fall back to HA config dir
            abs_path = os.path.join(hass.config.config_dir, path)

    def _read():
        with open(abs_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    try:
        return await hass.async_add_executor_job(_read)
    except (FileNotFoundError, yaml.YAMLError):
        return None


class HomeEnergyManagementConfigFlow(
    config_entries.ConfigFlow, domain=DOMAIN
):
    """Handle a config flow for Home Energy Management."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            mapping_path = user_input.get(CONF_MAPPING_PATH, DEFAULT_MAPPING_PATH)
            mapping = await _validate_mapping(self.hass, mapping_path)

            if mapping is None:
                errors["base"] = "mapping_not_found"
            else:
                return self.async_create_entry(
                    title="Home Energy Management",
                    data={CONF_MAPPING_PATH: mapping_path},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MAPPING_PATH,
                        default=DEFAULT_MAPPING_PATH,
                    ): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return HomeEnergyManagementOptionsFlow(config_entry)


class HomeEnergyManagementOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Home Energy Management (tariffs, etc.)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Main options step — grid tariffs."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "grid_tariff_peak_sek",
                        default=current.get("grid_tariff_peak_sek", 0.0),
                    ): vol.Coerce(float),
                    vol.Optional(
                        "grid_tariff_offpeak_sek",
                        default=current.get("grid_tariff_offpeak_sek", 0.0),
                    ): vol.Coerce(float),
                    vol.Optional(
                        "grid_tariff_peak_start",
                        default=current.get("grid_tariff_peak_start", 6),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                    vol.Optional(
                        "grid_tariff_peak_end",
                        default=current.get("grid_tariff_peak_end", 22),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                }
            ),
        )
