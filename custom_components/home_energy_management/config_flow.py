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
    abs_path = path if os.path.isabs(path) else os.path.join(hass.config.config_dir, path)

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
