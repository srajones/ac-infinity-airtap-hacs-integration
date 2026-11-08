import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import ACInfinityController
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.models import DeviceInfo
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.protocol import Protocol, parse_manufacturer_data
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import POSSIBLE_READ_CHARACTERISTIC_UUIDS as READ, POSSIBLE_WRITE_CHARACTERISTIC_UUIDS as WRITE
from custom_components.ac_infinity_airtap_ble.device import ACInfinityDevice, DeviceInfoEx


def controller(cls=ACInfinityController):
    return cls(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=DeviceInfo(type=6, name="D-8DAQJ", version=3))


def char(uuid, properties):
    return SimpleNamespace(uuid=uuid, properties=properties, handle=1, service_uuid="test")


@pytest.mark.asyncio
async def test_uses_acknowledged_write_when_supported_and_cleans_future():
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write"])
    async def write(characteristic, data, response):
        assert response is True
        dev._notification_handler(dev._read_char, bytearray(data))
    dev._client = SimpleNamespace(write_gatt_char=write)
    command = Protocol().get_model_data(6, 0, 2)
    assert await dev._execute_command_locked(command) == command
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_failed_write_cleans_pending_future():
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write-without-response"])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError):
        await dev._execute_command_locked(Protocol().get_model_data(6, 0, 2))
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_characteristics_must_be_a_matching_pair():
    dev = controller()
    chars = {READ[0]: char(READ[0], ["notify"]), WRITE[1]: char(WRITE[1], ["write"])}
    class Services:
        def __iter__(self):
            return iter(())
        def get_characteristic(self, uuid):
            return chars.get(uuid)
    services = Services()
    assert not dev._resolve_characteristics(services)


@pytest.mark.asyncio
async def test_reassembles_reply_and_ignores_wrong_sequence_and_crc():
    dev = controller(ACInfinityDevice)
    dev._notify_future = asyncio.get_running_loop().create_future()
    dev._pending_sequence = 2
    dev._pending_command = 1
    valid = Protocol()._add_head([0x10, 1, 2], 1, 2)
    wrong = Protocol()._add_head([0x10, 1, 2], 1, 3)
    dev._notification_handler(1, bytearray(wrong))
    assert not dev._notify_future.done()
    corrupt = bytearray(valid)
    corrupt[-1] ^= 1
    dev._notification_handler(1, corrupt)
    assert not dev._notify_future.done()
    dev._notification_handler(1, bytearray(valid[:8]))
    assert not dev._notify_future.done()
    dev._notification_handler(1, bytearray(valid[8:]))
    assert dev._notify_future.result() == valid


@pytest.mark.asyncio
async def test_converts_advertisement_state_to_extended_state():
    dev = controller(ACInfinityDevice)
    assert isinstance(dev.state, DeviceInfoEx)
    payload = bytes.fromhex("a4c138f648f6") + b"8DAQJ" + bytes.fromhex("03060006be0000090000000000000000")
    info = parse_manufacturer_data(payload)
    assert (info.type, info.version, info.name) == (6, 3, "D-8DAQJ")


@pytest.mark.asyncio
async def test_setup_requires_readable_mode_and_speeds_and_always_disconnects(monkeypatch):
    dev = controller(ACInfinityDevice)
    dev._ensure_connected = AsyncMock()
    dev._execute_disconnect = AsyncMock()
    dev._read_keys = AsyncMock(return_value={0x10: b"\x02", 0x11: b"\x00", 0x12: b"\x05"})
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    await dev.update()
    dev._read_keys.assert_awaited_once_with([0x10, 0x02, 0x03, 0x20])
    assert (dev.state.work_type, dev.state.level_on, dev.state.fan) == (2, 5, 5)
    dev._execute_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_rejects_empty_reply_and_disconnects():
    dev = controller(ACInfinityDevice)
    dev._ensure_connected = AsyncMock()
    dev._execute_disconnect = AsyncMock()
    dev._read_keys = AsyncMock(return_value={})
    with pytest.raises(Exception):
        await dev.update()
    dev._execute_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnected_callback_fails_response_wait_immediately():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    client = SimpleNamespace()
    dev._client = client
    dev._notify_future = asyncio.get_running_loop().create_future()
    dev._disconnected(client)
    with pytest.raises(BLEStepError):
        await dev._notify_future
    assert dev._client is None
    assert not dev.last_command_success


@pytest.mark.asyncio
async def test_stop_notify_failure_does_not_prevent_disconnect(caplog):
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    client = SimpleNamespace(is_connected=True, stop_notify=AsyncMock(side_effect=RuntimeError("bad stop")), disconnect=AsyncMock())
    dev._client = client
    await dev._execute_disconnect()
    client.disconnect.assert_awaited_once()
    assert dev._client is None
    assert dev._disconnect_timer is None
    assert "unsubscribe" in caplog.text


@pytest.mark.asyncio
async def test_subscribe_failure_disconnects_and_names_the_failed_step(monkeypatch):
    from bleak.exc import BleakError
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    client = SimpleNamespace(services=[], is_connected=True, start_notify=AsyncMock(side_effect=BleakError("subscription rejected")), disconnect=AsyncMock())
    monkeypatch.setattr(transport, "establish_connection", AsyncMock(return_value=client))
    def resolve(services):
        dev._read_char = char(READ[0], ["notify"])
        dev._write_char = char(WRITE[0], ["write"])
        return True
    monkeypatch.setattr(dev, "_resolve_characteristics", resolve)
    with pytest.raises(BLEStepError, match="notification subscription") as error:
        await dev._ensure_connected()
    assert error.value.code == "notify_failed"
    client.disconnect.assert_awaited_once()
    assert dev._client is None


@pytest.mark.asyncio
async def test_read_retries_with_backoff_but_setting_is_not_replayed(monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    dev._ensure_connected = AsyncMock()
    dev._execute_disconnect = AsyncMock()
    dev._send_command_while_connected = AsyncMock(side_effect=[BLEStepError("response wait", "no_response", "timeout"), b"ok"])
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert await dev._send_command(Protocol()._add_head([0x10], 1, 2)) == b"ok"
    assert dev._send_command_while_connected.await_count == 2
    sleep.assert_awaited_once_with(0.25)
    dev._send_command_while_connected = AsyncMock(side_effect=BLEStepError("response wait", "no_response", "timeout"))
    with pytest.raises(BLEStepError):
        await dev._send_command(Protocol()._add_head([0x10, 1, 2], 3, 2))
    assert dev._send_command_while_connected.await_count == 1


@pytest.mark.asyncio
async def test_warning_once_per_failure_streak(caplog):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    error = BLEStepError("write", "write_failed", "test")
    dev._record_failure(error)
    dev._record_failure(error)
    assert sum(record.levelname == "WARNING" for record in caplog.records) == 1
    dev._record_success()
    dev._record_failure(error)
    assert sum(record.levelname == "WARNING" for record in caplog.records) == 2


@pytest.mark.asyncio
async def test_response_timeout_is_specific_and_cleans_future(monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    monkeypatch.setattr(transport, "RESPONSE_TIMEOUT", 0.001)
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write-without-response"])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock())
    with pytest.raises(BLEStepError) as error:
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 2))
    assert error.value.code == "no_response"
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_truncated_tlv_is_rejected():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller(ACInfinityDevice)
    dev._send_command = AsyncMock(return_value=Protocol()._add_head([0x10, 5, 1], 1, 2))
    with pytest.raises(BLEStepError, match="Truncated TLV"):
        await dev._read_keys([0x10])


def test_config_errors_have_distinct_translation_keys():
    from custom_components.ac_infinity_airtap_ble.errors import config_error
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    for code in ("connect_failed", "no_response", "notify_failed", "service_failed", "write_failed", "unsupported_model", "invalid_response"):
        assert config_error(BLEStepError("test", code, "test")) == code
    assert config_error(TimeoutError()) == "no_response"
    assert config_error(ValueError()) == "unsupported_model"


def test_short_advertisement_rejected_with_clear_error():
    with pytest.raises(ValueError, match="manufacturer"):
        parse_manufacturer_data(b"short")


@pytest.mark.asyncio
async def test_old_session_notifications_and_disconnects_cannot_poison_reconnect(monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    dev = controller()
    dev._initialize_telink_session = AsyncMock()  # isolate stale-session routing from clock exchange
    callbacks = []
    async def start_notify(char, handler):
        callbacks.append(handler)
    first = SimpleNamespace(services=[], is_connected=True, start_notify=start_notify, stop_notify=AsyncMock(), disconnect=AsyncMock())
    second = SimpleNamespace(services=[], is_connected=True, start_notify=start_notify, stop_notify=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(transport, "establish_connection", AsyncMock(side_effect=[first, second]))
    def resolve(services):
        dev._read_char = char(READ[0], ["notify"])
        dev._write_char = char(WRITE[0], ["write"])
        return True
    monkeypatch.setattr(dev, "_resolve_characteristics", resolve)
    await dev._ensure_connected()
    await dev._execute_disconnect()
    await dev._ensure_connected()
    dev._notify_future = asyncio.get_running_loop().create_future()
    dev._pending_sequence, dev._pending_command = 2, 1
    reply = bytearray(Protocol()._add_head([0x10, 1, 2], 1, 2))
    callbacks[0](1, reply)
    assert not dev._notify_future.done()
    dev._disconnected(first)
    assert dev._client is second
    assert dev.last_command_success
    callbacks[1](1, reply)
    assert dev._notify_future.result() == reply
    await dev._execute_disconnect()


@pytest.mark.asyncio
async def test_unknown_device_model_rejected_before_ble_connect():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    with pytest.raises(BLEStepError, match="model"):
        ACInfinityDevice(SimpleNamespace(address="test"), state=DeviceInfo(type=255, name="unknown", version=0))


@pytest.mark.asyncio
async def test_setting_retries_transient_connect_before_any_write(monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    dev._ensure_connected = AsyncMock(side_effect=[BLEStepError("notification subscription", "notify_failed", "busy"), None])
    dev._execute_disconnect = AsyncMock()
    dev._send_command_while_connected = AsyncMock(return_value=b"ok")
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert await dev._send_command(Protocol()._add_head([0x10, 1, 2], 3, 2)) == b"ok"
    assert dev._ensure_connected.await_count == 2
    dev._send_command_while_connected.assert_awaited_once()


@pytest.mark.asyncio
async def test_backend_os_error_is_a_write_failure():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write"])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(side_effect=OSError("adapter gone")))
    with pytest.raises(BLEStepError) as error:
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 2))
    assert error.value.code == "write_failed"
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_long_command_split_into_minimum_mtu_chunks():
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write-without-response"])
    command = Protocol()._add_head(list(range(30)), 1, 2)
    chunks = []
    async def write(characteristic, data, response):
        assert response is False
        assert len(data) <= 20
        chunks.append(data)
        if len(b"".join(chunks)) == len(command):
            dev._notification_handler(1, bytearray(command))
    dev._client = SimpleNamespace(write_gatt_char=write)
    assert await dev._execute_command_locked(command) == command
    assert b"".join(chunks) == command


@pytest.mark.asyncio
async def test_cancelled_write_cleans_future_without_swallowing_cancellation():
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write"])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 2))
    assert dev._notify_future is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "service discovery"])
async def test_failed_connect_or_discovery_has_specific_error_and_cleanup(monkeypatch, stage):
    from bleak.exc import BleakError
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    client = SimpleNamespace(services=[], is_connected=True, disconnect=AsyncMock())
    connect = AsyncMock(side_effect=BleakError("unreachable")) if stage == "connect" else AsyncMock(return_value=client)
    monkeypatch.setattr(transport, "establish_connection", connect)
    if stage == "service discovery":
        monkeypatch.setattr(dev, "_resolve_characteristics", lambda services: (_ for _ in ()).throw(BleakError("discovery failed")))
    with pytest.raises(BLEStepError) as error:
        await dev._ensure_connected()
    assert error.value.code == ("connect_failed" if stage == "connect" else "service_failed")
    assert dev._client is None
    if stage != "connect":
        client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_mtu_negotiation_requests_247_and_handles_graceful_fallback():
    from bleak.exc import BleakError
    dev = controller()
    client = SimpleNamespace(request_mtu=AsyncMock(return_value=247), mtu_size=247)
    await dev._negotiate_mtu(client)
    client.request_mtu.assert_awaited_once_with(247)
    assert dev.negotiated_mtu == 247

    # Fallback when request_mtu raises NotImplementedError or BleakError
    client_err = SimpleNamespace(request_mtu=AsyncMock(side_effect=NotImplementedError), mtu_size=23)
    await dev._negotiate_mtu(client_err)
    assert dev.negotiated_mtu == 23

    client_bleak_err = SimpleNamespace(request_mtu=AsyncMock(side_effect=BleakError("unsupported")), mtu_size=23)
    await dev._negotiate_mtu(client_bleak_err)
    assert dev.negotiated_mtu == 23


@pytest.mark.asyncio
async def test_write_type_separation_settings_use_write_cmd_queries_use_write_req():
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = char(WRITE[0], ["write", "write-without-response"])

    calls = []
    async def write(characteristic, data, response):
        calls.append((bytes(data), response))
        dev._notification_handler(dev._read_char, bytearray(data))

    dev._client = SimpleNamespace(write_gatt_char=write)

    # 1. Query command (Opcode 1) -> ATT_WRITE_REQ (response=True)
    query_cmd = Protocol().get_model_data(6, 1, 2)
    assert query_cmd[9] == 1
    await dev._execute_command_locked(query_cmd)
    assert calls[-1][1] is True

    # 2. Operational setting command (Opcode 3, e.g. fan speed key 0x12) -> ATT_WRITE_CMD (response=False)
    setting_cmd = Protocol()._add_head([0x12, 1, 5], 3, 2)
    assert setting_cmd[9] == 3
    await dev._execute_command_locked(setting_cmd)
    assert calls[-1][1] is False

    # 3. Calendar clock sync command (Opcode 3, payload starting with 0x01, 0x07) -> ATT_WRITE_REQ (response=True)
    clock_cmd = Protocol()._add_head([0x01, 0x07, 0x1A, 0x0B, 0x07, 0x06, 0x13, 0x15, 0x21], 3, 0)
    await dev._execute_command_locked(clock_cmd)
    assert calls[-1][1] is True


@pytest.mark.asyncio
async def test_opcode_0x66_status_notification_accepted_for_command_3():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.util import crc16
    dev = controller()
    dev._pending_sequence = 2
    dev._pending_command = 3
    dev._notify_future = asyncio.get_running_loop().create_future()

    # Opcode 0x66 carries independent sequence number (e.g. 5 != 2)
    frame = bytearray(Protocol()._add_head([0x10, 1, 2], 3, 5))
    frame[9] = 0x66
    frame[-2:] = crc16(frame, 8, len(frame) - 10)

    # Notification handler should accept Opcode 0x66 and resolve the future
    dev._notification_handler(1, frame)
    assert dev._notify_future.done()
    assert dev._notify_future.result() == frame


@pytest.mark.asyncio
async def test_telink_ota_guard_rejects_handle_28_and_ota_uuids():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import validate_not_ota_endpoint

    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint(28)

    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint("00010203-0405-0607-0809-0a0b0c0d2b12")

    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint(SimpleNamespace(handle=28, uuid="some-uuid"))

    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint(SimpleNamespace(handle=99, uuid="00010203-0405-0607-0809-0a0b0c0d2b12"))

    # Test in _execute_command_locked
    dev = controller()
    dev._read_char = char(READ[0], ["notify"])
    dev._write_char = SimpleNamespace(handle=28, uuid="00010203-0405-0607-0809-0a0b0c0d2b12", properties=["write"])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock())

    with pytest.raises(ValueError, match="strictly forbidden"):
        await dev._execute_command_locked(Protocol().get_model_data(6, 1, 2))
    dev._client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_settle_delay_post_cccd_with_zero_intervening_reads(monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import TELINK_SETTLE_DELAY

    dev = controller()
    read_c = char(READ[0], ["notify"])
    write_c = char(WRITE[0], ["write"])
    client = SimpleNamespace(
        start_notify=AsyncMock(),
        read_gatt_char=AsyncMock(),
        write_gatt_char=AsyncMock(),
        is_connected=True,
        services=[],
        mtu_size=247,
    )
    events = []

    async def fake_connect(*args, **kwargs):
        events.append("connect")
        return client

    async def fake_start_notify(*args, **kwargs):
        events.append("start_notify")

    async def fake_sleep(duration):
        events.append(f"sleep_{duration}")

    async def fake_read(*args, **kwargs):
        events.append("read")

    async def fake_clock_sync():
        events.append("clock_sync")

    client.start_notify.side_effect = fake_start_notify
    client.read_gatt_char.side_effect = fake_read
    monkeypatch.setattr(transport, "establish_connection", AsyncMock(side_effect=fake_connect))
    monkeypatch.setattr(dev, "_resolve_characteristics", lambda s: setattr(dev, "_read_char", read_c) or setattr(dev, "_write_char", write_c) or True)
    monkeypatch.setattr(dev, "_read_device_information", AsyncMock(side_effect=fake_read))
    monkeypatch.setattr(dev, "_initialize_telink_session", AsyncMock(side_effect=fake_clock_sync))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await dev._ensure_connected()

    assert "start_notify" in events
    start_notify_idx = events.index("start_notify")
    sleep_idx = events.index(f"sleep_{TELINK_SETTLE_DELAY}")
    clock_idx = events.index("clock_sync")

    # Strict ordering: start_notify -> sleep 1.5s -> clock sync
    assert start_notify_idx < sleep_idx < clock_idx
    # Zero read calls occurred between start_notify and clock_sync
    events_between = events[start_notify_idx + 1:clock_idx]
    assert "read" not in events_between

