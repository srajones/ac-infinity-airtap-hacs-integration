import importlib.machinery
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for name, path in [
    ("custom_components", ROOT / "custom_components"),
    ("custom_components.ac_infinity_airtap_ble", ROOT / "custom_components/ac_infinity_airtap_ble"),
]:
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module

ha = ModuleType("homeassistant")
ha.__path__ = []
ha.__spec__ = importlib.machinery.ModuleSpec("homeassistant", None, is_package=True)
sys.modules["homeassistant"] = ha

core = ModuleType("homeassistant.core")
core.callback = lambda f: f
core.HomeAssistant = object
sys.modules["homeassistant.core"] = core

exceptions = ModuleType("homeassistant.exceptions")
exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
sys.modules["homeassistant.exceptions"] = exceptions

const = ModuleType("homeassistant.const")
const.PERCENTAGE = "%"
const.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
const.STATE_UNAVAILABLE = "unavailable"
const.STATE_UNKNOWN = "unknown"
const.CONF_ADDRESS = "address"
const.CONF_SERVICE_DATA = "service_data"
const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config")
const.UnitOfPressure = SimpleNamespace(KPA="kPa")
const.UnitOfTemperature = SimpleNamespace(CELSIUS="°C", FAHRENHEIT="°F")
const.Platform = SimpleNamespace(
    SENSOR="sensor", FAN="fan", NUMBER="number", SWITCH="switch", SELECT="select", BUTTON="button"
)
sys.modules["homeassistant.const"] = const

components = ModuleType("homeassistant.components")
components.__path__ = []
components.__spec__ = importlib.machinery.ModuleSpec("homeassistant.components", None, is_package=True)
sys.modules["homeassistant.components"] = components

bt = ModuleType("homeassistant.components.bluetooth")
bt.BluetoothScanningMode = SimpleNamespace(ACTIVE="active", PASSIVE="passive")
bt.BluetoothServiceInfoBleak = object
bt.BluetoothChange = object
bt.async_ble_device_from_address = lambda *a, **kw: None
bt.async_discovered_service_info = lambda *a, **kw: []
bt.async_last_service_info = lambda *a, **kw: None
sys.modules["homeassistant.components.bluetooth"] = bt

puc = ModuleType("homeassistant.components.bluetooth.passive_update_coordinator")


class PassiveBluetoothDataUpdateCoordinator:
    def __init__(self, hass, logger, address, mode, connectable):
        self.hass = hass
        self.logger = logger
        self.address = address
        self.mode = mode
        self.connectable = connectable

    def _async_handle_bluetooth_event(self, service_info, change):
        pass

    def async_update_listeners(self):
        pass


puc.PassiveBluetoothDataUpdateCoordinator = PassiveBluetoothDataUpdateCoordinator


class PassiveBluetoothCoordinatorEntity:
    __class_getitem__ = classmethod(lambda cls, item: cls)

    def __init__(self, coordinator):
        self.coordinator = coordinator

    def _handle_coordinator_update(self):
        pass


puc.PassiveBluetoothCoordinatorEntity = PassiveBluetoothCoordinatorEntity
sys.modules["homeassistant.components.bluetooth.passive_update_coordinator"] = puc

helpers = ModuleType("homeassistant.helpers")
helpers.__path__ = []
helpers.__spec__ = importlib.machinery.ModuleSpec("homeassistant.helpers", None, is_package=True)
sys.modules["homeassistant.helpers"] = helpers

uc = ModuleType("homeassistant.helpers.update_coordinator")


class BaseCoordinatorEntity:
    __class_getitem__ = classmethod(lambda cls, item: cls)

    def __init__(self, coordinator):
        self.coordinator = coordinator


uc.BaseCoordinatorEntity = BaseCoordinatorEntity
sys.modules["homeassistant.helpers.update_coordinator"] = uc

dr = ModuleType("homeassistant.helpers.device_registry")
dr.CONNECTION_BLUETOOTH = "bluetooth"
sys.modules["homeassistant.helpers.device_registry"] = dr

entity = ModuleType("homeassistant.helpers.entity")


class DeviceInfo(dict):
    def __init__(self, **kwargs):
        super().__init__(kwargs)


entity.DeviceInfo = DeviceInfo
sys.modules["homeassistant.helpers.entity"] = entity

ep = ModuleType("homeassistant.helpers.entity_platform")
ep.AddEntitiesCallback = object
sys.modules["homeassistant.helpers.entity_platform"] = ep

util = ModuleType("homeassistant.util")
util.slugify = lambda s: s.lower().replace(" ", "_")
sys.modules["homeassistant.util"] = util

sensor_mod = ModuleType("homeassistant.components.sensor")


class SensorEntity:
    _attr_native_value = None

    @property
    def native_value(self):
        return self._attr_native_value


sensor_mod.SensorEntity = SensorEntity


class RestoreSensor(SensorEntity):
    async def async_get_last_sensor_data(self):
        return None

    async def async_get_last_state(self):
        return None

    async def async_added_to_hass(self):
        pass


sensor_mod.RestoreSensor = RestoreSensor
sensor_mod.SensorDeviceClass = SimpleNamespace(
    TEMPERATURE="temperature",
    SIGNAL_STRENGTH="signal_strength",
    PRESSURE="pressure",
    HUMIDITY="humidity",
    ATMOSPHERIC_PRESSURE="atmospheric_pressure",
)
sensor_mod.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement")
sys.modules["homeassistant.components.sensor"] = sensor_mod

entries = ModuleType("homeassistant.config_entries")


class ConfigEntry:
    entry_id = "test_entry"


entries.ConfigEntry = ConfigEntry


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass


entries.ConfigFlow = ConfigFlow
sys.modules["homeassistant.config_entries"] = entries
