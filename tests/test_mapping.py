"""Tests for the variable mapping file — validates structure and completeness."""

import os
import pytest
import yaml


MAPPING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "variable_mapping.yaml"
)

LOCAL_MAPPING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "variable_mapping.local.yaml"
)


@pytest.fixture
def mapping():
    """Load the variable mapping YAML file."""
    with open(MAPPING_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def local_mapping():
    """Load the local variable mapping YAML file if it exists."""
    if not os.path.exists(LOCAL_MAPPING_PATH):
        pytest.skip("local mapping file not present")
    with open(LOCAL_MAPPING_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestMappingStructure:
    """Validate the mapping file has the expected structure."""

    def test_file_exists(self):
        assert os.path.exists(MAPPING_PATH), "variable_mapping.yaml must exist"

    def test_has_inputs_section(self, mapping):
        assert "inputs" in mapping, "Mapping must have an 'inputs' section"

    def test_has_outputs_section(self, mapping):
        assert "outputs" in mapping, "Mapping must have an 'outputs' section"

    def test_has_parameters_section(self, mapping):
        assert "parameters" in mapping, "Mapping must have a 'parameters' section"

    # --- Input sections ---

    def test_inputs_has_nordpool(self, mapping):
        assert "nordpool" in mapping["inputs"], "Inputs must include 'nordpool'"

    def test_inputs_has_ev_chargers(self, mapping):
        assert "ev_chargers" in mapping["inputs"], "Inputs must include 'ev_chargers'"

    def test_inputs_has_sungrow(self, mapping):
        assert "sungrow" in mapping["inputs"], "Inputs must include 'sungrow'"

    def test_inputs_has_smart_meter(self, mapping):
        assert "smart_meter" in mapping["inputs"], "Inputs must include 'smart_meter'"

    # --- Nordpool required fields ---

    def test_nordpool_has_current_price(self, mapping):
        np = mapping["inputs"]["nordpool"]
        assert "current_price" in np, "Nordpool must have 'current_price'"

    def test_nordpool_has_today_attribute(self, mapping):
        np = mapping["inputs"]["nordpool"]
        assert "today_prices_attribute" in np

    def test_nordpool_has_tomorrow_attribute(self, mapping):
        np = mapping["inputs"]["nordpool"]
        assert "tomorrow_prices_attribute" in np

    def test_nordpool_has_entries_per_hour(self, mapping):
        np = mapping["inputs"]["nordpool"]
        assert "entries_per_hour" in np
        assert np["entries_per_hour"] in (1, 2, 4)

    # --- Sungrow required fields ---

    def test_sungrow_has_battery_soc(self, mapping):
        sg = mapping["inputs"]["sungrow"]
        assert "battery_soc" in sg

    def test_sungrow_has_battery_power(self, mapping):
        sg = mapping["inputs"]["sungrow"]
        assert "battery_power" in sg

    def test_sungrow_has_pv_power(self, mapping):
        sg = mapping["inputs"]["sungrow"]
        assert "pv_power" in sg

    def test_sungrow_has_house_load(self, mapping):
        sg = mapping["inputs"]["sungrow"]
        assert "house_load" in sg

    # --- EV Chargers (list-based) ---

    def test_ev_chargers_is_list(self, mapping):
        ev = mapping["inputs"]["ev_chargers"]
        assert isinstance(ev, list), "ev_chargers must be a list"
        assert len(ev) >= 1, "Must have at least one EV charger"

    def test_ev_charger_has_required_fields(self, mapping):
        for charger in mapping["inputs"]["ev_chargers"]:
            assert "name" in charger, "Each EV charger must have a 'name'"
            assert "power" in charger, "Each EV charger must have a 'power' sensor"
            assert "charger_switch" in charger, "Each EV charger must have a 'charger_switch'"

    # --- Output sections ---

    def test_outputs_has_ev_chargers(self, mapping):
        assert "ev_chargers" in mapping["outputs"]

    def test_outputs_has_sungrow(self, mapping):
        assert "sungrow" in mapping["outputs"]

    # --- Sungrow output modes ---

    def test_sungrow_output_has_force_charge(self, mapping):
        sg = mapping["outputs"]["sungrow"]
        assert "force_charge" in sg

    def test_sungrow_output_has_force_discharge(self, mapping):
        sg = mapping["outputs"]["sungrow"]
        assert "force_discharge" in sg

    def test_sungrow_output_has_self_consumption(self, mapping):
        sg = mapping["outputs"]["sungrow"]
        assert "self_consumption" in sg

    def test_sungrow_output_has_soc_limits(self, mapping):
        sg = mapping["outputs"]["sungrow"]
        assert "min_soc" in sg
        assert "max_soc" in sg
        assert sg["min_soc"] >= 0
        assert sg["max_soc"] <= 100
        assert sg["min_soc"] < sg["max_soc"]

    def test_sungrow_output_has_capacity(self, mapping):
        sg = mapping["outputs"]["sungrow"]
        assert "capacity_kwh" in sg
        assert sg["capacity_kwh"] > 0

    # --- EV Charger output actions ---

    def test_ev_charger_output_has_start_stop(self, mapping):
        ev_list = mapping["outputs"]["ev_chargers"]
        assert isinstance(ev_list, list), "ev_chargers outputs must be a list"
        for charger in ev_list:
            assert "start_charging" in charger, f"Charger {charger.get('name')} must have start_charging"
            assert "stop_charging" in charger, f"Charger {charger.get('name')} must have stop_charging"
            assert "service" in charger["start_charging"]
            assert "service" in charger["stop_charging"]

    # --- Parameters ---

    def test_parameters_has_optimization_interval(self, mapping):
        params = mapping["parameters"]
        assert "optimization_interval_minutes" in params
        assert params["optimization_interval_minutes"] > 0

    def test_parameters_has_planning_horizon(self, mapping):
        params = mapping["parameters"]
        assert "planning_horizon_hours" in params
        assert params["planning_horizon_hours"] > 0

    def test_parameters_has_min_price_spread(self, mapping):
        params = mapping["parameters"]
        assert "min_price_spread" in params
        assert params["min_price_spread"] >= 0

    def test_parameters_has_prediction_settings(self, mapping):
        params = mapping["parameters"]
        assert "prediction_history_days" in params
        assert "prediction_recency_weight" in params
        assert 0 <= params["prediction_recency_weight"] <= 1


class TestLocalMapping:
    """Validate the local mapping has no CHANGE_ME placeholders."""

    def test_no_change_me_in_local(self, local_mapping):
        """Ensure no CHANGE_ME placeholders remain in the local file."""
        yaml_str = yaml.dump(local_mapping)
        assert "CHANGE_ME" not in yaml_str, (
            "Local mapping still contains CHANGE_ME placeholders"
        )

    def test_local_has_same_structure(self, local_mapping):
        """Local mapping should have the same top-level sections."""
        assert "inputs" in local_mapping
        assert "outputs" in local_mapping
        assert "parameters" in local_mapping

    def test_local_ev_chargers_configured(self, local_mapping):
        ev = local_mapping["inputs"]["ev_chargers"]
        assert isinstance(ev, list)
        assert len(ev) >= 1
        for charger in ev:
            assert "CHANGE_ME" not in str(charger)
