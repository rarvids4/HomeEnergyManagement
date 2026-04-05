<p align="center">
  <img src="https://img.shields.io/badge/Home%20Assistant-Custom%20Component-blue?logo=homeassistant&logoColor=white" alt="Home Assistant" />
  <img src="https://img.shields.io/badge/HACS-Custom-orange?logo=homeassistantcommunitystore&logoColor=white" alt="HACS" />
  <img src="https://img.shields.io/badge/Python-3.11+-green?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" />
</p>

# ⚡ Home Energy Management

> A smart Home Assistant integration that **optimises charging and discharging** of your EV chargers and home battery based on **Nordpool energy prices** and **predicted consumption patterns** — saving you money automatically. The battery scheduler uses a **linear-programming (LP) solver** for mathematically optimal charge/discharge decisions.

---

## 🎯 What It Does

```
┌─────────────────────────────────────────────────────────────┐
│                    Nordpool Prices                           │
│   💰 Low price    → Charge battery + Charge EVs            │
│   💸 High price   → Discharge battery + Stop EV charging   │
│   ☀️  Solar surplus → Charge EVs with free solar            │
│   ⚡ Negative price → Charge everything at max              │
│   📊 Normal        → Self-consumption mode                  │
└─────────────────────────────────────────────────────────────┘
```

The integration runs every 15 minutes (configurable) and:

1. **Reads** current and upcoming Nordpool hourly/15-min electricity prices
2. **Predicts** your household energy consumption using historical patterns (separate house & EV streams)
3. **Plans** a mathematically optimal 24–48 hour charge/discharge schedule via LP solver with grid tariff awareness
4. **Controls** multiple EV chargers and Sungrow battery automatically
5. **Manages** per-vehicle departure targets, SoC floors, and two-day price optimization
6. **Absorbs** solar surplus into EVs with dynamic current limiting
7. **Logs** every decision so you can review predictions vs reality

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🧮 **LP-optimized battery** | Linear-programming solver (scipy) finds the mathematically optimal charge/discharge schedule across the full planning horizon |
| 🔌 **Price-aware scheduling** | Detects price peaks/valleys from Nordpool, plans charge/discharge windows |
| 💸 **Grid tariff support** | Adds time-of-use network fees (peak/off-peak) to spot prices for accurate cost optimization |
| 🧠 **Split consumption prediction** | Learns house base load and EV charging patterns separately (weekday/weekend, time-of-day) |
| 🚗 **Multi-vehicle EV control** | Supports multiple chargers with independent departure times, SoC targets, and min charge levels |
| ☀️ **Solar surplus charging** | Automatically charges EVs with excess solar (dynamic current limiting when supported) |
| ⚡ **Negative price handling** | Maximizes load during negative prices — charges battery and all EVs at full power |
| 🔋 **Sungrow battery control** | Force charge/discharge, self-consumption, power setpoints, export power limiting |
| 🔋 **Dual inverter support** | Primary + slave Sungrow inverters (slave is read-only, all control via master) |
| 📅 **2-day optimization** | Optional 48-hour window defers EV charging to cheaper day-2 hours above a min charge floor |
| 🌙 **Night preference** | Prefers off-peak hours for EV charging to reduce grid stress |
| 🗺️ **Variable mapping** | All HA entity IDs in one YAML file — easy to adapt to any hardware |
| 📓 **Prediction log** | Internal log with accuracy tracking (MAE, MAPE) viewable on dashboards |
| 💰 **Savings estimate** | Sensor showing estimated daily savings from optimisation |
| 🔄 **Auto-replan** | Instant re-optimization when UI input helpers change (departure time, SoC targets, tariffs) |
| 🛡️ **Manual override respect** | Detects user-initiated switch changes and honours them for a cooldown period |

---

## 📦 Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the **three-dot menu** → **Custom repositories**
3. Add this repository URL, select category **Integration**
4. Click **Install**
5. **Restart** Home Assistant
6. Go to **Settings → Devices & Services → Add Integration → Home Energy Management**

### Manual

```bash
# Navigate to your Home Assistant config directory
cd /config/custom_components

# Clone the repository
git clone https://github.com/rarvids4/HomeEnergyManagement.git home_energy_management

# Restart Home Assistant
```

---

## ⚙️ Configuration

All configuration lives in a single YAML file: [`config/variable_mapping.yaml`](config/variable_mapping.yaml).

1. Copy it to `variable_mapping.local.yaml` (gitignored)
2. Replace every `CHANGE_ME_*` placeholder with your real HA entity IDs
3. Restart Home Assistant

The integration loads `variable_mapping.local.yaml` first (if it exists), falling back to the bundled template. You can also write the local config remotely via the `write_local_config` service.

> 💡 **Tip:** Find your entity IDs in **Developer Tools → States**

---

### Input Variables (sensors the integration reads)

#### Nordpool Prices

```yaml
inputs:
  nordpool:
    current_price: "sensor.nordpool_kwh_se3_sek_3_10_025"
    today_prices_attribute: "today"          # Attribute with today's price list
    tomorrow_prices_attribute: "tomorrow"    # Attribute for tomorrow's prices (available after ~13:00)
    raw_today_attribute: "raw_today"         # Optional: raw objects with {start, end, value}
    raw_tomorrow_attribute: "raw_tomorrow"
    entries_per_hour: 4    # 1 = hourly (24/day), 4 = 15-min (96/day) — auto-aggregated to hourly
    currency: "SEK"
    price_unit: "kWh"
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `current_price` | entity_id | *required* | Nordpool sensor entity |
| `today_prices_attribute` | string | `"today"` | Attribute name holding today's price list |
| `tomorrow_prices_attribute` | string | `"tomorrow"` | Attribute name holding tomorrow's price list |
| `raw_today_attribute` | string | `"raw_today"` | Optional: attribute with `{start, end, value}` objects |
| `raw_tomorrow_attribute` | string | `"raw_tomorrow"` | Optional: attribute with `{start, end, value}` objects |
| `entries_per_hour` | int | `1` | Number of price entries per hour. Set to `4` for 15-minute Nordpool data |
| `currency` | string | `"SEK"` | Price currency code |
| `price_unit` | string | `"kWh"` | Price unit |

#### EV Chargers

Supports multiple chargers as a list. Each charger is controlled via a switch entity.

```yaml
inputs:
  ev_chargers:
    - name: "my_ev"
      friendly_name: "My Electric Vehicle"
      status: "sensor.my_ev_status"
      power: "sensor.my_ev_power"
      power_unit: "kW"                                # "kW" or "W"
      session_energy: "sensor.my_ev_session_energy"
      lifetime_energy: "sensor.my_ev_lifetime_energy"
      charger_switch: "switch.my_ev_charger_enabled"

      # Vehicle API sensors (optional — improves SoC tracking)
      vehicle_soc: "sensor.my_ev_battery"
      vehicle_capacity_kwh: "sensor.my_ev_battery_capacity"
      vehicle_capacity_kwh_fallback: 52               # Static fallback when API returns "unknown"
      vehicle_target_soc: "sensor.my_ev_target_battery_charge_level"
      vehicle_charging_power: "sensor.my_ev_charging_power"
      vehicle_charging_power_fallback: 7400            # Watts — fallback when API is asleep

      # Departure / SoC settings (static fallbacks)
      departure_time: "07:00"
      min_departure_soc: 80
      min_charge_level: 20     # SoC floor — car never sits below this

      # HA input helpers (override static values from the UI)
      departure_time_entity: "input_datetime.my_ev_departure_time"
      min_departure_soc_entity: "input_number.my_ev_departure_soc"
      min_charge_level_entity: "input_number.my_ev_min_charge_level"
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | *required* | Internal name — must match the corresponding output charger name |
| `friendly_name` | string | — | Human-readable name for logs |
| `status` | entity_id | *required* | Charger status sensor (states: `charging`, `awaiting_start`, `connected`, `disconnected`, `completed`) |
| `power` | entity_id | *required* | Current charging power sensor |
| `power_unit` | string | `"W"` | `"kW"` or `"W"` — kW is auto-converted to W internally |
| `session_energy` | entity_id | — | Current session energy (kWh) |
| `lifetime_energy` | entity_id | — | Lifetime energy counter (kWh) |
| `charger_switch` | entity_id | — | Switch entity — `on` = charger ready/connected |
| `vehicle_soc` | entity_id | — | Vehicle battery percentage (from car API) |
| `vehicle_capacity_kwh` | entity_id | — | Vehicle battery capacity sensor (kWh) |
| `vehicle_capacity_kwh_fallback` | float | `0` | Static capacity (kWh) used when the sensor returns `unknown` |
| `vehicle_target_soc` | entity_id | — | Car's own target charge level (%) |
| `vehicle_charging_power` | entity_id | — | Max admissible charging power from car API |
| `vehicle_charging_power_fallback` | float | `0` | Static charging power (W) when the sensor is unavailable |
| `departure_time` | string | `"07:00"` | Time by which the car must be charged (HH:MM) |
| `min_departure_soc` | int | `100` | Target charge level (%) at departure |
| `min_charge_level` | int | `20` | SoC floor (%) — with 2-day optimization, charging above this may be deferred |
| `departure_time_entity` | entity_id | — | `input_datetime` helper — overrides `departure_time` at runtime |
| `min_departure_soc_entity` | entity_id | — | `input_number` helper — overrides `min_departure_soc` at runtime |
| `min_charge_level_entity` | entity_id | — | `input_number` helper — overrides `min_charge_level` at runtime |

#### Sungrow Inverter / Battery

```yaml
inputs:
  sungrow:
    battery_soc: "sensor.battery_level"
    battery_power: "sensor.signed_battery_power"    # Positive = charging (W)
    pv_power: "sensor.total_dc_power"
    grid_import_power: "sensor.import_power"
    grid_export_power: "sensor.export_power"
    house_load: "sensor.load_power"
    daily_pv_energy: "sensor.daily_pv_generation"
    daily_grid_import: "sensor.daily_imported_energy"
    daily_grid_export: "sensor.daily_exported_energy"
    daily_battery_charge: "sensor.daily_battery_charge"
    daily_battery_discharge: "sensor.daily_battery_discharge"
    battery_soh: "sensor.battery_state_of_health"
    battery_capacity_kwh: 10.0
    mppt1_power: "sensor.mppt1_power"               # Optional MPPT channels
    mppt2_power: "sensor.mppt2_power"
```

#### Sungrow Inverter 2 (optional slave)

A second inverter that provides read-only sensor data. All battery control goes through the master. PV and load values are automatically added to the totals.

```yaml
inputs:
  sungrow_2:
    battery_soc: "sensor.battery_level_2"
    battery_power: "sensor.signed_battery_power_2"
    pv_power: "sensor.total_dc_power_2"
    grid_import_power: "sensor.import_power_2"
    grid_export_power: "sensor.export_power_2"
    house_load: "sensor.load_power_2"
```

#### Smart Meter

```yaml
inputs:
  smart_meter:
    total_import: "sensor.meter_total_import"
    total_export: "sensor.meter_total_export"
    import_power: "sensor.meter_import_power"
    export_power: "sensor.meter_export_power"
    surplus_charging: "switch.meter_surplus_charging"  # Optional
```

#### Weather (optional)

```yaml
inputs:
  weather:
    entity: "weather.forecast_home"
    temperature: null    # null = read from weather entity attributes
```

---

### Output Variables (entities/services the integration controls)

#### EV Charger Control

Each charger is controlled by switch on/off. Optionally, a dynamic current limit can be configured for solar surplus tracking.

```yaml
outputs:
  ev_chargers:
    - name: "my_ev"                          # Must match the input charger name
      start_charging:
        service: "switch.turn_on"
        entity_id: "switch.my_ev_charger_enabled"
      stop_charging:
        service: "switch.turn_off"
        entity_id: "switch.my_ev_charger_enabled"

      # Optional: dynamic current limit for solar surplus tracking
      set_dynamic_limit:
        service: "easee.set_charger_dynamic_limit"
        device_id: "abc123..."               # Device ID from HA
        voltage: 230                         # Grid voltage (V)
        phases: 3                            # Number of phases
        min_current: 6                       # Minimum amps (A)
        max_current: 32                      # Maximum amps (A)
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | *required* | Must match the corresponding input charger `name` |
| `start_charging.service` | string | *required* | HA service to start charging (e.g. `switch.turn_on`) |
| `start_charging.entity_id` | entity_id | *required* | Target entity for start service |
| `stop_charging.service` | string | *required* | HA service to stop charging |
| `stop_charging.entity_id` | entity_id | *required* | Target entity for stop service |
| `set_dynamic_limit.service` | string | — | Service to set charger current limit (e.g. `easee.set_charger_dynamic_limit`) |
| `set_dynamic_limit.device_id` | string | — | HA device ID for the charger |
| `set_dynamic_limit.voltage` | int | `230` | Grid voltage in volts |
| `set_dynamic_limit.phases` | int | `3` | Number of charger phases |
| `set_dynamic_limit.min_current` | int | `6` | Minimum charging current (A) |
| `set_dynamic_limit.max_current` | int | `32` | Maximum charging current (A) |

> **Note:** Without `set_dynamic_limit`, the charger can only be started/stopped. Solar surplus charging will oscillate on/off instead of smoothly tracking the available surplus. If your charger supports current control, always configure this section.

#### Sungrow Battery Control

```yaml
outputs:
  sungrow:
    # Script-based mode switching (recommended)
    force_charge:
      service: "script.turn_on"
      entity_id: "script.sg_set_forced_charge_battery_mode"
    force_discharge:
      service: "script.turn_on"
      entity_id: "script.sg_set_forced_discharge_battery_mode"
    self_consumption:
      service: "script.turn_on"
      entity_id: "script.sg_set_self_consumption_mode"
    self_consumption_limited:                # Optional
      service: "script.turn_on"
      entity_id: "script.sg_set_self_consumption_limited_discharge"
    battery_bypass:                          # Optional
      service: "script.turn_on"
      entity_id: "script.sg_set_battery_bypass_mode"

    # Input_select alternative (less reliable but direct)
    battery_mode_select: "input_select.battery_forced_charge_discharge_cmd"
    battery_mode_options:
      stop: "Stop (default)"
      force_charge: "Forced charge"
      force_discharge: "Forced discharge"

    # Forced charge/discharge power setpoint (register 13052)
    set_forced_power:
      service: "input_number.set_value"
      entity_id: "input_number.sg_forced_charge_discharge_power"
      min: 0
      max: 5000
      step: 100

    # Charge/discharge power limits (safety caps)
    set_charge_power:
      service: "input_number.set_value"
      entity_id: "input_number.sg_battery_max_charge_power"
      min: 100
      max: 5000
      step: 100
    set_discharge_power:
      service: "input_number.set_value"
      entity_id: "input_number.sg_battery_max_discharge_power"
      min: 10
      max: 5000
      step: 100

    # Export power limit (register 13087/13088) — optional
    # Caps grid feed-in during negative prices to avoid paying to export.
    set_export_limit:
      service: "input_number.set_value"
      entity_id: "input_number.sg_export_power_limit"
      min: 0
      max: 5000
      negative_price_limit: 100    # W — cap applied when spot price < 0

    # SoC limits
    set_min_soc:
      service: "input_number.set_value"
      entity_id: "input_number.sg_min_soc"
    set_max_soc:
      service: "input_number.set_value"
      entity_id: "input_number.sg_max_soc"

    # Operational limits
    min_soc: 10        # % — don't discharge below this
    max_soc: 100       # % — don't charge above this
    capacity_kwh: 10.0 # Battery capacity in kWh
```

---

### System Parameters

All tuning parameters live in the `parameters:` section. Every parameter has a built-in default so you only need to set values you want to override.

#### Core Optimization

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `optimization_interval_minutes` | int | `15` | How often the optimizer re-runs (minutes) |
| `planning_horizon_hours` | int | `24` | How many hours ahead to plan |
| `min_price_spread` | float | `0.30` | Minimum price difference (SEK/kWh) to justify a charge/discharge cycle (heuristic fallback) |
| `enable_charger_control` | bool | `true` | Toggle all EV charger control on/off |
| `enable_battery_control` | bool | `true` | Toggle all battery control on/off |
| `log_level` | string | `"info"` | Internal prediction log level (`debug` / `info` / `warning`) |

#### LP Battery Optimizer

The battery schedule is computed by a **linear-programming (LP) solver** (scipy `linprog`, HiGHS method with revised-simplex fallback). It finds the mathematically optimal charge/discharge plan that **minimises total electricity cost** over the planning horizon, subject to:

| Constraint | Detail |
|------------|--------|
| **SoC bounds** | SoC stays between a hard floor of **6 %** and `max_soc` (default 100 %) at every hour |
| **Power limits** | Charge/discharge power capped at `battery_max_charge_power_w` / `battery_max_discharge_power_w` (default 5 000 W each) |
| **Round-trip efficiency** | **85 %** round-trip (√0.85 ≈ 0.922 applied to both charge and discharge) |
| **Grid neutrality** | When `self_consumption` is chosen, battery neither charges from nor discharges to grid |

**Pricing model:**

- **Buy price** (grid → battery): Nordpool spot price (incl. 25 % VAT) **+** time-of-use grid tariff (incl. VAT)
- **Sell price** (battery → grid): Nordpool spot × `sell_price_factor` (default 1.0)
- **Negative-price check**: Uses spot price only (before tariffs) — when spot < 0, the battery charges at max regardless

The LP solver replaces the earlier heuristic `grid_charge_max_soc` / `grid_charge_max_price` parameters, which have been removed. The solver automatically determines the optimal hours and SoC levels for grid charging based on price differentials and efficiency losses.

> 💡 **Fallback:** If scipy is unavailable or the LP solver fails, a simple heuristic classifier is used instead (charge at cheap hours, discharge at expensive hours, based on `min_price_spread`).

#### Consumption Prediction

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prediction_history_days` | int | `14` | Number of days of history to use for consumption prediction |
| `prediction_recency_weight` | float | `0.7` | Weight for recent observations (0.0–1.0). Higher = more weight on recent days |

The predictor tracks **house base load** and **EV charging load** as separate streams so that sporadic EV sessions don't distort the regular household pattern.

#### Grid Transfer Tariffs

Time-of-use network fees added to spot prices to get the **effective cost** per kWh. This lets the optimizer correctly prioritize hours when your total cost (spot + network) is lowest.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `grid_tariff_peak_sek` | float | `0.0` | Daytime grid fee (SEK/kWh, incl. VAT) |
| `grid_tariff_offpeak_sek` | float | `0.0` | Nighttime grid fee (SEK/kWh, incl. VAT) |
| `grid_tariff_peak_start` | int | `6` | Hour when peak tariff begins (0–23) |
| `grid_tariff_peak_end` | int | `22` | Hour when peak tariff ends (0–23) |
| `grid_tariff_peak_entity` | entity_id | — | `input_number` helper — overrides `grid_tariff_peak_sek` at runtime (when > 0) |
| `grid_tariff_offpeak_entity` | entity_id | — | `input_number` helper — overrides `grid_tariff_offpeak_sek` at runtime (when > 0) |

> **Example:** Vattenfall SE3 (incl. 25% VAT): peak 1.40 SEK/kWh, off-peak 0.831 SEK/kWh

#### EV Charging Strategy

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ev_default_target_soc` | int | `100` | Default target SoC (%) for vehicles without a per-vehicle setting |
| `ev_default_min_departure_soc` | int | `100` | Default departure SoC (%) — charge to this by departure time |
| `ev_default_min_charge_level` | int | `20` | Default SoC floor (%) — car never sits below this |
| `ev_default_departure_time` | string | `"07:00"` | Default departure time (HH:MM) for vehicles without a per-vehicle setting |
| `ev_cheap_price_threshold` | float | `0.10` | Always charge EVs when price is below this (SEK/kWh) |
| `ev_weekend_target_soc` | int | `80` | On Fridays, lower target SoC to this (car parked at home, solar fills later) |
| `solar_surplus_threshold_w` | int | `2000` | Minimum grid export (W) to trigger solar surplus EV charging |

#### EV Night Preference

The scheduler applies a price bonus to off-peak hours, preferring night charging even when daytime prices are marginally cheaper.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ev_night_start` | int | `22` | Hour when off-peak/night window starts (0–23) |
| `ev_night_end` | int | `6` | Hour when off-peak/night window ends (0–23) |
| `ev_night_preference_sek` | float | `0.10` | Night bonus (SEK/kWh) — subtracted from night prices during sorting |

#### 2-Day EV Optimization

When tomorrow's prices are available (typically after 13:00), the optimizer can look across a 48-hour window to find even cheaper charging opportunities.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ev_optimization_window` | int | `1` | `1` = today+remaining hours only; `2` = up to 48 hours ahead |
| `optimization_days_entity` | entity_id | — | `input_number` helper — overrides `ev_optimization_window` from the HA UI |

With `ev_optimization_window: 2`, the scheduler uses a **two-pass** strategy:
1. **Urgent pass:** Charge from current SoC up to `min_charge_level` using the cheapest near-term hours (before departure).
2. **Deferred pass:** Charge from `min_charge_level` up to `min_departure_soc` using the cheapest hours across the full 48-hour window — potentially deferring to cheaper day-2 hours.

#### Full Parameters Example

```yaml
parameters:
  optimization_interval_minutes: 15
  planning_horizon_hours: 24
  min_price_spread: 0.30
  prediction_history_days: 14
  prediction_recency_weight: 0.7
  enable_charger_control: true
  enable_battery_control: true

  grid_tariff_peak_sek: 1.40
  grid_tariff_offpeak_sek: 0.831
  grid_tariff_peak_start: 6
  grid_tariff_peak_end: 22
  grid_tariff_peak_entity: "input_number.grid_tariff_peak"
  grid_tariff_offpeak_entity: "input_number.grid_tariff_offpeak"

  ev_default_target_soc: 100
  ev_cheap_price_threshold: 0.10
  ev_night_start: 22
  ev_night_end: 6
  ev_night_preference_sek: 0.10
  ev_weekend_target_soc: 80
  solar_surplus_threshold_w: 2000

  ev_optimization_window: 1
  optimization_days_entity: "input_number.ev_optimization_days"

  log_level: "info"
```

---

## ⚡ Real-Time Overrides (Action Builder)

Beyond the hourly schedule, the action builder applies real-time overrides every cycle:

| Override | Trigger | Behaviour |
|----------|---------|-----------|
| **Negative price** | `current_price < 0` | All EVs charge at max current, ignore SoC limits — you get paid to consume |
| **Solar surplus** | `grid_export ≥ solar_surplus_threshold_w` | EVs charge using surplus. Dynamic current limit matches available power (if configured). Charging up to vehicle's own target SoC |
| **Ramp-down** | `vehicle_soc ≥ min_departure_soc` | Charger stopped — vehicle has reached its target |
| **Weekend target** | Friday (day 4) | SoC target reduced to `ev_weekend_target_soc` (car will top up from solar Saturday) |
| **Expensive hour** | Battery is discharging | Charger stopped — selling to grid is more profitable |
| **Manual override** | User turns off charger switch manually | Automation respects the choice for a cooldown period (2× optimization interval, minimum 30 min) |
| **Export limit** | `spot_price < 0` | Inverter grid feed-in capped to `negative_price_limit` watts to avoid paying to export |

---

## 📊 Sensors Created

The integration creates these sensor entities in Home Assistant:

| Sensor | Description |
|--------|-------------|
| `sensor.home_energy_management_optimization_status` | Current optimizer state (`ok` / `error`), summary, and price stats |
| `sensor.home_energy_management_current_energy_price` | Current Nordpool price with `today_prices` and `tomorrow_prices` attributes |
| `sensor.home_energy_management_next_planned_action` | Current action (`charge_battery` / `discharge_battery` / `self_consumption` / `maximize_load`), reason, and price |
| `sensor.home_energy_management_predicted_consumption` | Predicted next-hour consumption (kWh) with full 24h breakdown: `house_base_24h`, `ev_charging_24h`, `next_24h` |
| `sensor.home_energy_management_actual_consumption` | Actual consumption this cycle with `house_load_w`, `house_base_w`, `ev_power_w` |
| `sensor.home_energy_management_battery_plan` | Planned charge/discharge hours with `full_plan` attribute containing per-hour details |
| `sensor.home_energy_management_ev_charger_plan` | EV schedule with per-vehicle details: SoC, target, capacity, charge windows, scheduled hours |
| `sensor.home_energy_management_estimated_daily_savings` | Estimated daily savings from optimisation (SEK) |
| `sensor.home_energy_management_prediction_log` | Recent log entries with accuracy summary |
| `sensor.home_energy_management_prediction_accuracy_mae` | Prediction accuracy: MAE, MAPE, per-stream breakdown (house/EV/total), last 24h and 7d |

---

## 🔧 Services

| Service | Description |
|---------|-------------|
| `home_energy_management.force_replan` | Force the optimizer to re-run immediately |
| `home_energy_management.write_local_config` | Write/update `variable_mapping.local.yaml` remotely (paste full YAML) |
| `home_energy_management.read_local_config` | Read the current local config and show it as a persistent notification |

---

## 📁 Project Structure

```
HomeEnergyManagement/
├── custom_components/
│   └── home_energy_management/
│       ├── __init__.py          # Integration setup & config loading
│       ├── manifest.json        # HA integration manifest
│       ├── const.py             # Constants & default values
│       ├── config_flow.py       # UI configuration flow
│       ├── coordinator.py       # Data update coordinator (sensor reads, action execution)
│       ├── optimizer.py         # Orchestrator — delegates to sub-modules
│       ├── price_analysis.py    # Price horizon, grid tariff, statistics
│       ├── battery_strategy.py  # LP-based hour classification, charge/discharge decisions
│       ├── ev_scheduler.py      # Per-vehicle EV charging schedule
│       ├── action_builder.py    # Translates decisions into HA service calls
│       ├── predictor.py         # Split-stream consumption prediction
│       ├── sensor.py            # HA sensor entities
│       ├── services.py          # HA service handlers
│       ├── services.yaml        # Service definitions
│       ├── logger.py            # Prediction & decision log with accuracy tracking
│       ├── strings.json         # UI strings
│       └── translations/
│           └── en.json
├── config/
│   ├── variable_mapping.yaml        # 🗺️ Template — copy & customise
│   └── variable_mapping.local.yaml  # Your real config (gitignored)
├── tests/
│   ├── test_optimizer.py
│   ├── test_predictor.py
│   ├── test_mapping.py
│   └── test_logger.py
├── hacs.json
├── requirements.txt
└── README.md
```

---

## 🗺️ Supported Hardware

| Device | Role | Integration |
|--------|------|-------------|
| **Nordpool** | Electricity prices (hourly or 15-min) | [nordpool](https://github.com/custom-components/nordpool) HACS integration |
| **Sungrow** | Inverter + Battery (1 or 2 inverters) | Your existing Sungrow/Modbus integration |
| **Any switch-based charger** | EV Charger (Easee, Zaptec, etc.) | Any integration exposing a switch entity |
| **Easee** | EV Charger with dynamic current | [easee_hass](https://github.com/fondberg/easee_hass) — supports `set_dynamic_limit` |
| **Smart Meter** | Grid import/export | Via Sungrow CT or separate P1/HAN integration |
| **Weather** | Temperature for prediction | Built-in HA weather integration |

---

## 🧪 Development

```bash
# Clone the repo
git clone https://github.com/rarvids4/HomeEnergyManagement.git
cd HomeEnergyManagement

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/ -v
```

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Run tests (`pytest tests/ -v`)
4. Commit your changes
5. Push to your branch and open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">
  Made with ⚡ for smarter energy management
</p>
