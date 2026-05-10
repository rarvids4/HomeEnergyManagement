"""Tests for the variable mapping file — structure and completeness."""

from __future__ import annotations

import os
import yaml
import pytest


MAPPING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "variable_mapping.yaml"
)
LOCAL_MAPPING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "variable_mapping.local.yaml"
)


@pytest.fixture(scope="module")
def mapping() -> dict:
    assert os.path.exists(MAPPING_PATH), "variable_mapping.yaml must exist"
    with open(MAPPING_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def local_mapping() -> dict:
    if not os.path.exists(LOCAL_MAPPING_PATH):
        pytest.skip("local mapping file not present")
    with open(LOCAL_MAPPING_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_top_level_structure(mapping):
    """All top-level sections, input subsections, and parameter scalars present
    with sane values."""
    # Top-level
    for key in ("inputs", "outputs", "parameters"):
        assert key in mapping, f"Missing top-level section: {key}"

    # Input subsections
    for key in ("nordpool", "ev_chargers", "sungrow", "smart_meter"):
        assert key in mapping["inputs"], f"Missing input section: {key}"

    # Nordpool fields + valid entries_per_hour
    np = mapping["inputs"]["nordpool"]
    for key in ("current_price", "today_prices_attribute",
                "tomorrow_prices_attribute", "entries_per_hour"):
        assert key in np, f"Missing nordpool field: {key}"
    assert np["entries_per_hour"] in (1, 2, 4)

    # Sungrow input fields
    sg_in = mapping["inputs"]["sungrow"]
    for key in ("battery_soc", "battery_power", "pv_power", "house_load"):
        assert key in sg_in, f"Missing sungrow input field: {key}"

    # Parameter scalars
    params = mapping["parameters"]
    for key in ("optimization_interval_minutes", "planning_horizon_hours",
                "min_price_spread", "prediction_history_days",
                "prediction_recency_weight"):
        assert key in params, f"Missing parameter: {key}"
    assert params["optimization_interval_minutes"] > 0
    assert params["planning_horizon_hours"] > 0
    assert params["min_price_spread"] >= 0
    assert 0 <= params["prediction_recency_weight"] <= 1


def test_outputs_sungrow(mapping):
    """Sungrow output has all required modes + valid SoC/capacity bounds."""
    sg = mapping["outputs"]["sungrow"]
    for key in ("force_charge", "force_discharge", "self_consumption",
                "min_soc", "max_soc", "capacity_kwh"):
        assert key in sg, f"Missing sungrow output: {key}"
    assert 0 <= sg["min_soc"] < sg["max_soc"] <= 100
    assert sg["capacity_kwh"] > 0


def test_ev_chargers(mapping):
    """EV chargers list — both input and output entries are well-formed."""
    ev_in = mapping["inputs"]["ev_chargers"]
    assert isinstance(ev_in, list) and len(ev_in) >= 1
    for c in ev_in:
        for key in ("name", "power", "charger_switch"):
            assert key in c, f"Charger {c.get('name')} missing input field: {key}"

    ev_out = mapping["outputs"]["ev_chargers"]
    assert isinstance(ev_out, list) and len(ev_out) >= 1
    for c in ev_out:
        for key in ("start_charging", "stop_charging"):
            assert key in c, f"Charger {c.get('name')} missing output field: {key}"
            assert "service" in c[key]


def test_local_mapping_no_placeholders(local_mapping):
    """Local mapping must have same structure and zero CHANGE_ME placeholders."""
    for key in ("inputs", "outputs", "parameters"):
        assert key in local_mapping, f"Local mapping missing section: {key}"
    yaml_str = yaml.dump(local_mapping)
    assert "CHANGE_ME" not in yaml_str, (
        "Local mapping still contains CHANGE_ME placeholders"
    )
    ev = local_mapping["inputs"]["ev_chargers"]
    assert isinstance(ev, list) and len(ev) >= 1
