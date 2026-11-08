from __future__ import annotations

import dataclasses
import logging
from typing import Any

from .vendor.ac_infinity_ble.const import MANUFACTURER_ID
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
try:
    from homeassistant.components.bluetooth import async_last_service_info
except ImportError:
    async_last_service_info = None  # type: ignore[assignment]
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA
from homeassistant.data_entry_flow import FlowResult

from .const import BLEAK_EXCEPTIONS, DOMAIN
from .errors import config_error
from .device import ACInfinityDevice, DeviceInfoEx
from .legacy import async_adopt_legacy_entry, async_find_legacy_entry

_LOGGER = logging.getLogger(__name__)


def parse_manufacturer_data(data: bytes) -> DeviceInfoEx:
    from .vendor.ac_infinity_ble.protocol import parse_manufacturer_data as parse
    return DeviceInfoEx.create(parse(data))


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        # Upgrading from 1.x: adopt the orphaned ac_infinity entry for this
        # device silently, so the domain rename costs the user nothing. No
        # BLE round-trip here — the vent may not be reachable at startup, and
        # the stored data is already known-good.
        if legacy := async_find_legacy_entry(self.hass, discovery_info.address):
            data = await async_adopt_legacy_entry(self.hass, legacy)
            return self.async_create_entry(title=legacy.title, data=data)

        self._discovery_info = discovery_info
        try:
            device = parse_manufacturer_data(
                discovery_info.advertisement.manufacturer_data[MANUFACTURER_ID]
            )
        except (ValueError, KeyError):
            _LOGGER.debug("%s: Invalid manufacturer data", discovery_info.address, exc_info=True)
            return self.async_abort(reason="unsupported_model")
        self.context["title_placeholders"] = {"name": device.name}
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()

            # Reached when the user adds the integration by hand rather than
            # via discovery; adopt here as well so a 1.x entry is never left
            # orphaned alongside a duplicate new one.
            if legacy := async_find_legacy_entry(self.hass, address):
                data = await async_adopt_legacy_entry(self.hass, legacy)
                return self.async_create_entry(title=legacy.title, data=data)

            controller = ACInfinityDevice(
                discovery_info.device, advertisement_data=discovery_info.advertisement
            )
            try:
                await controller.update()
            except (*BLEAK_EXCEPTIONS, ValueError, TimeoutError) as err:
                _LOGGER.warning(
                    "%s: Active GATT validation timed out or failed (%s); falling back to advertisement-backed "
                    "passive mode to guarantee ambient air temperature sensor availability",
                    address,
                    err,
                )
                if (
                    discovery_info.advertisement
                    and MANUFACTURER_ID in discovery_info.advertisement.manufacturer_data
                ):
                    try:
                        mfr_info = parse_manufacturer_data(
                            discovery_info.advertisement.manufacturer_data[MANUFACTURER_ID]
                        )
                        new_state = dataclasses.replace(
                            controller.state,
                            **{k: v for k, v in dataclasses.asdict(mfr_info).items() if v is not None and k != "fan"}
                        )
                        controller._state = new_state
                        try:
                            controller.state = new_state
                        except AttributeError:
                            pass
                    except Exception:
                        _LOGGER.debug("%s: Error parsing advertisement during fallback", address, exc_info=True)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"

            if hasattr(controller, "stop") and callable(controller.stop):
                await controller.stop()
            if not errors:
                return self.async_create_entry(
                    title=controller.name,
                    data={
                        CONF_ADDRESS: discovery_info.address,
                        CONF_SERVICE_DATA: dataclasses.asdict(controller.state),
                        "protocol_variant": controller.protocol_variant,
                    },
                )

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery.address in current_addresses
                    or discovery.address in self._discovered_devices
                ):
                    continue
                if MANUFACTURER_ID not in discovery.advertisement.manufacturer_data:
                    continue
                self._discovered_devices[discovery.address] = discovery

        # No AC Infinity (manufacturer 2306) devices advertising — fall back to
        # entering a BLE MAC address manually and connecting to it directly.
        if not self._discovered_devices:
            return await self.async_step_manual()

        _LOGGER.debug("Discovered devices: %s", self._discovered_devices)

        devices = {}
        for service_info in self._discovered_devices.values():
            try:
                device = parse_manufacturer_data(
                    service_info.advertisement.manufacturer_data[MANUFACTURER_ID]
                )
            except (ValueError, KeyError):
                _LOGGER.debug("%s: Ignoring invalid manufacturer data", service_info.address, exc_info=True)
                continue
            devices[service_info.address] = f"{device.name} ({service_info.address})"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(devices),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add a device by BLE MAC address without relying on a 2306 advert."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            # The AirTap does not reliably advertise its manufacturer data, so
            # users upgrading from 1.x may never hit the discovery path. Adopt
            # here too rather than making them delete and re-add.
            if legacy := async_find_legacy_entry(self.hass, address):
                data = await async_adopt_legacy_entry(self.hass, legacy)
                return self.async_create_entry(title=legacy.title, data=data)

            ble_device = async_ble_device_from_address(self.hass, address, True)
            if ble_device is None:
                errors["base"] = "connect_failed"
            else:
                last_service_info = (
                    async_last_service_info(self.hass, address, connectable=True)
                    if callable(async_last_service_info)
                    else None
                )
                advert_data = last_service_info.advertisement if last_service_info else None
                mfr_data = (
                    advert_data.manufacturer_data.get(MANUFACTURER_ID)
                    if advert_data and advert_data.manufacturer_data
                    else None
                )
                device_info = None
                if mfr_data:
                    try:
                        device_info = parse_manufacturer_data(mfr_data)
                    except Exception:
                        pass
                if device_info is None:
                    device_info = DeviceInfoEx(
                        type=6, name=ble_device.name or address, version=0
                    )
                controller = ACInfinityDevice(
                    ble_device, state=device_info, advertisement_data=advert_data
                )
                fallback_ok = False
                try:
                    await controller.update()
                except (*BLEAK_EXCEPTIONS, ValueError, TimeoutError) as err:
                    if mfr_data is not None:
                        _LOGGER.warning(
                            "%s: Manual active GATT validation timed out (%s); falling back to advertisement data",
                            address,
                            err,
                        )
                        fallback_ok = True
                    else:
                        _LOGGER.debug("%s: Manual setup failed: %s", address, err, exc_info=True)
                        errors["base"] = config_error(err)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected error")
                    errors["base"] = "unknown"
                else:
                    fallback_ok = True

                if hasattr(controller, "stop") and callable(controller.stop):
                    await controller.stop()
                if fallback_ok and "base" not in errors:
                    return self.async_create_entry(
                        title=controller.name or address,
                        data={
                            CONF_ADDRESS: address,
                            CONF_SERVICE_DATA: dataclasses.asdict(controller.state),
                            "protocol_variant": controller.protocol_variant,
                        },
                    )

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ADDRESS, default="A4:C1:38:41:4C:80"
                ): str,
            }
        )
        return self.async_show_form(
            step_id="manual",
            data_schema=data_schema,
            errors=errors,
        )
