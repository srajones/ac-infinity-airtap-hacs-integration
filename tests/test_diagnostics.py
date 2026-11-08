import asyncio
from binascii import crc_hqx
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import ACInfinityController
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.models import DeviceInfo
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.protocol import Protocol

BASE = "-0000-1000-8000-00805f9b34fb"
CONTROL = "70d51002-2c7f-4e75-ae8a-d758951ce4e0"


def controller():
    return ACInfinityController(SimpleNamespace(address="A4:C1:38:F6:48:F6", details={"props": {"Paired": False, "ServicesResolved": True}}),
                                state=DeviceInfo(6, "D-8DAQJ", 3))


def char(uuid, properties=None):
    return SimpleNamespace(uuid=uuid, properties=properties or ["read"], handle=33, descriptors=[], service_uuid="test")


@pytest.mark.asyncio
async def test_revision_diagnostics_are_reads_and_run_once_per_controller(caplog):
    caplog.set_level("DEBUG")
    dev = controller()
    chars = {f"0000{key}{BASE}": char(f"0000{key}{BASE}") for key in ["2a26", "2a27", "2a28", "2a50"]}
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=chars.get), read_gatt_char=AsyncMock(return_value=bytearray(b"3.2.1")), is_connected=True)
    await dev._read_device_information(client)
    await dev._read_device_information(client)
    assert client.read_gatt_char.await_count == 4
    assert "firmware_revision" in caplog.text
    assert "hardware_revision" in caplog.text
    assert "software_revision" in caplog.text
    assert "pnp_id" in caplog.text


@pytest.mark.asyncio
async def test_optional_metadata_auth_error_logs_and_does_not_pair_or_abort(caplog):
    from bleak.exc import BleakError
    dev = controller()
    revision = char(f"00002a26{BASE}")
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=lambda uuid: revision if uuid == revision.uuid else None),
                             read_gatt_char=AsyncMock(side_effect=BleakError("Insufficient Authentication")), is_connected=True)
    caplog.set_level("DEBUG")
    await dev._read_device_information(client)
    assert "Insufficient Authentication" in caplog.text
    assert dev.last_command_success


@pytest.mark.asyncio
async def test_cccd_readback_reports_disabled_then_enabled_without_writing(caplog):
    caplog.set_level("DEBUG")
    dev = controller()
    dev._read_char = char(CONTROL, ["read", "notify"])
    dev._read_char.descriptors = [SimpleNamespace(uuid=f"00002902{BASE}", handle=17)]
    client = SimpleNamespace(read_gatt_descriptor=AsyncMock(side_effect=[b"\x00\x00", b"\x01\x00"]))
    await dev._log_subscription_state(client, "before_subscribe")
    await dev._log_subscription_state(client, "after_subscribe")
    assert "notifications_enabled=False" in caplog.text
    assert "notifications_enabled=True" in caplog.text
    assert client.read_gatt_descriptor.await_count == 2


@pytest.mark.asyncio
async def test_metadata_cancellation_propagates():
    dev = controller()
    revision = char(f"00002a26{BASE}")
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=lambda uuid: revision),
                             read_gatt_char=AsyncMock(side_effect=asyncio.CancelledError()), is_connected=True)
    with pytest.raises(asyncio.CancelledError):
        await dev._read_device_information(client)


@pytest.mark.asyncio
async def test_diagnostic_read_rejects_ota_before_any_io():
    dev = controller()
    client = SimpleNamespace(read_gatt_char=AsyncMock())
    ota = char("00010203-0405-0607-0809-0a0b0c0d2b12")
    with pytest.raises(ValueError, match="allowlist"):
        await dev._read_diagnostic_characteristic(client, ota, "unsafe")
    client.read_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [0, 0x10, 0x13, 0x17, 0x1f])
async def test_apk_reply_header_family_is_accepted_with_crc_and_sequence(header):
    dev = controller()
    dev._pending_sequence, dev._pending_command = 2, 1
    dev._notify_future = asyncio.get_running_loop().create_future()
    frame = bytearray(Protocol()._add_head([0x10, 1, 2], 1, 2))
    frame[1] = header
    frame[6:8] = crc_hqx(frame[:6], 0xffff).to_bytes(2, "big")
    dev._notification_handler(1, frame)
    assert dev._notify_future.done()
    assert dev._notify_future.result() == frame


@pytest.mark.asyncio
async def test_invalid_header_family_still_rejected():
    dev = controller()
    dev._pending_sequence, dev._pending_command = 2, 1
    dev._notify_future = asyncio.get_running_loop().create_future()
    frame = bytearray(Protocol()._add_head([0x10, 1, 2], 1, 2))
    frame[1] = 0x20
    frame[6:8] = crc_hqx(frame[:6], 0xffff).to_bytes(2, "big")
    dev._notification_handler(1, frame)
    assert not dev._notify_future.done()
    dev._notify_future.cancel()


@pytest.mark.asyncio
async def test_timeout_readback_is_diagnostic_only_and_cannot_fake_setup_success(monkeypatch, caplog):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    monkeypatch.setattr(transport, "RESPONSE_TIMEOUT", 0.001)
    caplog.set_level("DEBUG")
    dev = controller()
    dev.state.type = 48  # Type 6 no longer performs control-value READ snapshots.
    dev._setup_diagnostics = True
    dev._read_char = char(CONTROL, ["read", "notify"])
    dev._write_char = char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", ["read", "write"])
    command = Protocol()._add_head([0x10], 1, 2)
    stored_reply = Protocol()._add_head([0x10, 1, 2], 1, 2)
    client = SimpleNamespace(is_connected=True, read_gatt_char=AsyncMock(side_effect=[b"old", stored_reply]), write_gatt_char=AsyncMock())
    dev._client = client
    with pytest.raises(BLEStepError) as error:
        await dev._execute_command_locked(command)
    assert error.value.code == "no_response"
    assert client.read_gatt_char.await_count == 2
    assert "control_reply_after_timeout_not_accepted_as_live" in caplog.text
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_disconnected_response_wait_keeps_original_error_with_diagnostics_enabled():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    dev._setup_diagnostics = True
    dev._read_char = char(CONTROL, ["read", "notify"])
    dev._write_char = char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", ["read", "write"])
    client = SimpleNamespace(is_connected=True, read_gatt_char=AsyncMock(return_value=b""))
    dev._log_subscription_state = AsyncMock()
    async def write(*args):
        client.is_connected = False
        dev._disconnected(client)
    client.write_gatt_char = write
    dev._client = client
    with pytest.raises(BLEStepError, match="disconnected") as error:
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 2))
    assert error.value.code == "connect_failed"
    assert dev._notify_future is None
    dev._log_subscription_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_query_read_cannot_complete_a_not_yet_written_query(monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble import device as transport
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    monkeypatch.setattr(transport, "RESPONSE_TIMEOUT", 0.001)
    dev = controller()
    dev.state.type = 48  # Type 6 no longer performs control-value READ snapshots.
    dev._setup_diagnostics = True
    dev._read_char = char(CONTROL, ["read", "notify"])
    dev._write_char = char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", ["read", "write"])
    stored = bytearray(Protocol()._add_head([0x10, 1, 2], 1, 2))
    async def read(characteristic):
        dev._notification_handler(1, stored)
        return stored
    dev._client = SimpleNamespace(is_connected=True, read_gatt_char=read, write_gatt_char=AsyncMock())
    with pytest.raises(BLEStepError, match="response wait"):
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 2))


@pytest.mark.asyncio
async def test_disabled_cccd_warning_is_not_spammed_during_retries(caplog):
    dev = controller()
    dev._read_char = char(CONTROL, ["read", "notify"])
    dev._read_char.descriptors = [SimpleNamespace(uuid=f"00002902{BASE}", handle=17)]
    client = SimpleNamespace(read_gatt_descriptor=AsyncMock(return_value=b"\x00\x00"))
    await dev._log_subscription_state(client, "after_subscribe")
    await dev._log_subscription_state(client, "after_subscribe")
    assert sum(record.levelname == "WARNING" for record in caplog.records) == 1


@pytest.mark.asyncio
async def test_disconnect_during_baseline_read_never_attempts_command_write():
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError
    dev = controller()
    dev.state.type = 48  # Type 6 no longer performs control-value READ snapshots.
    dev._setup_diagnostics = True
    dev._read_char = char(CONTROL, ["read", "notify"])
    dev._write_char = char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", ["read", "write"])
    client = SimpleNamespace(is_connected=True, write_gatt_char=AsyncMock())
    async def read(*args):
        client.is_connected = False
        dev._disconnected(client)
        return b""
    client.read_gatt_char = read
    dev._client = client
    with pytest.raises(BLEStepError) as error:
        await dev._execute_command_locked(Protocol()._add_head([0x10], 1, 2))
    assert error.value.code == "connect_failed"
    client.write_gatt_char.assert_not_awaited()
    assert dev._notify_future is None


@pytest.mark.asyncio
async def test_optional_revision_lookup_failure_is_logged_not_fatal(caplog):
    from bleak.exc import BleakError
    dev = controller()
    def lookup(uuid):
        raise BleakError("Ambiguous revision characteristic")
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=lookup), is_connected=True)
    caplog.set_level("DEBUG")
    await dev._read_device_information(client)
    assert "Ambiguous revision characteristic" in caplog.text
    assert dev.last_command_success


@pytest.mark.asyncio
async def test_connection_context_uses_only_allowlisted_cached_fields(caplog):
    caplog.set_level("DEBUG")
    dev = controller()
    dev._ble_device.details["props"]["IrrelevantField"] = "do-not-log"
    class Client:
        @property
        def mtu_size(self):
            raise NotImplementedError("MTU unavailable on this backend")
    dev._log_connection_context(Client())
    assert "discovery_snapshot_not_live" in caplog.text
    assert "Paired" in caplog.text
    assert "do-not-log" not in caplog.text
