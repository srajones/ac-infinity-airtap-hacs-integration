"""Adversarial stress and protocol hardening verification for Final Audit Pass (Challenger 1).

Covers:
1. Handle 28 Telink OTA protection under direct and indirect paths.
2. Concurrency, re-entrancy, task cancellation, and zero-hang lock safety.
3. Packet fuzzing: TLV parsing, command notification reassembly, CRC, and status frames.
4. Coordinator event resilience and disconnected callback recovery.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ac_infinity_airtap_ble.coordinator import (
    ACInfinityDataUpdateCoordinator,
)
from custom_components.ac_infinity_airtap_ble.device import (
    ACInfinityDevice,
    DeviceInfoEx,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import (
    MANUFACTURER_ID,
    POSSIBLE_READ_CHARACTERISTIC_UUIDS,
    POSSIBLE_WRITE_CHARACTERISTIC_UUIDS,
    TELINK_OTA_FORBIDDEN_HANDLES,
    TELINK_OTA_FORBIDDEN_UUIDS,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import (
    validate_not_ota_endpoint,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import (
    BLEStepError,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.protocol import (
    Protocol,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.util import crc16


def make_char(uuid: str, properties: list[str] | None = None, handle: int = 1):
    return SimpleNamespace(
        uuid=uuid,
        properties=properties or ["read", "write", "notify"],
        handle=handle,
        descriptors=[],
        service_uuid="70d51000-2c7f-4e75-ae8a-d758951ce4e0",
    )


def make_controller(cls=ACInfinityDevice):
    ble_device = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        name="D-8DAQJ",
        details={"props": {"Paired": False}},
        rssi=-65,
    )
    state = DeviceInfoEx(type=6, name="D-8DAQJ", version=3)
    return cls(ble_device=ble_device, state=state)


# ==============================================================================
# SECTION 1: Adversarial Handle 28 & Telink OTA Protection (Direct & Indirect)
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_ota_guard_case_insensitivity_and_variants():
    """Verify validate_not_ota_endpoint catches all case variations and representations."""
    for handle in TELINK_OTA_FORBIDDEN_HANDLES:
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(handle)

    for uuid_str in TELINK_OTA_FORBIDDEN_UUIDS:
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(uuid_str)
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(uuid_str.upper())
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(uuid_str.title())

    # Object with handle=28
    char28 = make_char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", handle=28)
    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint(char28)

    # Object with handle=27
    char27 = make_char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", handle=27)
    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint(char27)

    # Object with forbidden UUID but arbitrary handle
    char_uuid_ota = make_char("00010203-0405-0607-0809-0a0b0c0d2b12", handle=105)
    with pytest.raises(ValueError, match="strictly forbidden"):
        validate_not_ota_endpoint(char_uuid_ota)

    # Safe endpoint should not raise
    safe_char = make_char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", handle=20)
    validate_not_ota_endpoint(safe_char)
    validate_not_ota_endpoint(None)


@pytest.mark.asyncio
async def test_adversarial_ota_indirect_injection_into_service_resolution():
    """Verify _resolve_characteristics rejects GATT services advertising OTA handles."""
    dev = make_controller()

    # GATT collection where write char has forbidden handle 28
    ota_write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write"], handle=28)
    safe_read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"], handle=22)

    fake_service = SimpleNamespace(
        uuid="70d51000-2c7f-4e75-ae8a-d758951ce4e0",
        handle=1,
        characteristics=[ota_write_char, safe_read_char],
    )

    class FakeServiceCollection:
        def __iter__(self):
            return iter([fake_service])

        def get_characteristic(self, uuid):
            if uuid == POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0]:
                return ota_write_char
            if uuid == POSSIBLE_READ_CHARACTERISTIC_UUIDS[0]:
                return safe_read_char
            return None

    # When resolving, validate_not_ota_endpoint must intercept handle 28 and raise ValueError
    with pytest.raises(ValueError, match="strictly forbidden"):
        dev._resolve_characteristics(FakeServiceCollection())


@pytest.mark.asyncio
async def test_adversarial_ota_indirect_command_pipeline_blocked():
    """Verify that any command write attempt to Handle 28 is aborted before BLE dispatch."""
    dev = make_controller()
    mock_client = SimpleNamespace(
        write_gatt_char=AsyncMock(),
        read_gatt_char=AsyncMock(),
        is_connected=True,
    )
    dev._client = mock_client
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"], handle=22)
    dev._write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write"], handle=28)

    command = Protocol().set_level(6, 2, 5, 0, 1)

    with pytest.raises(ValueError, match="strictly forbidden"):
        await dev._execute_command_locked(command)

    # BLE client must NOT have been called
    mock_client.write_gatt_char.assert_not_called()


# ==============================================================================
# SECTION 2: Concurrency, Cancellation, and Lock Safety
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_concurrent_commands_with_random_cancellations():
    """Stress-test _command_turn under high concurrency with interleaved cancellations."""
    dev = make_controller()
    disconnect_calls = 0

    async def mock_disconnect():
        nonlocal disconnect_calls
        disconnect_calls += 1

    dev._execute_disconnect = AsyncMock(side_effect=mock_disconnect)

    completed = 0
    cancelled = 0

    async def worker(idx: int):
        nonlocal completed, cancelled
        try:
            async with dev._command_turn():
                await asyncio.sleep(0.02)
                completed += 1
        except asyncio.CancelledError:
            cancelled += 1
            raise

    # Launch 30 concurrent command turns
    tasks = [asyncio.create_task(worker(i)) for i in range(30)]

    # Cancel half of them after a tiny delay
    await asyncio.sleep(0.01)
    for i in range(0, 30, 2):
        tasks[i].cancel()

    # Wait for all tasks to settle
    await asyncio.gather(*tasks, return_exceptions=True)

    # Verify counts
    assert cancelled == 15
    assert completed == 15
    assert dev._commands_in_line == 0
    # Lock must be released
    assert not dev._command_lock.locked()
    # Disconnect must have been triggered when the last task finished
    assert disconnect_calls >= 1

    # Now verify that a subsequent command can acquire the lock cleanly
    async with dev._command_turn():
        assert dev._commands_in_line == 1
    assert dev._commands_in_line == 0


@pytest.mark.asyncio
async def test_adversarial_disconnection_callback_wakes_waiting_future_instantly():
    """Verify that peripheral disconnect aborts pending _notify_future immediately without waiting 8s."""
    dev = make_controller()
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"])
    dev._write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write"])
    mock_client = SimpleNamespace(
        write_gatt_char=AsyncMock(),
        is_connected=True,
    )
    dev._client = mock_client

    command = Protocol().get_model_data(6, 0, 1)

    start = asyncio.get_running_loop().time()

    async def disconnect_trigger():
        await asyncio.sleep(0.05)
        # Peripheral drops link
        dev._disconnected(mock_client)

    asyncio.create_task(disconnect_trigger())

    with pytest.raises(BLEStepError) as exc_info:
        await dev._execute_command_locked(command)

    elapsed = asyncio.get_running_loop().time() - start

    assert exc_info.value.code == "connect_failed"
    assert "Device disconnected" in str(exc_info.value)
    # Must have completed in < 0.5s, NOT the 8s RESPONSE_TIMEOUT
    assert elapsed < 1.0


# ==============================================================================
# SECTION 3: Packet Fuzzing, Corrupted Framing & Status Data
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_tlv_read_keys_fuzzing():
    """Fuzz _read_keys TLV parser with corrupt lengths, zero lengths, and truncations."""
    dev = make_controller()

    # Test TLV decode via _read_keys with mock _send_command
    async def run_tlv_test(raw_payload):
        # Frame format: 10 header bytes + raw_payload + 2 CRC bytes
        frame = bytearray(10 + len(raw_payload) + 2)
        frame[0:2] = b"\xa5\x00"
        frame[9] = 1  # query command
        frame[10 : 10 + len(raw_payload)] = raw_payload
        dev._send_command = AsyncMock(return_value=bytes(frame))
        return await dev._read_keys([0x10])

    # 1. Truncated TLV (length byte claims 5 bytes, only 1 available)
    with pytest.raises(BLEStepError, match="Truncated TLV payload"):
        await run_tlv_test(b"\x10\x05\x01")

    # 2. Length byte claims 0 bytes (valid, empty key payload)
    res = await run_tlv_test(b"\x10\x00\x11\x01\x05")
    assert res[0x10] == b""
    assert res[0x11] == b"\x05"

    # 3. Missing length byte (odd number of bytes)
    with pytest.raises(BLEStepError, match="Truncated TLV payload"):
        await run_tlv_test(b"\x10")


@pytest.mark.asyncio
async def test_adversarial_notification_fragmentation_reassembly():
    """Verify that multi-chunk fragmented responses are reassembled correctly."""
    dev = make_controller()
    dev._pending_sequence = 1
    dev._pending_command = 1
    dev._notify_future = asyncio.get_running_loop().create_future()

    # Build a valid 20-byte response frame (payload len = 6)
    # Header: \xa5\x00 (2) + len 6 (2) + seq 1 (2) + crc (2) + \x00 (1) + cmd 1 (1) = 10 bytes
    header_pre = bytearray(b"\xa5\x00\x00\x06\x00\x01")
    header_crc = bytes(crc16(header_pre, 0, 6))
    header = header_pre + header_crc + b"\x00\x01"  # 10 bytes
    data_payload = b"\x10\x01\x02\x11\x01\x05"  # 6 bytes
    full_frame = bytearray(header + data_payload)
    payload_crc = bytes(crc16(full_frame, 8, len(full_frame) - 8))
    full_frame.extend(payload_crc)

    assert len(full_frame) == 18  # 12 overhead + 6 payload = 18

    # Split into 3 fragments
    frag1 = full_frame[:5]
    frag2 = full_frame[5:12]
    frag3 = full_frame[12:]

    # Feed fragment 1
    assert dev._accept_command_notification(frag1) is True
    assert not dev._notify_future.done()

    # Feed fragment 2
    assert dev._accept_command_notification(frag2) is True
    assert not dev._notify_future.done()

    # Feed fragment 3
    assert dev._accept_command_notification(frag3) is True
    assert dev._notify_future.done()
    assert bytes(dev._notify_future.result()) == bytes(full_frame)


@pytest.mark.asyncio
async def test_adversarial_status_frame_boundary_values():
    """Verify AirTap 0x1EFF status frames handle negative, zero, and extreme values safely."""
    dev = make_controller()

    # Status frame with 0 degrees C (temp = 0)
    frame_zero = bytearray(18)
    frame_zero[0:2] = b"\x1e\xff"
    frame_zero[8:10] = (0).to_bytes(2, "big")
    frame_zero[10:12] = (5000).to_bytes(2, "big")
    assert dev._parse_status_frame(bytes(frame_zero)) is True
    assert dev.state.tmp == 0.0
    assert dev.state.hum == 50.0

    # Status frame with high temperature (45.50 C = 4550)
    frame_high = bytearray(18)
    frame_high[0:2] = b"\x1e\xff"
    frame_high[8:10] = (4550).to_bytes(2, "big")
    assert dev._parse_status_frame(bytes(frame_high)) is True
    assert dev.state.tmp == 45.50

    # Status frame with truncated length (< 16 bytes: 2 header bytes + 0..13 data bytes = 2..15 bytes)
    for length in range(14):
        short_frame = bytes([0x1E, 0xFF] + [0x00] * length)
        assert dev._parse_status_frame(short_frame) is False


# ==============================================================================
# SECTION 4: Coordinator Event Resilience
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_coordinator_bluetooth_event_malformed_resilience():
    """Verify that coordinator isolates bad advertisement data without crashing callback loop."""
    dev = make_controller()
    mock_hass = MagicMock()
    mock_logger = MagicMock()
    ble_device = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        name="D-8DAQJ",
    )

    coordinator = ACInfinityDataUpdateCoordinator(
        hass=mock_hass,
        logger=mock_logger,
        ble_device=ble_device,
        controller=dev,
    )

    # Corrupt advertisement data
    bad_service_info = SimpleNamespace(
        device=ble_device,
        source="test_proxy",
        advertisement=SimpleNamespace(
            manufacturer_data={MANUFACTURER_ID: b"\x00\x01\x02"},  # Truncated
        ),
    )

    # Should not raise exception
    coordinator._async_handle_bluetooth_event(bad_service_info, None)
    assert coordinator._malformed_advertisement is True

    # Now provide valid advertisement data
    valid_data = bytearray(20)
    valid_data[6:11] = b"8DAQJ"
    valid_data[11] = 3
    valid_data[12] = 6
    valid_data[14:16] = (2250).to_bytes(2, "big")  # 22.5 C

    good_service_info = SimpleNamespace(
        device=ble_device,
        source="test_proxy",
        advertisement=SimpleNamespace(
            manufacturer_data={MANUFACTURER_ID: bytes(valid_data)},
        ),
    )

    coordinator._async_handle_bluetooth_event(good_service_info, None)
    assert coordinator._malformed_advertisement is False
    assert dev.state.tmp == 22.5
