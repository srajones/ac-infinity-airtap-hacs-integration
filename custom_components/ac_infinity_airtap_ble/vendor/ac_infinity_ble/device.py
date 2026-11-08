from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
)

from .const import (
    DEFAULT_MTU,
    MANUFACTURER_ID,
    POSSIBLE_READ_CHARACTERISTIC_UUIDS,
    POSSIBLE_WRITE_CHARACTERISTIC_UUIDS,
    TARGET_MTU,
    TELINK_DEVICE_TYPES,
    TELINK_OTA_FORBIDDEN_HANDLES,
    TELINK_OTA_FORBIDDEN_UUIDS,
    TELINK_SETTLE_DELAY,
    CallbackType,
)
from .exceptions import BLEStepError, CharacteristicMissingError
from .models import DeviceInfo
from .protocol import Protocol, is_command_header, parse_manufacturer_data
from .util import crc16, get_bit, get_bits, get_short

BLEAK_BACKOFF_TIME = 0.25
DISCONNECT_DELAY = 120
DEFAULT_ATTEMPTS = 3
GATT_TIMEOUT = 10
RESPONSE_TIMEOUT = 8
DISCONNECT_TIMEOUT = 5
DIAGNOSTIC_TIMEOUT = 2
PAIRING_TIMEOUT = 10
STANDARD_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"
REVISION_CHARACTERISTICS = {
    "firmware_revision": "00002a26" + STANDARD_UUID_SUFFIX,
    "hardware_revision": "00002a27" + STANDARD_UUID_SUFFIX,
    "software_revision": "00002a28" + STANDARD_UUID_SUFFIX,
    "pnp_id": "00002a50" + STANDARD_UUID_SUFFIX,
}
DIAGNOSTIC_READ_UUIDS = frozenset(REVISION_CHARACTERISTICS.values()) | {
    "70d51002-2c7f-4e75-ae8a-d758951ce4e0",
}

_LOGGER = logging.getLogger(__name__)


def validate_not_ota_endpoint(target: BleakGATTCharacteristic | int | str | None) -> None:
    """Strict Telink OTA guard: immediately raise ValueError before any I/O."""
    if target is None:
        return
    if isinstance(target, int):
        if target in TELINK_OTA_FORBIDDEN_HANDLES:
            raise ValueError(f"Access to Telink OTA handle {target} is strictly forbidden (not on allowlist)")
    elif isinstance(target, str):
        if target.lower() in TELINK_OTA_FORBIDDEN_UUIDS:
            raise ValueError(f"Access to Telink OTA UUID {target} is strictly forbidden (not on allowlist)")
    else:
        handle = getattr(target, "handle", None)
        uuid = getattr(target, "uuid", None)
        if handle in TELINK_OTA_FORBIDDEN_HANDLES:
            raise ValueError(f"Access to Telink OTA handle {handle} is strictly forbidden (not on allowlist)")
        if uuid and str(uuid).lower() in TELINK_OTA_FORBIDDEN_UUIDS:
            raise ValueError(f"Access to Telink OTA UUID {uuid} is strictly forbidden (not on allowlist)")


class ACInfinityController:
    def __init__(
        self,
        ble_device: BLEDevice,
        state: DeviceInfo | None = None,
        advertisement_data: AdvertisementData | None = None,
        attempt_pairing: bool = False,
    ) -> None:
        """Init the ACInfinityController."""
        if not state and not advertisement_data:
            raise ValueError("Must provide either state or advertisement_data")

        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._attempt_pairing = attempt_pairing
        self._operation_lock = asyncio.Lock()
        self._state = state or parse_manufacturer_data(
            advertisement_data.manufacturer_data[MANUFACTURER_ID]  # type: ignore
        )
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._read_char: BleakGATTCharacteristic | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._client: BleakClientWithServiceCache | None = None
        self._protocol: Protocol = Protocol()
        self._expected_disconnect = False
        self.loop = asyncio.get_running_loop()
        self._callbacks: list[Callable[[DeviceInfo, CallbackType], None]] = []
        self._notify_future: asyncio.Future[bytearray] | None = None
        self._sequence = 1
        self._pending_sequence: int | None = None
        self._pending_command: int | None = None
        self._response_buffer = bytearray()
        self.last_command_success = True
        self._failure_streak = False
        self._connect_attempt: int = 0
        self._setup_diagnostics = False
        self._diagnostic_read_label: str | None = None
        self._device_information_attempted = False
        self._diagnostic_warning = False
        self._cccd_warning = False
        self._negotiated_mtu: int = DEFAULT_MTU
        self._ble_device_callback: Callable[[], BLEDevice | None] | None = None
        self._firmware_revision: str | None = None
        self._hardware_revision: str | None = None
        self._software_revision: str | None = None

    def set_ble_device_callback(self, callback: Callable[[], BLEDevice | None]) -> None:
        """Set callback to resolve fresh connectable BLEDevice before connection."""
        self._ble_device_callback = callback

    def _get_ble_device(self) -> BLEDevice:
        """Return freshest BLEDevice instance."""
        if self._ble_device_callback is not None:
            if fresh := self._ble_device_callback():
                self._ble_device = fresh
        return self._ble_device

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        info = parse_manufacturer_data(
            advertisement_data.manufacturer_data[MANUFACTURER_ID]
        )
        self._state = replace(
            self._state, **{k: v for k, v in asdict(info).items() if v is not None}
        )
        if self._state.fan:
            if self._state.level_off or 0 > self._state.fan:
                self._state.level_off = self._state.fan
            if self._state.level_on or 10 < self._state.fan:
                self._state.level_on = self._state.fan
        self._fire_callbacks(CallbackType.ADVERTISEMENT)

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the name of the device."""
        return self._state.name

    @property
    def is_on(self) -> bool:
        """Get whether the device is on."""
        return bool(self._state.work_type == 2 and self._state.fan)

    @property
    def speed(self) -> int:
        """Get the speed of the device."""
        return self._state.fan or 0

    @property
    def temperature(self) -> float:
        """Get the temperature of the device."""
        return self._state.tmp or 0

    @property
    def humidity(self) -> float:
        """Get the humidity of the device."""
        return self._state.hum or 0

    @property
    def vpd(self) -> float:
        """Get the vpd of the device."""
        return self._state.vpd or 0

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    @property
    def negotiated_mtu(self) -> int:
        """Get the negotiated ATT MTU."""
        return self._negotiated_mtu

    @property
    def attempt_pairing(self) -> bool:
        """Return whether pairing should be attempted on retry."""
        return self._attempt_pairing

    @attempt_pairing.setter
    def attempt_pairing(self, value: bool) -> None:
        self._attempt_pairing = bool(value)

    @property
    def state(self) -> DeviceInfo:
        """Return the state."""
        return self._state

    @property
    def sequence(self) -> int:
        """Increment and return the sequence number."""
        if self._sequence == 65535:
            self._sequence = 0
        self._sequence += 1
        return self._sequence

    async def update(self) -> None:
        """Update the controller."""
        await self._ensure_connected()
        _LOGGER.debug("%s: Updating", self.name)
        command = self._protocol.get_model_data(self._state.type, 0, self.sequence)
        if data := await self._send_command(command):
            self._state.work_type = data[12]
            self._state.level_off = data[15]
            self._state.level_on = data[18]
            if self._state.work_type == 1:
                self._state.fan = self._state.level_off
            if self._state.work_type == 2:
                self._state.fan = self._state.level_on
            self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        await self._execute_disconnect()

    async def turn_on(self, speed: int | None = None) -> None:
        """Turn on the controller."""
        await self._ensure_connected()
        _LOGGER.debug("%s: Turn on", self.name)
        self._state.work_type = 2
        if speed is not None:
            self._state.fan = speed
            self._state.level_on = speed
        else:
            self._state.fan = self._state.level_on or 10
            self._state.level_on = self._state.fan

        command = self._protocol.set_level(
            self._state.type, 2, self._state.level_on, 0, self.sequence
        )
        await self._send_command(command)
        await self._execute_disconnect()

    async def turn_off(self) -> None:
        """Turn off the controller."""
        await self._ensure_connected()
        _LOGGER.debug("%s: Turn off", self.name)
        self._state.work_type = 1
        self._state.fan = self._state.level_off or 0
        self._state.level_off = self._state.fan
        command = self._protocol.set_level(
            self._state.type, 1, self._state.level_off, 0, self.sequence
        )
        await self._send_command(command)
        await self._execute_disconnect()

    async def set_speed(self, speed: int) -> None:
        """Set the speed of the controller."""
        await self._ensure_connected()
        _LOGGER.debug("%s: Set speed to %s", self.name, speed)
        self._state.work_type = 2 if speed > 0 else 1
        self.state.fan = speed
        if self._state.work_type == 1:
            self._state.level_off = speed
        else:
            self._state.level_on = speed
        command = self._protocol.set_level(
            self._state.type, self._state.work_type, speed, 0, self.sequence
        )
        await self._send_command(command)
        await self._execute_disconnect()

    async def stop(self) -> None:
        """Stop the controller."""
        _LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    def _fire_callbacks(self, type: CallbackType) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(self._state, type)

    def register_callback(
        self, callback: Callable[[DeviceInfo, CallbackType], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def _record_failure(self, error: BLEStepError) -> None:
        self.last_command_success = False
        _LOGGER.debug("%s [%s]: BLE %s failed (%s)", self.name, self.address,
                      error.step, error.code, exc_info=True)
        if not self._failure_streak:
            _LOGGER.warning("%s [%s]: BLE %s failed: %s; enable debug logging for details",
                            self.name, self.address, error.step, error)
            self._failure_streak = True
        self._fire_callbacks(CallbackType.AVAILABILITY)

    def _record_success(self) -> None:
        if self._failure_streak:
            _LOGGER.info("%s [%s]: BLE communication recovered", self.name, self.address)
        self._failure_streak = False
        self.last_command_success = True
        self._connect_attempt = 0
        self._fire_callbacks(CallbackType.AVAILABILITY)

    async def _ensure_connected(self, attempt: int = 1) -> None:
        """Connect, resolve a supported GATT pair, and subscribe, or release the slot."""
        async with self._connect_lock:
            if self._client and self._client.is_connected and self._read_char:
                self._reset_disconnect_timer()
                return
            client = None
            step, code = "connect", "connect_failed"
            try:
                _LOGGER.debug("%s [%s]: BLE connect; RSSI=%s", self.name, self.address, self.rssi)
                ble_device = self._get_ble_device()
                async with asyncio.timeout(45):
                    client = await establish_connection(
                        BleakClientWithServiceCache, ble_device, self.name,
                        self._disconnected, max_attempts=DEFAULT_ATTEMPTS,
                        use_services_cache=True, ble_device_callback=self._get_ble_device,
                    )
                self._client = client
                if self._attempt_pairing and (attempt >= 2 or self._connect_attempt >= 1 or self._failure_streak):
                    await self._pair_client(client, attempt)
                    if not client.is_connected:
                        raise BLEStepError("pairing", "connect_failed", "Device disconnected during pairing attempt")

                step, code = "mtu negotiation", "connect_failed"
                await self._negotiate_mtu(client)

                step, code = "service discovery", "service_failed"
                if not self._resolve_characteristics(client.services):
                    raise BLEStepError(step, "unsupported_model", "No supported notify/write characteristic pair")
                if self._setup_diagnostics:
                    self._log_connection_context(client)
                    await self._read_device_information(client)
                step, code = "notification subscription", "notify_failed"
                _LOGGER.debug("%s [%s]: BLE subscribe uuid=%s handle=%s",
                              self.name, self.address, self._read_char.uuid, self._read_char.handle)
                def notification_handler(sender, data):
                    if self._client is client:
                        self._notification_handler(sender, data)
                    else:
                        _LOGGER.debug("%s: Ignoring notification from previous BLE session: %s",
                                      self.name, data.hex())

                async with asyncio.timeout(GATT_TIMEOUT):
                    await client.start_notify(self._read_char, notification_handler)
                _LOGGER.debug("%s: BLE start_notify completed uuid=%s", self.name, self._read_char.uuid)
                if self._state.type in TELINK_DEVICE_TYPES:
                    step, code = "Telink readiness", "connect_failed"
                    _LOGGER.debug("%s: BLE Telink readiness wait start: type=%s delay=%.1fs "
                                  "strictly post-CCCD ACK (Android 2.1.2 protocol conformance)",
                                  self.name, self._state.type, TELINK_SETTLE_DELAY)
                    started = self.loop.time()
                    await asyncio.sleep(TELINK_SETTLE_DELAY)
                    if self._client is not client or not client.is_connected:
                        raise BLEStepError(step, code, "Device disconnected during Telink settle delay")
                    _LOGGER.debug("%s: BLE Telink readiness wait completed elapsed=%.3fs; "
                                  "clock initialization may begin", self.name, self.loop.time() - started)
                    step, code = "clock sync", "initialization_failed"
                    await self._initialize_telink_session()
                self._reset_disconnect_timer()
            except BaseException as err:
                if self._disconnect_timer:
                    self._disconnect_timer.cancel()
                    self._disconnect_timer = None
                self._client = None
                self._read_char = self._write_char = None
                if client is not None:
                    await self._safe_disconnect_client(client)
                if isinstance(err, asyncio.CancelledError):
                    raise
                if not isinstance(err, Exception):
                    raise
                error = err if isinstance(err, BLEStepError) else BLEStepError(step, code, repr(err))
                self._record_failure(error)
                if error is err:
                    raise
                raise error from err

    async def _pair_client(self, client: Any, attempt: int | None = None) -> bool:
        """Attempt BLE Just Works pairing on retry; gracefully handle non-supporting platforms/proxies."""
        pair_fn = getattr(client, "pair", None)
        if pair_fn is None or not callable(pair_fn):
            _LOGGER.debug("%s [%s]: BLE client has no pair() method; skipping pairing",
                          self.name, self.address)
            return False

        if attempt is not None:
            _LOGGER.info("%s [%s]: Attempting BLE Just Works pairing on retry (attempt %s)",
                         self.name, self.address, attempt)
        else:
            _LOGGER.info("%s [%s]: Attempting BLE Just Works pairing fallback",
                         self.name, self.address)

        try:
            async with asyncio.timeout(PAIRING_TIMEOUT):
                res = pair_fn()
                if inspect.isawaitable(res):
                    await res
            _LOGGER.info("%s [%s]: BLE pairing succeeded or already paired", self.name, self.address)
            return True
        except NotImplementedError as err:
            _LOGGER.debug("%s [%s]: BLE pairing not implemented on this platform/proxy: %s; continuing without pairing",
                          self.name, self.address, err)
            return False
        except (BleakError, OSError, TimeoutError, Exception) as err:
            _LOGGER.debug("%s [%s]: BLE optional pair() attempt finished/skipped: %r; continuing session",
                          self.name, self.address, err)
            return False

    def _get_clock_time(self) -> datetime:
        """Return aware local time; HA overrides this with its configured timezone."""
        return datetime.now().astimezone()

    async def _negotiate_mtu(self, client) -> int:
        """Negotiate ATT MTU 247 where supported by client/backend; handle MTU constraints gracefully."""
        if hasattr(client, "request_mtu"):
            try:
                res = client.request_mtu(TARGET_MTU)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as err:
                _LOGGER.debug("%s: client.request_mtu(%d) failed: %s", self.name, TARGET_MTU, err)
        elif hasattr(getattr(client, "_backend", None), "request_mtu"):
            try:
                res = client._backend.request_mtu(TARGET_MTU)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as err:
                _LOGGER.debug("%s: backend request_mtu(%d) failed: %s", self.name, TARGET_MTU, err)
        mtu = getattr(client, "mtu_size", DEFAULT_MTU)
        try:
            if callable(mtu):
                mtu = mtu()
        except Exception:
            mtu = DEFAULT_MTU
        self._negotiated_mtu = int(mtu) if isinstance(mtu, (int, float)) else DEFAULT_MTU
        char_max_write = getattr(self._write_char, "max_write_without_response_size", None)
        try:
            if callable(char_max_write):
                char_max_write = char_max_write()
        except Exception:
            char_max_write = None
        if isinstance(char_max_write, (int, float)) and char_max_write > 0:
            char_mtu = int(char_max_write) + 3
            if char_mtu > self._negotiated_mtu:
                _LOGGER.debug(
                    "%s [%s]: ATT MTU updated to %d bytes from characteristic max_write_without_response_size (%d)",
                    self.name, self.address, char_mtu, char_max_write,
                )
                self._negotiated_mtu = char_mtu
        if self._negotiated_mtu >= TARGET_MTU:
            _LOGGER.debug("%s [%s]: ATT MTU negotiated to %d bytes", self.name, self.address, self._negotiated_mtu)
        else:
            _LOGGER.debug("%s [%s]: ATT MTU is %d bytes (target %d); handling MTU constraints gracefully",
                          self.name, self.address, self._negotiated_mtu, TARGET_MTU)
        return self._negotiated_mtu

    async def _initialize_telink_session(self) -> None:
        """Clock-sync only; never provision preferences or change fan/mode settings."""
        validate_not_ota_endpoint(self._write_char)
        validate_not_ota_endpoint(self._read_char)
        if (self._write_char.uuid.lower() != POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0].lower()
                or self._read_char.uuid.lower() != POSSIBLE_READ_CHARACTERISTIC_UUIDS[0].lower()
                or "write" not in self._write_char.properties):
            raise BLEStepError("clock sync write", "unsupported_model",
                               f"Type-{self._state.type} app initialization requires the 70d51001/02 acknowledged pair")
        # Android constructs new connection state with sequence=-1; allocation
        # increments first, so clock is 0, then the first read is 1.
        self._sequence = -1
        try:
            now = self._get_clock_time()
            command = self._protocol.set_clock_data(now, self.sequence)
        except (ValueError, TypeError, OverflowError) as err:
            raise BLEStepError("clock sync calendar", "initialization_failed", repr(err)) from err
        _LOGGER.debug("%s: BLE clock sync start type=6 sequence=0 local_time=%s zone=%s; "
                      "clock only, no fan/mode changes", self.name, now.isoformat(), now.tzname())
        _LOGGER.debug("%s: Android requests MTU 247; using one public acknowledged write. "
                      "BlueZ mtu_size=23 can be a placeholder, not the on-air MTU; "
                      "pairing fallback on retry if needed", self.name)
        try:
            # Do NOT recurse into _send_command/_ensure_connected: connect lock
            # is held. This exchange still uses the operation lock and matching
            # sequence/command/CRC validation and registers RX before TX.
            result = await self._send_command_while_connected(command)
        except BLEStepError as err:
            if self._attempt_pairing and self._client and self._client.is_connected:
                _LOGGER.debug("%s: Initial BLE clock sync failed (%s); attempting pairing fallback",
                              self.name, err.step)
                if await self._pair_client(self._client):
                    if self._client and self._client.is_connected:
                        _LOGGER.info("%s: Retrying BLE clock sync after pairing", self.name)
                        try:
                            result = await self._send_command_while_connected(command)
                        except BLEStepError as retry_err:
                            if retry_err.step.startswith("clock sync"):
                                raise
                            raise BLEStepError(f"clock sync / {retry_err.step}", retry_err.code, str(retry_err)) from retry_err
                    else:
                        raise
                else:
                    if err.step.startswith("clock sync"):
                        raise
                    raise BLEStepError(f"clock sync / {err.step}", err.code, str(err)) from err
            else:
                if err.step.startswith("clock sync"):
                    raise
                raise BLEStepError(f"clock sync / {err.step}", err.code, str(err)) from err
        payload = result[10:-2]
        _LOGGER.debug("%s: BLE clock sync reply sequence=0 payload=%s", self.name, payload.hex())
        if payload != b"\x01\x00":
            raise BLEStepError("clock sync acknowledgement", "initialization_failed",
                               f"Expected explicit parameter 01 success 00; received {payload.hex()}")
        if self._client is None or not self._client.is_connected:
            raise BLEStepError("clock sync completion", "connect_failed", "Disconnected after clock reply")
        _LOGGER.debug("%s: BLE clock sync acknowledged; session initialized, next sequence=1; "
                      "model/speed reads still required for setup", self.name)

    def _log_diagnostic_failure(self, label: str, error: Exception) -> None:
        """Optional diagnostics never claim a query succeeded."""
        _LOGGER.debug("%s: BLE diagnostic %s failed: %r", self.name, label, error, exc_info=True)
        if not self._diagnostic_warning:
            _LOGGER.warning("%s: Optional BLE diagnostic %s unavailable: %s; setup will still query the vent",
                            self.name, label, error)
            self._diagnostic_warning = True

    def _log_connection_context(self, client) -> None:
        # This is the BLEDevice's discovery snapshot, NOT a live security query.
        details = getattr(self._ble_device, "details", None)
        props = details.get("props", {}) if isinstance(details, dict) else {}
        props = props if isinstance(props, dict) else {}
        context = {key: props[key] for key in
                   ("Paired", "Bonded", "Trusted", "Connected", "ServicesResolved", "AddressType")
                   if key in props}
        try:
            mtu = getattr(client, "mtu_size", "unavailable")
        except (BleakError, NotImplementedError, OSError) as err:
            self._log_diagnostic_failure("MTU context", err)
            mtu = "unavailable"
        _LOGGER.debug("%s: BLE connection context discovery_snapshot_not_live=%s mtu=%s "
                      "(pairing fallback enabled on retry)", self.name, context, mtu)

    async def _read_diagnostic_characteristic(self, client, characteristic, label: str) -> bytes | None:
        """Read ONLY allowlisted revision/PnP/control-reply values; never an OTA endpoint."""
        validate_not_ota_endpoint(characteristic)
        if characteristic.uuid.lower() not in DIAGNOSTIC_READ_UUIDS:
            raise ValueError("Characteristic is outside the diagnostic read allowlist")
        if "read" not in characteristic.properties:
            _LOGGER.debug("%s: BLE diagnostic %s skipped: no read property", self.name, label)
            return None
        previous_label = self._diagnostic_read_label
        self._diagnostic_read_label = label
        try:
            async with asyncio.timeout(DIAGNOSTIC_TIMEOUT):
                value = bytes(await client.read_gatt_char(characteristic))
            _LOGGER.debug("%s: BLE diagnostic read label=%s uuid=%s handle=%s bytes=%s ascii=%r",
                          self.name, label, characteristic.uuid, characteristic.handle,
                          value.hex(), value.decode("ascii", errors="backslashreplace"))
            return value
        except (BleakError, TimeoutError, OSError, EOFError, NotImplementedError, AttributeError) as err:
            self._log_diagnostic_failure(label, err)
            return None
        finally:
            self._diagnostic_read_label = previous_label

    async def _read_device_information(self, client) -> None:
        """Mirror the app's documented revision READS; do not subscribe to revision indications."""
        if self._device_information_attempted:
            return
        self._device_information_attempted = True
        for label, uuid in REVISION_CHARACTERISTICS.items():
            try:
                characteristic = client.services.get_characteristic(uuid)
            except (BleakError, NotImplementedError, AttributeError) as err:
                self._log_diagnostic_failure(f"{label} lookup", err)
                continue
            if characteristic is None:
                _LOGGER.debug("%s: BLE diagnostic %s absent", self.name, label)
                continue
            val = await self._read_diagnostic_characteristic(client, characteristic, label)
            if val is not None:
                try:
                    str_val = val.decode("utf-8", errors="replace").strip("\x00")
                    if label == "firmware_revision":
                        self._firmware_revision = str_val
                    elif label == "hardware_revision":
                        self._hardware_revision = str_val
                    elif label == "software_revision":
                        self._software_revision = str_val
                except Exception:
                    pass
            if not client.is_connected:
                raise BLEStepError("device information read", "connect_failed",
                                   "Device disconnected during optional diagnostic reads")

    async def _log_subscription_state(self, client, stage: str) -> None:
        """Observe the selected control CCCD; never write descriptors ourselves."""
        descriptors = getattr(self._read_char, "descriptors", ())
        cccd = next((descriptor for descriptor in descriptors
                     if descriptor.uuid.lower() == "00002902" + STANDARD_UUID_SUFFIX), None)
        if cccd is None:
            _LOGGER.debug("%s: BLE CCCD stage=%s descriptor unavailable; subscription unverified",
                          self.name, stage)
            return
        try:
            async with asyncio.timeout(DIAGNOSTIC_TIMEOUT):
                value = bytes(await client.read_gatt_descriptor(cccd.handle))
            bits = int.from_bytes(value, "little") if len(value) == 2 else None
            _LOGGER.debug("%s: BLE CCCD stage=%s handle=%s bytes=%s notifications_enabled=%s indications_enabled=%s",
                          self.name, stage, cccd.handle, value.hex(),
                          bool(bits & 1) if bits is not None else "unknown",
                          bool(bits & 2) if bits is not None else "unknown")
            if (stage == "after_subscribe" and bits is not None and not bits & 1
                    and not self._cccd_warning):
                self._cccd_warning = True
                _LOGGER.warning("%s: BLE CCCD readback does not show notifications enabled; "
                                "start_notify success alone is not confirmation", self.name)
        except (BleakError, TimeoutError, OSError, EOFError, NotImplementedError, AttributeError) as err:
            self._log_diagnostic_failure(f"CCCD {stage}", err)

    def _accept_command_notification(self, data: bytearray) -> bool:
        """Reassemble and validate A5 replies; telemetry cannot complete a request."""
        if not self._notify_future or self._notify_future.done() or data[:2] == b"\x1e\xff":
            return False
        if is_command_header(data):
            self._response_buffer.clear()
        elif not self._response_buffer:
            return False
        self._response_buffer.extend(data)
        packet = self._response_buffer
        if len(packet) < 8:
            return True
        length = int.from_bytes(packet[2:4], "big") + 12
        if not is_command_header(packet) or bytes(crc16(packet, 0, 6)) != packet[6:8] or length > 4096:
            _LOGGER.debug("%s: Ignoring invalid response header: %s", self.name, packet.hex())
            packet.clear()
            return True
        if len(packet) < length:
            return True
        frame = bytes(packet[:length])
        packet.clear()
        if (len(frame) != length or bytes(crc16(frame, 8, length - 10)) != frame[-2:]
                or (frame[9] != 0x66 and int.from_bytes(frame[4:6], "big") != self._pending_sequence)
                or frame[8] != 0
                or frame[9] not in ({3, 0x66} if self._pending_command == 3 else {self._pending_command})):
            _LOGGER.debug("%s: Ignoring unmatched/invalid response: %s", self.name, frame.hex())
            return True
        self._notify_future.set_result(bytearray(frame))
        return True

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        _LOGGER.debug("%s: BLE RX sender=%s bytes=%s", self.name, _sender, data.hex())
        if self._accept_command_notification(data):
            return

        if len(data) >= 18 and data[:2] == b"\x1e\xff":
            self._state.is_degree = get_bit(data[6], 0)
            self._state.tmp_state = get_bits(data[6], 1, 2)
            self._state.hum_state = get_bits(data[6], 3, 2)
            self._state.vpd_state = get_bits(data[6], 5, 2)
            self._state.choose_port = get_bits(data[7], 4, 4)
            self._state.tmp = get_short(data, 8) / 100
            self._state.hum = get_short(data, 10) / 100
            self._state.vpd = get_short(data, 12) / 100
            self._state.fan_type = get_short(data, 14)
            self._state.fan_state = get_bits(data[16], 0, 2)
            # self._state.fan = get_bits(data[17], 0, 4) # Not accurate
            self._state.work_type = get_bits(data[17], 4, 4)
            self._fire_callbacks(CallbackType.NOTIFICATION)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        self._disconnect_timer = self.loop.call_later(
            DISCONNECT_DELAY, self._disconnect
        )

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._client is not client or self._expected_disconnect:
            _LOGGER.debug(
                "%s: Disconnected from device; RSSI: %s", self.name, self.rssi
            )
            return
        if self._client is client:
            self._client = None
            self._read_char = None
            self._write_char = None
            self._response_buffer.clear()
            self._pending_sequence = None
            self._pending_command = None
            if self._notify_future and not self._notify_future.done():
                self._notify_future.set_exception(
                    BLEStepError("response wait", "connect_failed", "Device disconnected during command"))
        self._record_failure(BLEStepError("connection", "connect_failed", "Unexpected disconnect"))

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            DISCONNECT_DELAY,
        )
        await self._execute_disconnect()

    async def _safe_disconnect_client(self, client, read_char=None) -> None:
        """Best-effort bounded cleanup: a stop-notify failure must not leak a slot."""
        if read_char is not None and client.is_connected:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.stop_notify(read_char)
            except Exception as err:
                self._record_failure(BLEStepError("unsubscribe", "disconnect_failed", repr(err)))
        # Bleak disconnect is idempotent and also releases backend resources
        # after an unexpected link loss; do not skip it on is_connected=False.
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
        except Exception as err:
            self._record_failure(BLEStepError("disconnect", "disconnect_failed", repr(err)))

    async def _execute_disconnect(self) -> None:
        async with self._connect_lock:
            read_char, client = self._read_char, self._client
            self._expected_disconnect = True
            self._client = None
            self._read_char = self._write_char = None
            self._response_buffer.clear()
            self._pending_sequence = None
            self._pending_command = None
            if self._disconnect_timer:
                self._disconnect_timer.cancel()
                self._disconnect_timer = None
            if client is not None:
                await self._safe_disconnect_client(client, read_char)

    async def _send_command_locked(self, command: bytes) -> bytes:
        return await self._execute_command_locked(command)

    async def _send_command(
        self, command: bytes | Callable[[], bytes], retry: int | None = None
    ) -> bytes:
        """Retry read-only requests on a fresh session; never replay uncertain writes."""
        attempts = DEFAULT_ATTEMPTS
        try:
            for attempt in range(attempts):
                self._connect_attempt = attempt
                writing_started = False
                resolved_command = None
                try:
                    await self._ensure_connected(attempt=attempt + 1)
                    # Allocate sequence AFTER per-session initialization, and rebuild
                    # a read after reconnect rather than reusing its old session ID.
                    resolved_command = command() if callable(command) else command
                    writing_started = True
                    return await self._send_command_while_connected(resolved_command, retry)
                except BLEStepError as err:
                    is_query = bool(resolved_command and len(resolved_command) >= 10 and resolved_command[9] == 1)
                    if ((writing_started and not is_query)
                            or err.code in {"unsupported_model", "invalid_response", "initialization_failed"}
                            or attempt + 1 == attempts):
                        raise
                    _LOGGER.debug("%s: BLE attempt %s/%s failed at %s; reconnecting after %.2fs",
                                  self.name, attempt + 1, attempts, err.step,
                                  BLEAK_BACKOFF_TIME * (2 ** attempt), exc_info=True)
                    await self._execute_disconnect()
                    await asyncio.sleep(BLEAK_BACKOFF_TIME * (2 ** attempt))
            raise RuntimeError("Unreachable")
        finally:
            self._connect_attempt = 0

    async def _send_command_while_connected(
        self, command: bytes, retry: int | None = None
    ) -> bytes | None:
        """Send command to device and read response."""
        _LOGGER.debug(
            "%s: Sending command %s",
            self.name,
            command.hex(),
        )
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, waiting; RSSI: %s",
                self.name,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                return await self._send_command_locked(command)
            except BleakNotFoundError:
                _LOGGER.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.name,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except CharacteristicMissingError as ex:
                _LOGGER.debug(
                    "%s: characteristic missing: %s; RSSI: %s",
                    self.name,
                    ex,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                _LOGGER.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    async def _execute_command_locked(self, command: bytes) -> bytes:
        """Write according to advertised capabilities and await a matching reply."""
        if self._client is None or not self._read_char or not self._write_char:
            raise BLEStepError("write", "service_failed", "Missing active GATT pair")
        validate_not_ota_endpoint(self._write_char)
        if len(command) < 10:
            raise BLEStepError("write", "write_failed", f"Command payload too short ({len(command)} bytes; minimum 10)")
        # Diagnostics must finish before registering a pending request: a stored
        # value or unsolicited notification during that read is not a query reply.
        if self._setup_diagnostics and command[9] == 1 and self._state.type != 6:
            await self._read_diagnostic_characteristic(
                self._client, self._read_char, "control_reply_before_query")
            if self._client is None or not self._client.is_connected:
                raise BLEStepError("diagnostic read", "connect_failed",
                                   "Device disconnected before command write")
        self._notify_future = self.loop.create_future()
        self._pending_sequence = int.from_bytes(command[4:6], "big")
        self._pending_command = command[9]
        self._response_buffer.clear()
        is_clock = (self._state.type in TELINK_DEVICE_TYPES and len(command) == 21
                    and command[8:12] == b"\x00\x03\x01\x07")
        is_query = (command[9] == 1)
        is_setting = (command[9] == 3 and not is_clock)
        has_write = "write" in self._write_char.properties
        has_write_cmd = "write-without-response" in self._write_char.properties

        if is_clock:
            if not has_write:
                raise BLEStepError("clock sync write", "unsupported_model", "Clock requires acknowledged write")
            response = True
        elif is_query:
            response = True if has_write else False
        elif is_setting:
            # Operational fan speed and mode adjustments use ATT_WRITE_CMD (response=False)
            # when supported by characteristic, matching official Android app capture Event 18
            response = False if has_write_cmd else True
        else:
            response = True if has_write else False

        step, code = ("clock sync write" if is_clock else "write"), "write_failed"
        try:
            _LOGGER.debug("%s [%s]: BLE write uuid=%s handle=%s properties=%s response=%s bytes=%s",
                          self.name, self.address, self._write_char.uuid,
                          self._write_char.handle, self._write_char.properties, response, command.hex())
            async with asyncio.timeout(GATT_TIMEOUT):
                char_max_write = getattr(self._write_char, "max_write_without_response_size", None)
                try:
                    if callable(char_max_write):
                        char_max_write = char_max_write()
                except Exception:
                    char_max_write = None
                max_pdu = max(20, self._negotiated_mtu - 3)
                if isinstance(char_max_write, (int, float)) and char_max_write >= 20:
                    max_pdu = max(max_pdu, int(char_max_write))

                is_retry = getattr(self, "_connect_attempt", 0) >= 1
                should_split = len(command) > max_pdu or (is_clock and is_retry)
                chunk_size = min(max_pdu, 20) if should_split else len(command)

                chunks = [command[i:i + chunk_size] for i in range(0, len(command), chunk_size)]
                if is_clock:
                    if len(chunks) > 1:
                        _LOGGER.debug(
                            "%s: BLE clock sync split into %d chunks (chunk_size=%d, MTU=%d, max_pdu=%d, retry=%s)",
                            self.name, len(chunks), chunk_size, self._negotiated_mtu, max_pdu, is_retry,
                        )
                    else:
                        _LOGGER.debug(
                            "%s: BLE clock sync single acknowledged write length=%d (MTU=%d, max_pdu=%d)",
                            self.name, len(command), self._negotiated_mtu, max_pdu,
                        )

                for idx, chunk in enumerate(chunks):
                    if idx > 0:
                        await asyncio.sleep(0.02)
                    _LOGGER.debug("%s: BLE TX chunk %d/%d (%d bytes): %s",
                                  self.name, idx + 1, len(chunks), len(chunk), chunk.hex())
                    await self._client.write_gatt_char(self._write_char, chunk, response)
            _LOGGER.debug("%s: BLE write completed; awaiting notification sequence=%s command=%s",
                          self.name, self._pending_sequence, self._pending_command)
            step, code = ("clock sync response wait" if is_clock else "response wait"), "no_response"
            async with asyncio.timeout(RESPONSE_TIMEOUT):
                result = bytes(await self._notify_future)
            if not is_clock:
                self._record_success()
            return result
        except (BleakError, TimeoutError, EOFError, OSError) as err:
            error = err if isinstance(err, BLEStepError) else BLEStepError(step, code, repr(err))
            self._record_failure(error)
            if (self._setup_diagnostics and error.code == "no_response"
                    and (command[9] == 1 or is_clock)
                    and self._client is not None and self._client.is_connected
                    and self._read_char is not None):
                await self._log_subscription_state(self._client, "response_timeout")
                if self._state.type != 6:
                    await self._read_diagnostic_characteristic(
                        self._client, self._read_char, "control_reply_after_timeout_not_accepted_as_live")
            if error is err:
                raise
            raise error from err
        finally:
            if not self._notify_future.done():
                self._notify_future.cancel()
            elif not self._notify_future.cancelled():
                self._notify_future.exception()
            self._notify_future = None
            self._pending_sequence = self._pending_command = None
            self._response_buffer.clear()

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Log the complete service map; never mix independent protocol pairs."""
        self._read_char = self._write_char = None
        for service in services:
            _LOGGER.debug("%s [%s]: BLE service uuid=%s handle=%s",
                          self.name, self.address, service.uuid, service.handle)
            for char in service.characteristics:
                _LOGGER.debug("%s: BLE characteristic uuid=%s handle=%s service=%s properties=%s",
                              self.name, char.uuid, char.handle, service.uuid, char.properties)
                for descriptor in getattr(char, "descriptors", ()):
                    _LOGGER.debug("%s: BLE descriptor uuid=%s handle=%s characteristic=%s",
                                  self.name, descriptor.uuid, descriptor.handle, char.uuid)
        for read_uuid, write_uuid in zip(POSSIBLE_READ_CHARACTERISTIC_UUIDS,
                                         POSSIBLE_WRITE_CHARACTERISTIC_UUIDS, strict=True):
            read = services.get_characteristic(read_uuid)
            write = services.get_characteristic(write_uuid)
            if (read is not None and write is not None
                    and {"notify", "indicate"}.intersection(read.properties)
                    and {"write", "write-without-response"}.intersection(write.properties)):
                validate_not_ota_endpoint(read)
                validate_not_ota_endpoint(write)
                self._read_char, self._write_char = read, write
                _LOGGER.debug("%s: BLE selected notify=%s write=%s", self.name, read.uuid, write.uuid)
                char_max_write = getattr(self._write_char, "max_write_without_response_size", None)
                try:
                    if callable(char_max_write):
                        char_max_write = char_max_write()
                except Exception:
                    char_max_write = None
                if isinstance(char_max_write, (int, float)) and char_max_write > 0:
                    char_mtu = int(char_max_write) + 3
                    if char_mtu > self._negotiated_mtu:
                        _LOGGER.debug(
                            "%s [%s]: ATT MTU updated to %d bytes from write characteristic (max_write=%d)",
                            self.name, self.address, char_mtu, char_max_write,
                        )
                        self._negotiated_mtu = char_mtu
                return True
        return False
