from __future__ import annotations

import contextlib

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN, FAMILY_E_MODELS, MANUFACTURER, get_device_model_name
from .coordinator import ACInfinityDataUpdateCoordinator
from .device import ACInfinityDevice
from .models import ACInfinityData
from .vendor.ac_infinity_ble.protocol import get_mode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    device = data.device
    coordinator = data.coordinator

    entities: list[SensorEntity] = [
        TemperatureSensor(coordinator, device, "Temperature"),
        CurrentSpeedSensor(coordinator, device, "Current Speed"),
        OperatingModeSensor(coordinator, device, "Operating Mode"),
        OnSpeedSensor(coordinator, device, "On Speed"),
        SignalStrengthSensor(coordinator, device, "Signal Strength"),
        ProtocolVersionSensor(coordinator, device, "Protocol Version"),
    ]

    if device.state.type in (6, 48):
        if device.state.hum not in (None, 0, 0.0):
            entities.append(HumiditySensor(coordinator, device, "Humidity"))
        if device.state.vpd not in (None, 0, 0.0):
            entities.append(VpdSensor(coordinator, device, "VPD"))
    else:
        if device.state.hum is not None:
            entities.append(HumiditySensor(coordinator, device, "Humidity"))
        if device.state.vpd is not None or (
            device.state.version >= 3 and device.state.type in FAMILY_E_MODELS
        ):
            entities.append(VpdSensor(coordinator, device, "VPD"))

    if device.firmware_revision is not None:
        entities.append(FirmwareRevisionSensor(coordinator, device, "Firmware Revision"))
    if device.software_revision is not None:
        entities.append(SoftwareRevisionSensor(coordinator, device, "Software Revision"))
    if device.hardware_revision is not None:
        entities.append(HardwareRevisionSensor(coordinator, device, "Hardware Revision"))
    async_add_entities(entities)


class ACInfinitySensor(
    PassiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], SensorEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._name = name
        self._attr_name = name
        self._attr_unique_id = f"{self._device.address}_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=get_device_model_name(device.state.type),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )
        with contextlib.suppress(NotImplementedError):
            self._update_attrs()

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        raise NotImplementedError("Not yet implemented.")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()


class TemperatureSensor(ACInfinitySensor, RestoreSensor):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator, device, name)
        if self._device.state.tmp is not None:
            self._attr_native_value = self._device.state.tmp

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None:
            if last_sensor_data := await self.async_get_last_sensor_data():
                self._attr_native_value = last_sensor_data.native_value
            elif last_state := await self.async_get_last_state():
                if last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                    with contextlib.suppress(ValueError, TypeError):
                        self._attr_native_value = float(last_state.state)
        self._update_attrs()

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        if self._device.state.tmp is not None:
            self._attr_native_value = self._device.state.tmp


class CurrentSpeedSensor(ACInfinitySensor, RestoreSensor):
    """The speed the vent reports, including in Auto and off mode."""

    _attr_name = "Current Speed"
    _attr_icon = "mdi:fan"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator, device, name)
        if self._device.state.current_speed is not None:
            self._attr_native_value = self._device.state.current_speed
        elif self._device.state.fan is not None:
            self._attr_native_value = self._device.state.fan

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None:
            if last_sensor_data := await self.async_get_last_sensor_data():
                self._attr_native_value = last_sensor_data.native_value
            elif last_state := await self.async_get_last_state():
                if last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                    with contextlib.suppress(ValueError, TypeError):
                        self._attr_native_value = float(last_state.state)
        self._update_attrs()

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        if self._device.state.current_speed is not None:
            self._attr_native_value = self._device.state.current_speed
        elif self._device.state.fan is not None:
            self._attr_native_value = self._device.state.fan


class OperatingModeSensor(ACInfinitySensor):
    """The operating mode reported by the device (key 0x10)."""

    _attr_icon = "mdi:tune-vertical"

    @callback
    def _update_attrs(self) -> None:
        mode_id = self._device.state.work_type
        if mode_id is not None:
            self._attr_native_value = get_mode(mode_id)
        elif getattr(self._device.state, "current_speed", None) is not None:
            self._attr_native_value = "ON" if self._device.state.current_speed > 0 else "OFF"
        elif getattr(self._device.state, "fan", None) is not None:
            self._attr_native_value = "ON" if self._device.state.fan > 0 else "OFF"
        else:
            self._attr_native_value = None


class OnSpeedSensor(ACInfinitySensor):
    """The speed the vent is configured to run at in on mode (key 0x12)."""

    _attr_icon = "mdi:fan-plus"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        if self._device.state.level_on is not None:
            self._attr_native_value = self._device.state.level_on
        elif getattr(self._device.state, "current_speed", None) is not None:
            self._attr_native_value = self._device.state.current_speed
        elif getattr(self._device.state, "fan", None) is not None:
            self._attr_native_value = self._device.state.fan
        else:
            self._attr_native_value = None


class SignalStrengthSensor(ACInfinitySensor):
    """The Bluetooth RSSI signal strength."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._device.rssi


class ProtocolVersionSensor(ACInfinitySensor):
    """The AC Infinity advertisement protocol version."""

    _attr_icon = "mdi:numeric"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._device.state.version


class FirmwareRevisionSensor(ACInfinitySensor):
    """Device firmware SDK revision."""

    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._device.firmware_revision


class SoftwareRevisionSensor(ACInfinitySensor):
    """Device software revision."""

    _attr_icon = "mdi:package-up"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._device.software_revision


class HardwareRevisionSensor(ACInfinitySensor):
    """Device hardware revision."""

    _attr_icon = "mdi:memory"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._device.hardware_revision


class HumiditySensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = self._device.humidity


class VpdSensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = UnitOfPressure.KPA
    _attr_device_class = SensorDeviceClass.ATMOSPHERIC_PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = self._device.vpd
