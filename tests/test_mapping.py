"""Tests for the variable mapping file — validates structure and completeness."""

import os
import pytest
import yaml


MAPPING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "variable_mapping.yaml"
)


@pytest.fixture
def mapping():
    """Load the variable mapping YAML file."""
    with open(MAPPING_PATH, "r", encoding="utf-8") as fh:
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

    def test_inputs_has_easee(self, mapping):
        assert "easee" in mapping["inputs"], "Inputs must include 'easee'"

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

    # --- Easee required fields ---

    def test_easee_has_status(self, mapping):
        easee = mapping["inputs"]["easee"]
        assert "status" in easee

    def test_easee_has_power(self, mapping):
        easee = mapping["inputs"]["easee"]
        assert "power" in easee

    # --- Output sections ---

    def test_outputs_has_easee(self, mapping):
        assert "easee" in mapping["outputs"]

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

    # --- Easee output actions ---

    def test_easee_output_has_start_charging(self, mapping):
        easee = mapping["outputs"]["easee"]
        assert "start_charging" in easee
        assert "service" in easee["start_charging"]

    def test_easee_output_has_stop_charging(self, mapping):
        easee = mapping["outputs"]["easee"]
        assert "stop_charging" in easee
        assert "service" in easee["stop_charging"]

    def test_easee_output_has_current_limit(self, mapping):
        easee = mapping["outputs"]["easee"]
        assert "set_current_limit" in easee
        limit = easee["set_current_limit"]
        assert "min_amps" in limit
        assert "max_amps" in limit
        assert limit["min_amps"] < limit["max_amps"]

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
