from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

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
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

try:
    from homeassistant.config_entries import OptionsFlow
except (ImportError, AttributeError):
    class OptionsFlow:  # type: ignore[no-redef]
        """Fallback OptionsFlow."""
        def __init__(self, config_entry: Any = None) -> None:
            self.config_entry = config_entry
        def async_show_form(self, **kwargs: Any) -> FlowResult:
            return kwargs
        def async_create_entry(self, **kwargs: Any) -> FlowResult:
            return kwargs

from .const import (
    AC_INFINITY_SERVICE_UUID,
    BLEAK_EXCEPTIONS,
    CONF_PASSIVE_ONLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_MODEL,
    DOMAIN,
    get_device_model_name,
)
from .device import ACInfinityDevice, DeviceInfoEx
from .errors import config_error
from .legacy import async_adopt_legacy_entry, async_find_legacy_entry
from .vendor.ac_infinity_ble.const import MANUFACTURER_ID

_LOGGER = logging.getLogger(__name__)

MAC_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")


def parse_manufacturer_data(data: bytes) -> DeviceInfoEx:
    from .vendor.ac_infinity_ble.protocol import parse_manufacturer_data as parse
    return DeviceInfoEx.create(parse(data))


def is_valid_ac_infinity_advertisement(
    service_info: BluetoothServiceInfoBleak | None,
) -> bool:
    """Verify that a BluetoothServiceInfoBleak represents a genuine AC Infinity advertisement.

    Zero accidental adds:
    1. Must have MANUFACTURER_ID (2306 / 0x0902) in advertisement.manufacturer_data.
    2. Validate manufacturer data payload length >= 19.
    3. Parse device with parse_manufacturer_data(raw_data).
    4. Verify device.type in DEVICE_MODEL (reject unknown/bogus types).
    5. Verify raw_data[6:11] (serial e.g. b"8DAQJ") is valid printable ASCII characters (c.isalnum()).
    6. Verify device.version is sane (1 <= device.version <= 20).
    7. Returns True only if all checks pass.
    """
    if service_info is None:
        return False
    advert = getattr(service_info, "advertisement", None)
    if advert is None:
        return False
    mfr_data = getattr(advert, "manufacturer_data", None)
    if not isinstance(mfr_data, dict) or MANUFACTURER_ID not in mfr_data:
        return False
    raw_data = mfr_data[MANUFACTURER_ID]
    if not isinstance(raw_data, (bytes, bytearray)):
        return False
    raw_bytes = bytes(raw_data)
    if len(raw_bytes) < 19:
        return False
    serial_slice = raw_bytes[6:11]
    try:
        serial_str = serial_slice.decode("ascii")
    except UnicodeDecodeError:
        return False
    if not serial_str or not all(c.isalnum() for c in serial_str):
        return False
    try:
        device = parse_manufacturer_data(raw_bytes)
    except Exception:
        return False
    if device.type not in DEVICE_MODEL:
        return False
    if not (1 <= device.version <= 20):
        return False
    return True


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

        if not is_valid_ac_infinity_advertisement(discovery_info):
            _LOGGER.debug(
                "%s: Discovery advertisement rejected — not a valid AC Infinity device",
                discovery_info.address,
            )
            return self.async_abort(reason="unsupported_model")

        self._discovery_info = discovery_info
        try:
            device = parse_manufacturer_data(
                discovery_info.advertisement.manufacturer_data[MANUFACTURER_ID]
            )
        except Exception:
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
            selected = user_input[CONF_ADDRESS]
            if selected == "__manual__":
                return await self.async_step_manual()

            if selected == "__all__":
                valid_devices_list = [
                    s for s in self._discovered_devices.values()
                    if is_valid_ac_infinity_advertisement(s)
                ]
                if not valid_devices_list:
                    valid_devices_list = list(self._discovered_devices.values())
                if not valid_devices_list:
                    return await self.async_step_manual()

                first_device = valid_devices_list[0]
                remaining_devices = valid_devices_list[1:]

                if hasattr(self.hass, "async_create_task") and hasattr(self.hass, "config_entries"):
                    source = getattr(config_entries, "SOURCE_BLUETOOTH", "bluetooth")
                    for remaining in remaining_devices:
                        self.hass.async_create_task(
                            self.hass.config_entries.flow.async_init(
                                DOMAIN,
                                context={"source": source},
                                data=remaining,
                            )
                        )

                discovery_info = first_device
                address = first_device.address
            else:
                address = selected
                discovery_info = self._discovered_devices.get(address)
                if discovery_info is None:
                    ble_dev = async_ble_device_from_address(self.hass, address, True)
                    if ble_dev is None:
                        errors["base"] = "cannot_connect"
                        return await self.async_step_manual(user_input)

            if discovery_info is not None:
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
                        and hasattr(discovery_info.advertisement, "manufacturer_data")
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

        # Enumerate discovered devices
        discovered: dict[str, BluetoothServiceInfoBleak] = {}
        if self._discovery_info and is_valid_ac_infinity_advertisement(self._discovery_info):
            discovered[self._discovery_info.address] = self._discovery_info

        current_addresses = self._async_current_ids()
        try:
            available_discoveries = async_discovered_service_info(self.hass, connectable=True)
        except TypeError:
            available_discoveries = async_discovered_service_info(self.hass)

        for discovery in available_discoveries:
            if discovery.address in current_addresses:
                continue
            if discovery.address not in discovered and is_valid_ac_infinity_advertisement(discovery):
                discovered[discovery.address] = discovery

        self._discovered_devices.update(discovered)

        # Build valid discovered devices dict
        valid_devices: dict[str, tuple[BluetoothServiceInfoBleak, DeviceInfoEx]] = {}
        for addr, service_info in list(self._discovered_devices.items()):
            if addr in current_addresses:
                continue
            if is_valid_ac_infinity_advertisement(service_info):
                try:
                    dev_info = parse_manufacturer_data(
                        service_info.advertisement.manufacturer_data[MANUFACTURER_ID]
                    )
                    valid_devices[addr] = (service_info, dev_info)
                except Exception:
                    pass

        # If no valid AC Infinity devices discovered, fall back cleanly to manual MAC entry
        if not valid_devices:
            _LOGGER.debug("No valid AC Infinity devices discovered; falling back to manual entry")
            return await self.async_step_manual()

        _LOGGER.debug("Valid discovered AC Infinity devices: %s", list(valid_devices.keys()))

        options: dict[str, str] = {}
        if len(valid_devices) >= 2:
            options["__all__"] = f"Add all discovered AC Infinity devices ({len(valid_devices)})"

        for addr, (service_info, dev_info) in valid_devices.items():
            dev_name = (
                service_info.name
                if getattr(service_info, "name", None) and service_info.name != addr
                else dev_info.name
            )
            model_name = get_device_model_name(dev_info.type)
            options[addr] = f"{dev_name} ({addr}) — {model_name}"

        options["__manual__"] = "Enter Bluetooth MAC address manually..."

        default_selection: str
        if self._discovery_info and self._discovery_info.address in valid_devices:
            default_selection = self._discovery_info.address
        elif valid_devices:
            default_selection = next(iter(valid_devices.keys()))
        else:
            default_selection = "__manual__"

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default=default_selection): vol.In(options),
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
        """Add a device by BLE MAC address with strict verification."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_address = user_input.get(CONF_ADDRESS, "").strip()
            if not MAC_REGEX.match(raw_address):
                errors["base"] = "invalid_address"
                return self.async_show_form(
                    step_id="manual",
                    data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
                    errors=errors,
                )

            address = raw_address.replace("-", ":").upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            # Upgrades from 1.x
            if legacy := async_find_legacy_entry(self.hass, address):
                data = await async_adopt_legacy_entry(self.hass, legacy)
                return self.async_create_entry(title=legacy.title, data=data)

            last_service_info = (
                async_last_service_info(self.hass, address, connectable=True)
                if callable(async_last_service_info)
                else None
            )

            # If cached advertisement exists, verify it
            if last_service_info is not None:
                has_ac_uuid = False
                advert = getattr(last_service_info, "advertisement", None)
                if advert:
                    service_uuids = [
                        u.lower() for u in getattr(advert, "service_uuids", []) or []
                    ]
                    has_ac_uuid = AC_INFINITY_SERVICE_UUID.lower() in service_uuids

                is_valid_mfr = is_valid_ac_infinity_advertisement(last_service_info)
                if not is_valid_mfr and not has_ac_uuid:
                    _LOGGER.warning(
                        "%s: Device rejected — advertisement is not an AC Infinity device",
                        address,
                    )
                    errors["base"] = "not_ac_infinity_device"
                    return self.async_show_form(
                        step_id="manual",
                        data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
                        errors=errors,
                    )

            ble_device = async_ble_device_from_address(self.hass, address, True)
            if ble_device is None:
                errors["base"] = "connect_failed"
            else:
                advert_data = last_service_info.advertisement if last_service_info else None
                mfr_data = (
                    advert_data.manufacturer_data.get(MANUFACTURER_ID)
                    if advert_data and getattr(advert_data, "manufacturer_data", None)
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
                vol.Required(CONF_ADDRESS): str,
            }
        )
        return self.async_show_form(
            step_id="manual",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ACInfinityOptionsFlowHandler:
        """Get the options flow for this handler."""
        return ACInfinityOptionsFlowHandler(config_entry)


class ACInfinityOptionsFlowHandler(OptionsFlow):
    """Handle options flow for AC Infinity integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options if self.config_entry else {}
        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                vol.Optional(
                    CONF_PASSIVE_ONLY,
                    default=options.get(CONF_PASSIVE_ONLY, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)
