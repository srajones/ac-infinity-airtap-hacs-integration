from __future__ import annotations

import contextlib
import dataclasses
import logging

from .vendor.ac_infinity_ble import DeviceInfo
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ATTEMPT_PAIRING, CONF_PASSIVE_ONLY, DOMAIN, is_passive_mode
from .coordinator import ACInfinityDataUpdateCoordinator
from .device import ACInfinityDevice, DeviceInfoEx
from .models import ACInfinityData

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find AC Infinity device with address {address}"
        )

    service_data = entry.data[CONF_SERVICE_DATA]
    if type(service_data) is dict:
        valid_fields = {f.name for f in dataclasses.fields(DeviceInfoEx)}
        filtered_data = {k: v for k, v in service_data.items() if k in valid_fields}
        device_info = DeviceInfoEx(**filtered_data)
    elif type(service_data) is DeviceInfoEx:
        device_info = service_data
    elif type(service_data) is DeviceInfo:
        device_info = DeviceInfoEx.create(service_data)
    else:
        raise ValueError(
            f"Unexpected config entry service data type: {type(service_data)}"
        )

    attempt_pairing = entry.options.get(
        CONF_ATTEMPT_PAIRING, entry.data.get(CONF_ATTEMPT_PAIRING, False)
    )
    device = ACInfinityDevice(
        ble_device,
        device_info,
        protocol_variant=entry.data.get("protocol_variant"),
        attempt_pairing=attempt_pairing,
    )
    device.set_ble_device_callback(
        lambda: bluetooth.async_ble_device_from_address(hass, address.upper(), connectable=True)
    )
    coordinator = ACInfinityDataUpdateCoordinator(hass, _LOGGER, ble_device, device)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ACInfinityData(
        entry.title, device, coordinator
    )

    if (
        is_passive_mode(entry, device)
        and not (entry.options or {}).get(CONF_PASSIVE_ONLY)
        and not (entry.options or {}).get("_passive_migrated")
    ):
        with contextlib.suppress(Exception):
            hass.config_entries.async_update_entry(
                entry,
                options={
                    **(entry.options or {}),
                    CONF_PASSIVE_ONLY: True,
                    "_passive_migrated": True,
                },
            )

    _async_cleanup_stale_entities(hass, entry, device)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(coordinator.async_start())
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_cleanup_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, device: ACInfinityDevice
) -> None:
    """Remove stale or misnamed entity registry entries from earlier releases."""
    with contextlib.suppress(Exception):
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        if ent_reg is None:
            return
        passive = is_passive_mode(entry, device)
        is_airtap = device.state.type in (6, 48)
        unnamed_sensor_suffixes = (
            "_operating_mode",
            "_on_speed",
            "_protocol_version",
            "_firmware_revision",
            "_software_revision",
            "_hardware_revision",
        )
        for reg_entry in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
            uid = str(getattr(reg_entry, "unique_id", "") or "")
            domain = getattr(reg_entry, "domain", "") or reg_entry.entity_id.split(".", 1)[0]
            if is_airtap:
                if uid.endswith("_humidity") and device.state.hum in (None, 0, 0.0):
                    ent_reg.async_remove(reg_entry.entity_id)
                    continue
                if uid.endswith("_vpd") and device.state.vpd in (None, 0, 0.0):
                    ent_reg.async_remove(reg_entry.entity_id)
                    continue
            if passive and domain in ("number", "select", "switch", "button"):
                ent_reg.async_remove(reg_entry.entity_id)
                continue
            if domain == "sensor" and uid.endswith(unnamed_sensor_suffixes):
                orig_name = getattr(reg_entry, "original_name", None)
                if orig_name in (None, "", entry.title, device.name):
                    ent_reg.async_remove(reg_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AC Infinity config entry cleanly with zero leaked resources."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data: ACInfinityData = hass.data[DOMAIN].pop(entry.entry_id)
        await data.device.stop()
    return unload_ok

