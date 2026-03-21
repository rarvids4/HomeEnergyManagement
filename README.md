<p align="center">
  <img src="https://img.shields.io/badge/Home%20Assistant-Custom%20Component-blue?logo=homeassistant&logoColor=white" alt="Home Assistant" />
  <img src="https://img.shields.io/badge/HACS-Custom-orange?logo=homeassistantcommunitystore&logoColor=white" alt="HACS" />
  <img src="https://img.shields.io/badge/Python-3.11+-green?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" />
</p>

# ⚡ Home Energy Management

> A smart Home Assistant integration that **optimises charging and discharging** of your EV charger and home battery based on **Nordpool energy prices** and **predicted consumption patterns** — saving you money automatically.

---

## 🎯 What It Does

```
┌─────────────────────────────────────────────────────────────┐
│                    Nordpool Prices                           │
│   💰 Low price  → Charge battery + Charge EV               │
│   💸 High price → Discharge battery + Stop EV charging     │
│   📊 Normal     → Self-consumption mode                    │
└─────────────────────────────────────────────────────────────┘
```

The integration runs every 15 minutes (configurable) and:

1. **Reads** current and upcoming Nordpool hourly electricity prices
2. **Predicts** your household energy consumption using historical patterns
3. **Plans** an optimal 24-hour charge/discharge schedule
4. **Controls** your Easee charger and Sungrow battery automatically
5. **Logs** every decision so you can review predictions vs reality

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔌 **Price-aware scheduling** | Detects price peaks/valleys from Nordpool, plans charge/discharge windows |
| 🧠 **Consumption prediction** | Learns your energy patterns (weekday/weekend, time-of-day, seasonal) |
| 🚗 **Easee charger control** | Starts/stops EV charging, adjusts amperage during optimal price windows |
| 🔋 **Sungrow battery control** | Switches between charge/discharge/self-consumption modes automatically |
| 🗺️ **Variable mapping** | All HA entity IDs configured in one YAML file — easy to adapt |
| 📓 **Prediction log** | Internal log of every planning decision, viewable on HA dashboards |
| 💰 **Savings estimate** | Sensor showing estimated daily savings from optimisation |

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

### Variable Mapping

All sensor and control entity mappings live in one file: [`config/variable_mapping.yaml`](config/variable_mapping.yaml)

Edit it to match **your** Home Assistant entity IDs:

```yaml
inputs:
  nordpool:
    current_price: "sensor.nordpool_kwh_se3_sek_3_10_025"  # ← Your entity
  easee:
    status: "sensor.easee_status"
    power: "sensor.easee_power"
  sungrow:
    battery_soc: "sensor.sungrow_battery_soc"
    pv_power: "sensor.sungrow_pv_power"
    house_load: "sensor.sungrow_house_load"

outputs:
  easee:
    start_charging:
      service: "easee.start"
      entity_id: "switch.easee_is_enabled"
  sungrow:
    force_charge:
      service: "select.select_option"
      entity_id: "select.sungrow_battery_mode"
      mode_value: "force_charge"
```

> 💡 **Tip:** Find your entity IDs in **Developer Tools → States**

### Tuning Parameters

```yaml
parameters:
  optimization_interval_minutes: 15   # How often to re-plan
  planning_horizon_hours: 24          # How far ahead to look
  min_price_spread: 0.30              # Min SEK/kWh spread to trigger cycling
  prediction_history_days: 14         # Days of history for predictions
  enable_charger_control: true        # Toggle EV charger control
  enable_battery_control: true        # Toggle battery control
```

---

## 📊 Sensors Created

The integration creates these sensor entities in Home Assistant:

| Sensor | Description |
|--------|-------------|
| `sensor.optimization_status` | Current optimizer state (ok / error) |
| `sensor.current_energy_price` | Current Nordpool price with today/tomorrow in attributes |
| `sensor.next_planned_action` | What the optimizer is doing right now (charge/discharge/self) |
| `sensor.predicted_consumption` | Predicted consumption for the next hour (kWh) |
| `sensor.battery_plan` | Overview of planned charge/discharge hours |
| `sensor.charger_plan` | EV charger schedule overview |
| `sensor.daily_savings` | Estimated daily savings from optimisation |
| `sensor.prediction_log` | Recent log entries for dashboard display |

---

## 📁 Project Structure

```
HomeEnergyManagement/
├── custom_components/
│   └── home_energy_management/
│       ├── __init__.py          # Integration setup & entry points
│       ├── manifest.json        # HA integration manifest
│       ├── const.py             # Constants & default values
│       ├── config_flow.py       # UI configuration flow
│       ├── coordinator.py       # Data update coordinator
│       ├── optimizer.py         # Price-peak scheduling engine
│       ├── predictor.py         # Consumption prediction
│       ├── sensor.py            # HA sensor entities
│       ├── services.py          # HA service handlers
│       ├── services.yaml        # Service definitions
│       ├── logger.py            # Prediction & decision log
│       ├── strings.json         # UI strings
│       └── translations/
│           └── en.json
├── config/
│   └── variable_mapping.yaml    # 🗺️ Map your HA entities here
├── tests/
│   ├── test_optimizer.py        # Optimizer unit tests
│   ├── test_predictor.py        # Predictor unit tests
│   ├── test_mapping.py          # Mapping file validation
│   └── test_logger.py           # Logger unit tests
├── hacs.json                    # HACS metadata
├── requirements.txt             # Python dependencies
└── README.md
```

---

## 🗺️ Supported Hardware

| Device | Role | Integration |
|--------|------|-------------|
| **Nordpool** | Electricity prices | [nordpool](https://github.com/custom-components/nordpool) HACS integration |
| **Easee** | EV Charger | [easee_hass](https://github.com/fondberg/easee_hass) HACS integration |
| **Sungrow** | Inverter + Battery | Your existing Sungrow integration |
| **Smart Meter** | Grid import/export | Via Sungrow or separate integration |

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
