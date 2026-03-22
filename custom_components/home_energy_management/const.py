"""Constants for Home Energy Management."""

DOMAIN = "home_energy_management"
PLATFORMS = ["sensor"]

# Config keys
CONF_MAPPING_PATH = "mapping_path"

# Default mapping bundled inside the component (deployed via HACS)
DEFAULT_MAPPING_PATH = "default_mapping.yaml"
# Local override in HA config root (writable via write_local_config service)
LOCAL_MAPPING_PATH = "variable_mapping.local.yaml"

# Coordinator update interval (seconds) — overridden by mapping parameters
DEFAULT_UPDATE_INTERVAL = 900  # 15 minutes

# --- Mapping section keys ---
MAPPING_INPUTS = "inputs"
MAPPING_OUTPUTS = "outputs"
MAPPING_PARAMETERS = "parameters"

# --- Input sub-sections ---
INPUT_NORDPOOL = "nordpool"
INPUT_EV_CHARGERS = "ev_chargers"
INPUT_SUNGROW = "sungrow"
INPUT_SUNGROW_2 = "sungrow_2"  # slave inverter (read-only sensors, no control)
INPUT_SMART_METER = "smart_meter"
INPUT_WEATHER = "weather"

# Backward compat alias for old config files that use "easee"
INPUT_EASEE = "easee"

# --- Output sub-sections ---
OUTPUT_EV_CHARGERS = "ev_chargers"
OUTPUT_SUNGROW = "sungrow"
# Note: sungrow_2 is a slave inverter — all control goes through the master.
# No OUTPUT_SUNGROW_2 needed.

# Backward compat alias
OUTPUT_EASEE = "easee"

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

# --- Grid charge limits ---
# Only charge from grid up to this SoC (%) — let solar fill the rest
DEFAULT_GRID_CHARGE_MAX_SOC = 15  # %
# Never charge from grid if price exceeds this (SEK/kWh)
DEFAULT_GRID_CHARGE_MAX_PRICE = 0.40  # SEK/kWh

# --- Charger limits ---
DEFAULT_MIN_AMPS = 6
DEFAULT_MAX_AMPS = 32

# --- EV smart charging thresholds ---
# Always charge EVs when price is below this (SEK/kWh), regardless of battery action
DEFAULT_EV_CHEAP_PRICE_THRESHOLD = 0.10
# Charge EVs when grid export exceeds this (W) — absorb solar surplus
DEFAULT_SOLAR_SURPLUS_THRESHOLD = 2000

# Preferred EV charging window (night, off-peak) to minimize grid load
DEFAULT_EV_NIGHT_START = 22   # 22:00
DEFAULT_EV_NIGHT_END = 6      # 06:00
# Night hours get this bonus in sorting (SEK/kWh) — prefer night over day
DEFAULT_EV_NIGHT_PREFERENCE_SEK = 0.10
# On Friday evenings, target lower SoC (car parked at home Sat, solar fills later)
DEFAULT_EV_WEEKEND_TARGET_SOC = 80  # %
# Default EV target SoC for night charging (charge if below this)
DEFAULT_EV_TARGET_SOC = 100  # %

# --- Nordpool ---
DEFAULT_ENTRIES_PER_HOUR = 1  # 1 = hourly prices, 4 = 15-min

# --- Optimizer actions ---
ACTION_CHARGE_BATTERY = "charge_battery"
ACTION_DISCHARGE_BATTERY = "discharge_battery"
ACTION_SELF_CONSUMPTION = "self_consumption"
ACTION_MAXIMIZE_LOAD = "maximize_load"
ACTION_PRE_DISCHARGE = "pre_discharge"
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
