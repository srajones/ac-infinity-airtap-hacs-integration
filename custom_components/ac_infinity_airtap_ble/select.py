from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from .const import DOMAIN, MANUFACTURER, get_device_model_name, is_passive_mode
from .coordinator import (ACInfinityDataUpdateCoordinator,
                          ActiveBluetoothCoordinatorEntity)
from .device import BRIGHTNESS_LEVEL_TO_BYTE, ACInfinityDevice
from .models import ACInfinityData

MODE_OFF = "Off"
MODE_ON = "On"
MODE_AUTO = "Auto"
VENT_MODE_OPTIONS = [MODE_OFF, MODE_ON, MODE_AUTO]
BRIGHTNESS_OPTIONS = [str(lvl) for lvl in sorted(BRIGHTNESS_LEVEL_TO_BYTE)]  # "1".."5"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    if is_passive_mode(entry, data.device):
        return
    async_add_entities(
        [
            ACInfinityDisplayBrightness(data.coordinator, data.device, "Display Brightness"),
            ACInfinityTemperatureUnit(data.coordinator, data.device, "Temperature Unit"),
            ACInfinityModeSelect(data.coordinator, data.device, "Mode"),
        ]
    )


class ACInfinityDisplayBrightness(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], SelectEntity, RestoreEntity
):
    """Display brightness, 5 discrete levels (cmd-3 setting key 0x21 byte[0])."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:brightness-6"
    _attr_options = BRIGHTNESS_OPTIONS

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_name = name
        self._attr_unique_id = f"{self._device.address}_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=get_device_model_name(device.state.type),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )
        self._update_attrs()

    async def async_added_to_hass(self) -> None:
        """Restore the last known value; nothing reads it until a command connects."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.options:
            self._restore(last.state)
            self._update_attrs()

    @callback
    def _restore(self, option: str) -> None:
        if self._device.state.display_brightness is None:
            self._device.state.display_brightness = BRIGHTNESS_LEVEL_TO_BYTE[int(option)]

    async def async_select_option(self, option: str) -> None:
        await self._device.set_display_brightness_level(int(option))
        self._write_state_from_device()

    @callback
    def _write_state_from_device(self) -> None:
        """Reflect commanded brightness in HA immediately (optimistic, like the switch)."""
        self._update_attrs()
        self.async_write_ha_state()

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        level = self._device.display_brightness_level
        self._attr_current_option = str(level) if level is not None else "5"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()


class ACInfinityTemperatureUnit(ACInfinityDisplayBrightness):
    """The unit the vent displays (cmd-3 setting key 0x20)."""

    _attr_icon = "mdi:temperature-fahrenheit"
    _attr_options = [UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS]

    @callback
    def _restore(self, option: str) -> None:
        if self._device.state.unit_celsius is None:
            self._device.state.unit_celsius = option == UnitOfTemperature.CELSIUS

    async def async_select_option(self, option: str) -> None:
        await self._device.set_temperature_unit(option == UnitOfTemperature.CELSIUS)
        self._write_state_from_device()

    @callback
    def _update_attrs(self) -> None:
        unit = self._device.state.unit_celsius
        if unit is None and getattr(self._device.state, "is_degree", None) is not None:
            unit = bool(self._device.state.is_degree)
        if unit is None:
            self._attr_current_option = None
        else:
            self._attr_current_option = (
                UnitOfTemperature.CELSIUS if unit else UnitOfTemperature.FAHRENHEIT
            )


class ACInfinityModeSelect(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], SelectEntity, RestoreEntity
):
    """Operating mode selector for AirTap vents (key 0x10)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:tune-vertical"
    _attr_options = VENT_MODE_OPTIONS

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_name = name
        self._attr_unique_id = f"{self._device.address}_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=get_device_model_name(device.state.type),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )
        self._update_attrs()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.options:
            self._restore(last.state)
            self._update_attrs()

    @callback
    def _restore(self, option: str) -> None:
        if self._device.state.work_type is None:
            if option == MODE_OFF:
                self._device.state.work_type = 1
            elif option == MODE_ON:
                self._device.state.work_type = 2
            elif option == MODE_AUTO:
                self._device.state.work_type = 3

    async def async_select_option(self, option: str) -> None:
        if option == MODE_OFF:
            await self._device.turn_off()
        elif option == MODE_ON:
            await self._device.turn_on()
        elif option == MODE_AUTO:
            await self._device.set_auto_mode()
        self._write_state_from_device()

    @callback
    def _write_state_from_device(self) -> None:
        self._update_attrs()
        self.async_write_ha_state()

    @callback
    def _update_attrs(self) -> None:
        mode = self._device.state.work_type
        if mode == 1:
            self._attr_current_option = MODE_OFF
        elif mode == 2:
            self._attr_current_option = MODE_ON
        elif mode == 3:
            self._attr_current_option = MODE_AUTO
        elif getattr(self._device.state, "current_speed", None) is not None:
            self._attr_current_option = (
                MODE_ON if self._device.state.current_speed > 0 else MODE_OFF
            )
        elif getattr(self._device.state, "fan", None) is not None:
            self._attr_current_option = MODE_ON if self._device.state.fan > 0 else MODE_OFF
        else:
            self._attr_current_option = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_attrs()
        super()._handle_coordinator_update()
