"""Pytest conftest — mock Home Assistant modules for unit tests.

The custom component imports from homeassistant.* which is only
available inside a real HA environment.  We inject lightweight
stubs into sys.modules so that ``import homeassistant…`` succeeds
during test collection.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _stub(name: str) -> ModuleType:
    """Return an existing stub or create a new MagicMock module."""
    if name not in sys.modules:
        mod = MagicMock(spec=ModuleType)
        mod.__name__ = name
        mod.__path__ = []          # mark as package so sub-imports work
        mod.__file__ = f"<stub {name}>"
        sys.modules[name] = mod
    return sys.modules[name]


# All homeassistant sub-modules referenced anywhere in the component
_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
]

for _mod_name in _HA_MODULES:
    _stub(_mod_name)

# Make sure the stubs provide the names the component actually imports:
# homeassistant.config_entries.ConfigEntry
sys.modules["homeassistant.config_entries"].ConfigEntry = MagicMock()

# homeassistant.core.HomeAssistant / ServiceCall
sys.modules["homeassistant.core"].HomeAssistant = MagicMock()
sys.modules["homeassistant.core"].ServiceCall = MagicMock()

# homeassistant.helpers.update_coordinator.DataUpdateCoordinator / CoordinatorEntity
sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = type(
    "DataUpdateCoordinator", (), {"__init__": lambda *a, **kw: None}
)
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = type(
    "CoordinatorEntity", (), {}
)

# homeassistant.components.sensor – SensorEntity / SensorDeviceClass / SensorStateClass
sys.modules["homeassistant.components.sensor"].SensorEntity = type("SensorEntity", (), {})
sys.modules["homeassistant.components.sensor"].SensorDeviceClass = MagicMock()
sys.modules["homeassistant.components.sensor"].SensorStateClass = MagicMock()

# homeassistant.helpers.entity_platform.AddEntitiesCallback
sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = MagicMock()

# homeassistant – config_entries attribute (for ``from homeassistant import config_entries``)
sys.modules["homeassistant"].config_entries = sys.modules["homeassistant.config_entries"]
