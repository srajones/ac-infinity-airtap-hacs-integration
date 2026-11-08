from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, replace

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
    MANUFACTURER_ID,
    POSSIBLE_READ_CHARACTERISTIC_UUIDS,
    POSSIBLE_WRITE_CHARACTERISTIC_UUIDS,
    CallbackType,
)
from .exceptions import BLEStepError, CharacteristicMissingError
from .models import DeviceInfo
from .protocol import Protocol, parse_manufacturer_data
from .util import crc16, get_bit, get_bits, get_short

BLEAK_BACKOFF_TIME = 0.25
DISCONNECT_DELAY = 120
DEFAULT_ATTEMPTS = 3
GATT_TIMEOUT = 10
RESPONSE_TIMEOUT = 8
DISCONNECT_TIMEOUT = 5

_LOGGER = logging.getLogger(__name__)


class ACInfinityController:
    def __init__(
        self,
        ble_device: BLEDevice,
        state: DeviceInfo | None = None,
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        """Init the ACInfinityController."""
        if not state and not advertisement_data:
            raise ValueError("Must provide either state or advertisement_data")

        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
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
        self._fire_callbacks(CallbackType.AVAILABILITY)

    async def _ensure_connected(self) -> None:
        """Connect, resolve a supported GATT pair, and subscribe, or release the slot."""
        async with self._connect_lock:
            if self._client and self._client.is_connected and self._read_char:
                self._reset_disconnect_timer()
                return
            client = None
            step, code = "connect", "connect_failed"
            try:
                _LOGGER.debug("%s [%s]: BLE connect; RSSI=%s", self.name, self.address, self.rssi)
                async with asyncio.timeout(45):
                    client = await establish_connection(
                        BleakClientWithServiceCache, self._ble_device, self.name,
                        self._disconnected, max_attempts=DEFAULT_ATTEMPTS,
                        use_services_cache=True, ble_device_callback=lambda: self._ble_device,
                    )
                step, code = "service discovery", "service_failed"
                if not self._resolve_characteristics(client.services):
                    raise BLEStepError(step, "unsupported_model", "No supported notify/write characteristic pair")
                self._client = client
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

    def _accept_command_notification(self, data: bytearray) -> bool:
        """Reassemble and validate A5 replies; telemetry cannot complete a request."""
        if not self._notify_future or self._notify_future.done() or data[:2] == b"\x1e\xff":
            return False
        if data[:2] == b"\xa5\x00":
            self._response_buffer.clear()
        elif not self._response_buffer:
            return False
        self._response_buffer.extend(data)
        packet = self._response_buffer
        if len(packet) < 8:
            return True
        length = int.from_bytes(packet[2:4], "big") + 12
        if packet[:2] != b"\xa5\x00" or bytes(crc16(packet, 0, 6)) != packet[6:8] or length > 4096:
            _LOGGER.debug("%s: Ignoring invalid response header: %s", self.name, packet.hex())
            packet.clear()
            return True
        if len(packet) < length:
            return True
        frame = bytes(packet[:length])
        packet.clear()
        if (len(frame) != length or bytes(crc16(frame, 8, length - 10)) != frame[-2:]
                or int.from_bytes(frame[4:6], "big") != self._pending_sequence
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
        if client.is_connected:
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
            if self._disconnect_timer:
                self._disconnect_timer.cancel()
                self._disconnect_timer = None
            if client is not None:
                await self._safe_disconnect_client(client, read_char)

    async def _send_command_locked(self, command: bytes) -> bytes:
        return await self._execute_command_locked(command)

    async def _send_command(self, command: bytes, retry: int | None = None) -> bytes:
        """Retry read-only requests on a fresh session; never replay uncertain writes."""
        attempts = DEFAULT_ATTEMPTS
        for attempt in range(attempts):
            writing_started = False
            try:
                await self._ensure_connected()
                writing_started = True
                return await self._send_command_while_connected(command, retry)
            except BLEStepError as err:
                if ((writing_started and command[9] != 1)
                        or err.code in {"unsupported_model", "invalid_response"}
                        or attempt + 1 == attempts):
                    raise
                _LOGGER.debug("%s: BLE attempt %s/%s failed at %s; reconnecting after %.2fs",
                              self.name, attempt + 1, attempts, err.step,
                              BLEAK_BACKOFF_TIME * (2 ** attempt), exc_info=True)
                await self._execute_disconnect()
                await asyncio.sleep(BLEAK_BACKOFF_TIME * (2 ** attempt))
        raise RuntimeError("Unreachable")

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
        self._notify_future = self.loop.create_future()
        self._pending_sequence = int.from_bytes(command[4:6], "big")
        self._pending_command = command[9]
        self._response_buffer.clear()
        # Match the official app's acknowledged write when the device supports it.
        response = "write" in self._write_char.properties
        step, code = "write", "write_failed"
        try:
            _LOGGER.debug("%s [%s]: BLE write uuid=%s handle=%s properties=%s response=%s bytes=%s",
                          self.name, self.address, self._write_char.uuid,
                          self._write_char.handle, self._write_char.properties, response, command.hex())
            async with asyncio.timeout(GATT_TIMEOUT):
                # Twenty bytes fits the minimum ATT MTU on every supported backend.
                for offset in range(0, len(command), 20):
                    chunk = command[offset:offset + 20]
                    _LOGGER.debug("%s: BLE TX chunk=%s", self.name, chunk.hex())
                    await self._client.write_gatt_char(self._write_char, chunk, response)
            step, code = "response wait", "no_response"
            async with asyncio.timeout(RESPONSE_TIMEOUT):
                result = bytes(await self._notify_future)
            self._record_success()
            return result
        except (BleakError, TimeoutError, EOFError, OSError) as err:
            error = err if isinstance(err, BLEStepError) else BLEStepError(step, code, repr(err))
            self._record_failure(error)
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
        for read_uuid, write_uuid in zip(POSSIBLE_READ_CHARACTERISTIC_UUIDS,
                                         POSSIBLE_WRITE_CHARACTERISTIC_UUIDS, strict=True):
            read = services.get_characteristic(read_uuid)
            write = services.get_characteristic(write_uuid)
            if (read is not None and write is not None
                    and {"notify", "indicate"}.intersection(read.properties)
                    and {"write", "write-without-response"}.intersection(write.properties)):
                self._read_char, self._write_char = read, write
                _LOGGER.debug("%s: BLE selected notify=%s write=%s", self.name, read.uuid, write.uuid)
                return True
        return False
