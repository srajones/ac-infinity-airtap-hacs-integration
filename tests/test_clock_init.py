"""Type-6 initialization reconstructed as protocol facts, not copied app code."""
import asyncio
import binascii
import logging
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError

from custom_components.ac_infinity_airtap_ble.device import ACInfinityDevice
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.models import DeviceInfo
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.protocol import Protocol

NOW = datetime(2026, 11, 7, 19, 21, 33, tzinfo=ZoneInfo("America/Detroit"))


def test_clock_frame_has_app_calendar_payload_and_independent_crcs():
    frame = Protocol().set_clock_data(NOW, 0)
    assert len(frame) == 21
    assert frame[:6] == bytes.fromhex("a50000090000")
    assert frame[8:10] == b"\x00\x03"
    assert frame[10:-2] == bytes.fromhex("01071a0b0706131521")
    assert frame[6:8] == binascii.crc_hqx(frame[:6], 0xffff).to_bytes(2, "big")
    assert frame[-2:] == binascii.crc_hqx(frame[8:-2], 0xffff).to_bytes(2, "big")


@pytest.mark.parametrize("day,weekday", [(8, 7), (9, 1)])
def test_clock_weekdays_follow_iso_not_android_calendar_numbering(day, weekday):
    frame = Protocol().set_clock_data(NOW.replace(day=day), 0xffff)
    assert frame[15] == weekday
    assert frame[4:6] == b"\xff\xff"


@pytest.mark.parametrize("now", [NOW.replace(tzinfo=None), NOW.replace(year=1999), NOW.replace(year=2100)])
def test_clock_rejects_ambiguous_or_out_of_range_dates(now):
    with pytest.raises(ValueError):
        Protocol().set_clock_data(now, 0)


@pytest_asyncio.fixture
async def session(monkeypatch):
    import custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device as transport
    dev = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"),
                           state=DeviceInfo(type=6, version=3, name="test"))
    dev._get_clock_time = lambda: NOW
    sent, clients = [], []
    behavior = {"clock_ack": [1, 0], "silent": False, "cancel": False, "setting_error": False, "clock_write_errors": 0}
    def resolve(_):
        dev._read_char = SimpleNamespace(uuid="70d51002-2c7f-4e75-ae8a-d758951ce4e0", handle=16, properties=["notify"])
        dev._write_char = SimpleNamespace(uuid="70d51001-2c7f-4e75-ae8a-d758951ce4e0", handle=20, properties=["write"])
        return True
    dev._resolve_characteristics = resolve
    async def connect(*args, **kwargs):
        client = SimpleNamespace(is_connected=True, services=[], mtu_size=247, callback=None,
                                 stop_notify=AsyncMock(), disconnect=AsyncMock())
        async def subscribe(char, callback): client.callback = callback
        pending_clock_chunk = bytearray()
        async def write(char, frame, response):
            assert char.uuid.startswith("70d51001") and response is True
            frame = bytes(frame)
            if len(frame) == 20 and frame[9] == 3 and frame[10:12] == b"\x01\x07":
                pending_clock_chunk.extend(frame)
                return
            if pending_clock_chunk:
                frame = bytes(pending_clock_chunk) + frame
                pending_clock_chunk.clear()
            sent.append(frame)
            if behavior["cancel"]: raise asyncio.CancelledError()
            is_clock = frame[9] == 3 and frame[10:12] == b"\x01\x07"
            if is_clock and behavior["clock_write_errors"]:
                behavior["clock_write_errors"] -= 1
                raise BleakError("ATT clock write failed: Invalid Attribute Value Length")
            if is_clock and behavior["silent"]: return
            if not is_clock and frame[9] == 3 and behavior["setting_error"]:
                raise BleakError("setting write rejected")
            payload = behavior["clock_ack"] if is_clock else [0x10, 1, 2]
            if frame[10:-2] == bytes.fromhex("1112"):
                payload = [0x11, 1, 0, 0x12, 1, 5]
            reply = Protocol()._add_head(payload, frame[9], int.from_bytes(frame[4:6], "big"))
            client.callback(16, bytearray(reply))
        async def disconnect(): client.is_connected = False
        client.start_notify = AsyncMock(side_effect=subscribe)
        client.write_gatt_char = AsyncMock(side_effect=write)
        client.disconnect.side_effect = disconnect
        clients.append(client)
        return client
    monkeypatch.setattr(transport, "establish_connection", AsyncMock(side_effect=connect))
    monkeypatch.setattr(transport, "RESPONSE_TIMEOUT", 0.002)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    try:
        yield dev, sent, clients, behavior
    finally:
        await dev.stop()


@pytest.mark.asyncio
async def test_clock_is_first_single_21_byte_write_before_model_query_and_once_per_connection(session, caplog):
    dev, sent, clients, _ = session
    with caplog.at_level(logging.DEBUG):
        assert await dev._read_keys([0x10, 0x02, 0x03, 0x20]) == {0x10: b"\x02"}
        await dev._read_keys([0x11, 0x12])
    assert [frame[9] for frame in sent] == [3, 1, 1]
    assert [int.from_bytes(frame[4:6], "big") for frame in sent] == [0, 1, 2]
    assert len(sent[0]) == 21
    assert len(clients) == 1
    assert "clock sync acknowledged" in caplog.text
    assert "single acknowledged write length=21" in caplog.text
    assert "BlueZ" in caplog.text and "247" in caplog.text
    await dev.stop()


@pytest.mark.asyncio
async def test_clock_is_repeated_with_sequence_zero_on_fresh_connection(session):
    dev, sent, clients, _ = session
    await dev._read_keys([0x10])
    await dev._execute_disconnect()
    await dev._read_keys([0x10])
    assert [(f[9], int.from_bytes(f[4:6], "big")) for f in sent] == [(3, 0), (1, 1), (3, 0), (1, 1)]
    assert len(clients) == 2
    await dev.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[1, 1], [1, 2], [], [2, 0], [1, 0, 1]])
async def test_rejected_or_malformed_clock_ack_never_queries_or_retries(session, payload):
    dev, sent, clients, behavior = session
    behavior["clock_ack"] = payload
    with pytest.raises(BLEStepError) as error:
        await dev._read_keys([0x10])
    assert error.value.code == "initialization_failed"
    assert "clock sync" in error.value.step
    assert len(sent) == 1 and sent[0][9] == 3
    clients[0].disconnect.assert_awaited_once()
    assert dev._client is None and dev._notify_future is None
    assert not dev.last_command_success and dev.protocol_variant is None


@pytest.mark.asyncio
async def test_clock_timeout_retries_bounded_fresh_sessions_without_query_or_warning_spam(session, caplog):
    dev, sent, clients, behavior = session
    behavior["silent"] = True
    with pytest.raises(BLEStepError) as error:
        await dev._read_keys([0x10])
    assert error.value.code == "no_response" and "clock sync" in error.value.step
    assert "BLE clock sync response wait failed" in caplog.text
    assert len(sent) == len(clients) == 3
    assert all(f[9] == 3 and f[4:6] == b"\x00\x00" for f in sent)
    assert [c.args[0] for c in asyncio.sleep.await_args_list if c.args[0] != 0.02] == [1.5, 0.25, 1.5, 0.5, 1.5]
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
    assert dev._client is None and dev._notify_future is None
    for client in clients: client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_clock_write_cleans_connection_and_propagates(session):
    dev, sent, clients, behavior = session
    behavior["cancel"] = True
    with pytest.raises(asyncio.CancelledError): await dev._read_keys([0x10])
    assert len(sent) == 1 and sent[0][9] == 3
    clients[0].disconnect.assert_awaited_once()
    assert dev._client is None and dev._notify_future is None


@pytest.mark.asyncio
async def test_user_setting_waits_for_clock_but_uncertain_setting_is_never_replayed(session):
    dev, sent, clients, behavior = session
    behavior["setting_error"] = True
    with pytest.raises(HomeAssistantError): await dev._send_setting_now([0x10, 1, 2])
    assert [(f[9], int.from_bytes(f[4:6], "big")) for f in sent] == [(3, 0), (3, 1)]
    assert len(clients) == 1 and dev._client is None


@pytest.mark.asyncio
async def test_integration_uses_home_assistant_timezone_not_host_default(monkeypatch):
    import homeassistant
    fake_dt = SimpleNamespace(now=lambda: NOW)
    fake_util = SimpleNamespace(dt=fake_dt)
    monkeypatch.setattr(homeassistant, "util", fake_util, raising=False)
    monkeypatch.setitem(sys.modules, "homeassistant.util", fake_util)
    monkeypatch.setitem(sys.modules, "homeassistant.util.dt", fake_dt)
    dev = ACInfinityDevice(SimpleNamespace(address="test"), state=DeviceInfo(type=6, version=3, name="test"))
    assert dev._get_clock_time() is NOW


@pytest.mark.asyncio
async def test_clock_never_uses_unacknowledged_fallback(session):
    dev, sent, clients, _ = session
    original = dev._resolve_characteristics
    def resolve(services):
        original(services)
        dev._write_char.properties = ["write-without-response"]
        return True
    dev._resolve_characteristics = resolve
    with pytest.raises(BLEStepError) as error: await dev._read_keys([0x10])
    assert error.value.code == "unsupported_model"
    assert not sent and len(clients) == 1
    assert dev._client is None


def test_initialization_error_has_matching_translations():
    import json
    from pathlib import Path
    root = Path(__file__).parents[1] / "custom_components/ac_infinity_airtap_ble"
    for path in (root / "strings.json", root / "translations/en.json"):
        errors = json.loads(path.read_text())["config"]["error"]
        assert "clock" in errors["initialization_failed"].lower()


@pytest.mark.asyncio
async def test_complete_setup_still_requires_model_and_speed_after_clock(session):
    dev, sent, _, _ = session
    await dev.update()
    assert [f[9] for f in sent] == [3, 1, 1]
    assert [int.from_bytes(f[4:6], "big") for f in sent] == [0, 1, 2]
    assert dev.protocol_variant == "android_2_1_2_telink"
    assert dev.state.fan == 5 and dev._client is None


@pytest.mark.asyncio
async def test_legacy_type_48_does_not_gain_clock_initialization(session):
    dev, sent, _, _ = session
    dev._state.type = 48
    dev._get_clock_time = lambda: pytest.fail("Non-type-6 must not sync clock")
    await dev._read_keys([0x10])
    assert [f[9] for f in sent] == [1]


@pytest.mark.asyncio
async def test_lost_model_reply_reconnects_and_rebuilds_query_after_clock(session):
    dev, sent, clients, _ = session
    original = dev._send_command_while_connected
    first = True
    async def send(frame, retry=None):
        nonlocal first
        if frame[9] == 1 and first:
            first = False
            raise BLEStepError("response wait", "no_response", "lost model reply")
        return await original(frame, retry)
    dev._send_command_while_connected = send
    await dev._read_keys([0x10])
    assert [(f[9], int.from_bytes(f[4:6], "big")) for f in sent] == [(3, 0), (3, 0), (1, 1)]
    assert len(clients) == 2


@pytest.mark.asyncio
async def test_clock_write_failure_is_labelled_before_warning_and_preserves_att_reason(session, caplog):
    dev, sent, clients, behavior = session
    behavior["clock_write_errors"] = 3
    with caplog.at_level(logging.DEBUG), pytest.raises(BLEStepError) as error:
        await dev._read_keys([0x10])
    assert error.value.code == "write_failed" and "clock sync write" in error.value.step
    assert "Invalid Attribute Value Length" in str(error.value)
    assert "BLE clock sync write failed" in caplog.text
    assert len(sent) == len(clients) == 3 and all(f[9] == 3 for f in sent)
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_transient_clock_write_failure_can_recover_on_fresh_session(session):
    dev, sent, clients, behavior = session
    behavior["clock_write_errors"] = 1
    await dev._read_keys([0x10])
    assert [(f[9], int.from_bytes(f[4:6], "big")) for f in sent] == [(3, 0), (3, 0), (1, 1)]
    assert len(clients) == 2 and dev.last_command_success


@pytest.mark.asyncio
async def test_bad_local_calendar_fails_before_any_write_and_releases_client(session):
    dev, sent, clients, _ = session
    dev._get_clock_time = lambda: NOW.replace(tzinfo=None)
    with pytest.raises(BLEStepError) as error: await dev._read_keys([0x10])
    assert error.value.code == "initialization_failed"
    assert error.value.step == "clock sync calendar"
    assert not sent and len(clients) == 1
    assert dev._client is None


@pytest.mark.asyncio
async def test_clock_ack_alone_does_not_clear_failure_streak_or_restore_availability(session):
    dev, sent, _, _ = session
    dev._failure_streak = True
    dev.last_command_success = False
    await dev._ensure_connected()
    assert [f[9] for f in sent] == [3]
    assert dev._failure_streak and not dev.last_command_success
    await dev._read_keys([0x10])
    assert not dev._failure_streak and dev.last_command_success


@pytest.mark.asyncio
async def test_user_action_never_writes_fan_setting_if_clock_init_times_out(session):
    dev, sent, clients, behavior = session
    behavior["silent"] = True
    with pytest.raises(HomeAssistantError): await dev._send_setting_now([0x10, 1, 2])
    assert len(sent) == len(clients) == 3
    assert all(f[9] == 3 and f[10:12] == b"\x01\x07" for f in sent)
    assert dev._client is None and dev._notify_future is None
