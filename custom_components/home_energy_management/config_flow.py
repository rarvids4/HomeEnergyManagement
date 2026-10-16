"""Config flow for Home Energy Management."""

from __future__ import annotations

import os

import voluptuous as vol
import yaml
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import (
    BUY_PRICE_MODES,
    CONF_MAPPING_PATH,
    DEFAULT_BUY_PRICE_FLOOR_SEK,
    DEFAULT_BUY_PRICE_MODE,
    DEFAULT_BUY_PRICE_MONTHLY_AVG_SEK,
    DEFAULT_GRID_TARIFF_OFFPEAK_SEK,
    DEFAULT_GRID_TARIFF_PEAK_END,
    DEFAULT_GRID_TARIFF_PEAK_SEK,
    DEFAULT_GRID_TARIFF_PEAK_START,
    DEFAULT_MAPPING_PATH,
    DOMAIN,
)

# Options key → (optimizer attribute, fallback default)
_OPTION_ATTRS = {
    "grid_tariff_peak_sek": ("grid_tariff_peak", DEFAULT_GRID_TARIFF_PEAK_SEK),
    "grid_tariff_offpeak_sek": ("grid_tariff_offpeak", DEFAULT_GRID_TARIFF_OFFPEAK_SEK),
    "grid_tariff_peak_start": ("grid_tariff_peak_start", DEFAULT_GRID_TARIFF_PEAK_START),
    "grid_tariff_peak_end": ("grid_tariff_peak_end", DEFAULT_GRID_TARIFF_PEAK_END),
    "buy_price_mode": ("buy_price_mode", DEFAULT_BUY_PRICE_MODE),
    "buy_price_floor_sek": ("buy_price_floor", DEFAULT_BUY_PRICE_FLOOR_SEK),
    "buy_price_monthly_avg_sek": ("buy_price_monthly_avg", DEFAULT_BUY_PRICE_MONTHLY_AVG_SEK),
}


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
        return HomeEnergyManagementOptionsFlow()


class HomeEnergyManagementOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Home Energy Management (tariffs, etc.).

    ``self.config_entry`` is provided by Home Assistant — it can no
    longer be assigned in ``__init__`` (HA 2025.12+).
    """

    def _current(self) -> dict:
        """Values the form starts from: saved options, else what's running.

        Falling back to the running optimizer (mapping / input helpers)
        means opening and saving the form never resets a tariff to 0.
        """
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        opt = getattr(data.get("coordinator"), "optimizer", None)
        current = {}
        for key, (attr, fallback) in _OPTION_ATTRS.items():
            if key in self.config_entry.options:
                current[key] = self.config_entry.options[key]
            else:
                current[key] = getattr(opt, attr, fallback)
        return current

    async def async_step_init(self, user_input=None):
        """Main options step — grid tariffs and buy-price contract."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._current()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "grid_tariff_peak_sek",
                        default=current["grid_tariff_peak_sek"],
                    ): vol.Coerce(float),
                    vol.Optional(
                        "grid_tariff_offpeak_sek",
                        default=current["grid_tariff_offpeak_sek"],
                    ): vol.Coerce(float),
                    vol.Optional(
                        "grid_tariff_peak_start",
                        default=current["grid_tariff_peak_start"],
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                    vol.Optional(
                        "grid_tariff_peak_end",
                        default=current["grid_tariff_peak_end"],
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                    vol.Optional(
                        "buy_price_mode",
                        default=current["buy_price_mode"],
                    ): vol.In(BUY_PRICE_MODES),
                    vol.Optional(
                        "buy_price_floor_sek",
                        default=current["buy_price_floor_sek"],
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        "buy_price_monthly_avg_sek",
                        default=current["buy_price_monthly_avg_sek"],
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
        )
