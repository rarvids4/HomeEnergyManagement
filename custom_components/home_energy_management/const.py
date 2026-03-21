"""Constants for Home Energy Management."""

DOMAIN = "home_energy_management"
PLATFORMS = ["sensor"]

# Config keys
CONF_MAPPING_PATH = "mapping_path"

# Default path relative to HA config dir
DEFAULT_MAPPING_PATH = "custom_components/home_energy_management/../../config/variable_mapping.yaml"

# Coordinator update interval (seconds) — overridden by mapping parameters
DEFAULT_UPDATE_INTERVAL = 900  # 15 minutes

# --- Mapping section keys ---
MAPPING_INPUTS = "inputs"
MAPPING_OUTPUTS = "outputs"
MAPPING_PARAMETERS = "parameters"

# --- Input sub-sections ---
INPUT_NORDPOOL = "nordpool"
INPUT_EASEE = "easee"
INPUT_SUNGROW = "sungrow"
INPUT_SMART_METER = "smart_meter"
INPUT_WEATHER = "weather"

# --- Output sub-sections ---
OUTPUT_EASEE = "easee"
OUTPUT_SUNGROW = "sungrow"

# --- Parameter defaults ---
DEFAULT_OPTIMIZATION_INTERVAL = 15  # minutes
DEFAULT_PLANNING_HORIZON = 24  # hours
DEFAULT_MIN_PRICE_SPREAD = 0.30  # SEK/kWh
DEFAULT_PREDICTION_HISTORY_DAYS = 14
DEFAULT_PREDICTION_RECENCY_WEIGHT = 0.7

# --- Battery limits ---
DEFAULT_MIN_SOC = 10  # %
DEFAULT_MAX_SOC = 100  # %
DEFAULT_BATTERY_CAPACITY = 10.0  # kWh

# --- Charger limits ---
DEFAULT_MIN_AMPS = 6
DEFAULT_MAX_AMPS = 32

# --- Optimizer actions ---
ACTION_CHARGE_BATTERY = "charge_battery"
ACTION_DISCHARGE_BATTERY = "discharge_battery"
ACTION_SELF_CONSUMPTION = "self_consumption"
ACTION_START_EV_CHARGE = "start_ev_charge"
ACTION_STOP_EV_CHARGE = "stop_ev_charge"
ACTION_SET_EV_AMPS = "set_ev_amps"

# --- Sensor entity IDs (created by integration) ---
SENSOR_NEXT_ACTION = "next_planned_action"
SENSOR_CURRENT_PRICE = "current_energy_price"
SENSOR_PREDICTED_CONSUMPTION = "predicted_consumption"
SENSOR_BATTERY_PLAN = "battery_plan"
SENSOR_CHARGER_PLAN = "charger_plan"
SENSOR_DAILY_SAVINGS = "daily_savings"
SENSOR_OPTIMIZATION_STATUS = "optimization_status"

# --- Log ---
LOG_MAX_ENTRIES = 500
