"""Protocol facts from David's APK, tested with fake BLE (not hardware)."""
import asyncio
import binascii
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from custom_components.ac_infinity_airtap_ble.device import ACInfinityDevice
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.models import DeviceInfo
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.protocol import Protocol

PROFILE = 'android_2_1_2_telink'


def device(**kwargs):
    return ACInfinityDevice(SimpleNamespace(address='A4:C1:38:F6:48:F6'),
                            state=DeviceInfo(type=6, version=3, name='test'), **kwargs)


def connected_device(monkeypatch):
    import custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device as transport
    dev = device()
    dev._read_char = SimpleNamespace(uuid='read', handle=16, properties=['notify'])
    dev._write_char = SimpleNamespace(uuid='write', handle=20, properties=['write'])
    dev._resolve_characteristics = lambda _: True
    client = SimpleNamespace(is_connected=True, services=object(), start_notify=AsyncMock(),
        stop_notify=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(transport, 'establish_connection', AsyncMock(return_value=client))
    return dev, client


@pytest.mark.asyncio
async def test_telink_waits_1500ms_after_subscribe_before_returning_ready(monkeypatch):
    dev, client = connected_device(monkeypatch)
    events = []
    async def subscribed(*args): events.append('subscribed')
    async def settle(seconds):
        assert events == ['subscribed']
        assert seconds == 1.5
        events.append('settled')
    client.start_notify.side_effect = subscribed
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock(side_effect=settle))
    await dev._ensure_connected()
    assert events == ['subscribed', 'settled']
    await dev._ensure_connected()  # same ready session must not wait twice
    assert events == ['subscribed', 'settled']
    await dev.stop()


@pytest.mark.asyncio
async def test_cancelled_telink_wait_disconnects_and_propagates(monkeypatch):
    dev, client = connected_device(monkeypatch)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await dev._ensure_connected()
    client.disconnect.assert_awaited_once()
    assert dev._client is None


@pytest.mark.asyncio
async def test_link_lost_during_telink_wait_has_specific_error(monkeypatch):
    dev, client = connected_device(monkeypatch)
    async def lost(_): client.is_connected = False
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock(side_effect=lost))
    with pytest.raises(BLEStepError) as error:
        await dev._ensure_connected()
    assert error.value.step == 'Telink readiness'
    assert error.value.code == 'connect_failed'
    client.disconnect.assert_awaited_once()
    assert dev._client is None


@pytest.mark.asyncio
async def test_app_query_sequence_zero_then_speed_read_without_clock_or_probe_variants(monkeypatch):
    dev = device()
    sent = []
    async def send(command, retry=None):
        sent.append(command)
        assert retry is None
        assert command[:2] == b'\xa5\x00' and command[8:10] == b'\x00\x01'
        assert command[6:8] == binascii.crc_hqx(command[:6], 0xffff).to_bytes(2, 'big')
        assert command[-2:] == binascii.crc_hqx(command[8:-2], 0xffff).to_bytes(2, 'big')
        if len(sent) == 1:
            assert command.hex() == 'a5000004000070f9000110020320a0b7'
            return Protocol()._add_head([0x10, 1, 2], 1, 0)
        assert command[10:-2] == bytes.fromhex('1112')
        assert command[4:6] == b'\x00\x01'
        return Protocol()._add_head([0x11, 1, 0, 0x12, 1, 5], 1, 1)
    dev._send_command = send
    dev._execute_disconnect = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    await dev.update()
    assert len(sent) == 2
    assert dev.protocol_variant == PROFILE
    assert dev.state.fan == 5
    dev._execute_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_speculative_variant_scan_after_no_response():
    dev = device()
    dev._read_keys = AsyncMock(side_effect=BLEStepError('response wait', 'no_response', 'timeout'))
    dev._execute_disconnect = AsyncMock()
    with pytest.raises(BLEStepError) as error:
        await dev.update()
    assert error.value.code == 'no_response'
    dev._read_keys.assert_awaited_once_with([0x10, 0x02, 0x03, 0x20])
    dev._execute_disconnect.assert_awaited_once()
    assert dev.protocol_variant is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [{}, {0x10: b'\x07'}])
async def test_invalid_mode_stops_before_speed_query_or_profile_save(mode):
    dev = device()
    dev._read_keys = AsyncMock(return_value=mode)
    dev._execute_disconnect = AsyncMock()
    with pytest.raises(BLEStepError) as error:
        await dev.update()
    assert error.value.code == 'invalid_response'
    assert dev.protocol_variant is None
    assert dev._read_keys.await_count == 1
    dev._execute_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_mode_reply_cannot_make_setup_succeed_or_save_profile():
    dev = device()
    dev._read_keys = AsyncMock(side_effect=[{0x10: b'\x02'}, {}])
    dev._execute_disconnect = AsyncMock()
    with pytest.raises(BLEStepError) as error:
        await dev.update()
    assert error.value.code == 'invalid_response'
    assert dev.protocol_variant is None
    assert dev._read_keys.await_count == 2
    dev._execute_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_restores_saved_authoritative_profile():
    dev = device(protocol_variant=PROFILE)
    assert dev.protocol_variant == PROFILE
    assert dev.sequence == 0


@pytest.mark.asyncio
async def test_bad_saved_profile_ignored_with_warning(caplog):
    dev = device(protocol_variant={'unexpected': 'data'})
    assert dev.protocol_variant is None
    assert 'Ignoring unrecognized/inapplicable saved protocol variant' in caplog.text


@pytest.mark.asyncio
async def test_read_callbacks_are_labelled_not_claimed_as_att_notifications(caplog):
    dev = device()
    char = SimpleNamespace(uuid='70d51002-2c7f-4e75-ae8a-d758951ce4e0', handle=16, properties=['read', 'notify'])
    async def read(characteristic):
        dev._notification_handler(16, bytearray(b'\x00'))
        return b'\x00'
    with caplog.at_level(logging.DEBUG):
        await dev._read_diagnostic_characteristic(SimpleNamespace(read_gatt_char=read), char, 'control_reply_test')
    assert 'diagnostic_read=control_reply_test' in caplog.text
    assert dev._diagnostic_read_label is None


@pytest.mark.asyncio
async def test_setup_does_not_read_notify_value_or_accept_read_induced_stale_reply(monkeypatch):
    import custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device as transport
    dev = device()
    dev._setup_diagnostics = True
    dev._read_char = SimpleNamespace(uuid='read', handle=16, properties=['read', 'notify'])
    dev._write_char = SimpleNamespace(uuid='write', handle=20, properties=['write'])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(), is_connected=True)
    dev._read_diagnostic_characteristic = AsyncMock()
    dev._log_subscription_state = AsyncMock()
    monkeypatch.setattr(transport, 'RESPONSE_TIMEOUT', 0.001)
    with pytest.raises(BLEStepError) as error:
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 0))
    assert error.value.code == 'no_response'
    dev._read_diagnostic_characteristic.assert_not_awaited()
    dev._log_subscription_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_setup_always_disconnects_and_does_not_save_profile():
    dev = device()
    dev._read_keys = AsyncMock(side_effect=asyncio.CancelledError())
    dev._execute_disconnect = AsyncMock()
    with pytest.raises(asyncio.CancelledError): await dev.update()
    dev._execute_disconnect.assert_awaited_once()
    assert dev.protocol_variant is None


@pytest.mark.asyncio
async def test_runtime_readback_uses_authoritative_app_query():
    dev = device(protocol_variant=PROFILE)
    dev._read_keys = AsyncMock(return_value={})
    assert await dev._read_back()
    assert dev._read_keys.await_args_list[0].args == ([0x10, 0x02, 0x03, 0x20],)


@pytest.mark.asyncio
async def test_real_future_accepts_version_three_reply_at_sequence_zero(monkeypatch):
    dev = device()
    dev._ensure_connected = AsyncMock()
    dev._execute_disconnect = AsyncMock()
    dev._read_char = SimpleNamespace(uuid='read', handle=16, properties=['notify'])
    dev._write_char = SimpleNamespace(uuid='write', handle=20, properties=['write'])
    async def write(characteristic, command, response):
        assert response is True and command[9] == 1
        seq = int.from_bytes(command[4:6], 'big')
        payload = [0x10, 1, 2] if seq == 0 else [0x11, 1, 0, 0x12, 1, 5]
        reply = bytearray(Protocol()._add_head(payload, 1, seq))
        reply[1] = 0x13
        reply[6:8] = binascii.crc_hqx(reply[:6], 0xffff).to_bytes(2, 'big')
        dev._notification_handler(16, reply)
    dev._client = SimpleNamespace(write_gatt_char=write)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    await dev.update()
    assert dev.state.fan == 5 and dev.protocol_variant == PROFILE
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_reply_must_match_entire_command_not_only_low_byte():
    dev = device()
    dev._notify_future = asyncio.get_running_loop().create_future()
    dev._pending_sequence, dev._pending_command = 0, 1
    frame = bytearray(Protocol()._add_head([0x10, 1, 2], 1, 0))
    frame[8] = 1
    frame[-2:] = binascii.crc_hqx(frame[8:-2], 0xffff).to_bytes(2, 'big')
    dev._notification_handler(16, frame)
    assert not dev._notify_future.done()
    dev._notify_future.cancel()
