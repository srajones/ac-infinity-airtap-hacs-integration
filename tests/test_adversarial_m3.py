import ast
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.ac_infinity_airtap_ble.device import ACInfinityDevice
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import (
    POSSIBLE_READ_CHARACTERISTIC_UUIDS,
    POSSIBLE_WRITE_CHARACTERISTIC_UUIDS,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import (
    ACInfinityController,
    DISCONNECT_TIMEOUT,
    GATT_TIMEOUT,
    RESPONSE_TIMEOUT,
    validate_not_ota_endpoint,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import (
    BLEStepError,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.models import (
    DeviceInfo,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.protocol import (
    Protocol,
    parse_manufacturer_data,
)


def make_controller(cls=ACInfinityController):
    return cls(
        SimpleNamespace(address="A4:C1:38:F6:48:F6", details={"props": {"Paired": False}}),
        state=DeviceInfo(type=6, name="D-8DAQJ", version=3),
    )


def make_char(uuid: str, properties: list[str] | None = None, handle: int = 1):
    return SimpleNamespace(
        uuid=uuid,
        properties=properties or ["read", "write", "notify"],
        handle=handle,
        descriptors=[],
        service_uuid="70d51000-2c7f-4e75-ae8a-d758951ce4e0",
    )


# ==============================================================================
# CHALLENGE 1: Telink OTA Protection Guard
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_telink_ota_guard_strict():
    """Verify that Handle 27, Handle 28, and UUIDs 1912 / 2b12 raise ValueError before any I/O."""
    for handle in [27, 28]:
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(handle)
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(SimpleNamespace(handle=handle, uuid="normal-uuid"))

    for uuid_str in [
        "00010203-0405-0607-0809-0a0b0c0d1912",
        "00010203-0405-0607-0809-0a0b0c0d2b12",
        "00010203-0405-0607-0809-0A0B0C0D2B12",  # uppercase
    ]:
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(uuid_str)
        with pytest.raises(ValueError, match="strictly forbidden"):
            validate_not_ota_endpoint(SimpleNamespace(handle=99, uuid=uuid_str))

    # Safe endpoint does not raise
    validate_not_ota_endpoint(make_char("70d51001-2c7f-4e75-ae8a-d758951ce4e0", handle=20))
    validate_not_ota_endpoint(None)


@pytest.mark.asyncio
async def test_adversarial_ota_guard_blocks_write_locked():
    """Verify _execute_command_locked immediately throws ValueError if write_char is handle 28."""
    dev = make_controller()
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"], handle=22)
    dev._write_char = make_char("00010203-0405-0607-0809-0a0b0c0d2b12", ["write"], handle=28)
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(), is_connected=True)

    with pytest.raises(ValueError, match="strictly forbidden"):
        await dev._execute_command_locked(b"\xa5\x00\x00\x01\x00\x01\x00\x00\x00\x01\x10\x00\x00")

    dev._client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_adversarial_ota_guard_blocks_diagnostic_read():
    """Verify _read_diagnostic_characteristic immediately throws ValueError on handle 28 or UUID 2b12."""
    dev = make_controller()
    dev._client = SimpleNamespace(read_gatt_char=AsyncMock(), is_connected=True)

    ota_char = make_char("00010203-0405-0607-0809-0a0b0c0d2b12", ["read"], handle=28)
    with pytest.raises(ValueError, match="strictly forbidden"):
        await dev._read_diagnostic_characteristic(dev._client, ota_char, "unsafe_read")

    dev._client.read_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_adversarial_ota_guard_blocks_telink_init():
    """Verify _initialize_telink_session rejects OTA characteristics before any I/O."""
    dev = make_controller()
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"], handle=22)
    dev._write_char = make_char("00010203-0405-0607-0809-0a0b0c0d2b12", ["write"], handle=28)
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(), is_connected=True)

    with pytest.raises(ValueError, match="strictly forbidden"):
        await dev._initialize_telink_session()

    dev._client.write_gatt_char.assert_not_awaited()


# ==============================================================================
# CHALLENGE 2: Zero Synchronous I/O or time.sleep
# ==============================================================================


def test_adversarial_zero_sync_io_and_no_time_sleep():
    """Static AST audit: Ensure zero time.sleep, open(), urllib, requests in custom_components."""
    root_dir = "custom_components/ac_infinity_airtap_ble"
    violations = []

    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            with open(path, encoding="utf-8") as fp:
                tree = ast.parse(fp.read(), filename=path)

            for node in ast.walk(tree):
                # Check for `import time` or `from time import sleep`
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in ("time", "urllib", "requests", "httpx", "socket"):
                            violations.append(f"{path}:{node.lineno}: Disallowed import '{alias.name}'")
                elif isinstance(node, ast.ImportFrom):
                    if node.module in ("time", "urllib", "requests", "httpx", "socket"):
                        violations.append(f"{path}:{node.lineno}: Disallowed import from '{node.module}'")
                # Check for calls to time.sleep or open()
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        if func.attr == "sleep" and isinstance(func.value, ast.Name) and func.value.id == "time":
                            violations.append(f"{path}:{node.lineno}: Call to 'time.sleep()'")
                    elif isinstance(func, ast.Name):
                        if func.id == "open":
                            violations.append(f"{path}:{node.lineno}: Direct call to 'open()'")

    assert not violations, f"Found blocking calls: {violations}"


# ==============================================================================
# CHALLENGE 3: Packet Truncation and Fuzzing Resilience
# ==============================================================================


def test_adversarial_manufacturer_data_fuzzing():
    """Fuzz parse_manufacturer_data with truncated, corrupt, and oversized payloads."""
    # Length < 19 must raise ValueError, NEVER IndexError
    for length in range(0, 19):
        with pytest.raises(ValueError, match="Truncated"):
            parse_manufacturer_data(bytes([0x00] * length))

    # Types requiring length >= 23
    for dev_type in [7, 8, 9, 11, 12, 16, 17, 18, 20]:
        buf = bytearray(20)
        buf[11] = 3  # version
        buf[12] = dev_type
        with pytest.raises(ValueError, match="Truncated"):
            parse_manufacturer_data(bytes(buf))

    # Random corrupt bytes of various lengths >= 19
    for length in [19, 23, 24, 30, 50]:
        buf = bytearray([0xFF] * length)
        # Should parse safely without throwing unexpected exceptions
        info = parse_manufacturer_data(bytes(buf))
        assert info is not None
        assert isinstance(info.type, int)


@pytest.mark.asyncio
async def test_adversarial_notification_handler_fuzzing():
    """Fuzz notification handler with malformed, oversized, and truncated frames."""
    dev = make_controller(ACInfinityDevice)

    # Completely corrupt or truncated packets
    for bad_data in [
        b"",
        b"\x00",
        b"\xff\xff",
        b"\x1e\xff",  # truncated status frame
        b"\x1e\xff\x09\x02\x03\x0c",  # partial status frame
        b"\xa5\x00",  # incomplete command header
        b"\xa5\x00\xff\xff\x00\x01\x00\x00",  # huge length claim
        b"\xa5\x00\x00\x02\x00\x01\x12\x34\x00\x01\x10\x01",  # bad CRC
    ]:
        # Must not raise an unhandled exception
        dev._notification_handler(1, bytearray(bad_data))

    assert dev.last_command_success is True


# ==============================================================================
# CHALLENGE 4: Lock Hierarchy and Cancellation Safety
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_command_turn_cancellation_safety():
    """Verify that cancelling a task waiting in _command_turn does not desync commands_in_line."""
    dev = make_controller(ACInfinityDevice)
    events = []

    # Mock _execute_disconnect
    async def mock_disconnect():
        events.append("disconnect")

    dev._execute_disconnect = AsyncMock(side_effect=mock_disconnect)

    task1_entered = asyncio.Event()
    task1_hold = asyncio.Event()

    async def task1():
        async with dev._command_turn():
            events.append("task1_start")
            task1_entered.set()
            await task1_hold.wait()
            events.append("task1_end")

    async def task2():
        async with dev._command_turn():
            events.append("task2_ran")

    # Start task 1
    t1 = asyncio.create_task(task1())
    await task1_entered.wait()
    assert dev._commands_in_line == 1

    # Start task 2 which will queue up waiting for _command_lock
    t2 = asyncio.create_task(task2())
    await asyncio.sleep(0.01)
    assert dev._commands_in_line == 2

    # Cancel task 2 while it is waiting in line
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2

    # _commands_in_line must have decremented back to 1
    assert dev._commands_in_line == 1
    # task 1 is still holding the connection, so disconnect should NOT have fired yet
    assert "disconnect" not in events

    # Now let task 1 complete
    task1_hold.set()
    await t1

    # Now _commands_in_line is 0, and disconnect fired!
    assert dev._commands_in_line == 0
    assert "disconnect" in events


@pytest.mark.asyncio
async def test_adversarial_notify_future_cancelled_cleans_up():
    """Verify that cancellation of a waiting command cancels _notify_future cleanly."""
    dev = make_controller()
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"])
    dev._write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write"])
    dev._client = SimpleNamespace(write_gatt_char=AsyncMock(), is_connected=True)

    command = Protocol().get_model_data(6, 0, 1)

    task = asyncio.create_task(dev._execute_command_locked(command))
    await asyncio.sleep(0.01)

    assert dev._notify_future is not None
    assert not dev._notify_future.done()

    # Cancel the task awaiting the command response
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Future must be cleared and cleaned up
    assert dev._notify_future is None
    assert dev._pending_sequence is None
    assert dev._pending_command is None


# ==============================================================================
# CHALLENGE 5: Bounded Async Timeouts
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_all_ble_timeouts_enforced():
    """Verify bounded timeouts on command write, response wait, and diagnostics."""
    dev = make_controller()
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"])
    dev._write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write"])

    # 1. Test write_gatt_char timeout: if write hangs, it must raise BLEStepError within GATT_TIMEOUT
    async def hanging_write(*args, **kwargs):
        await asyncio.sleep(GATT_TIMEOUT + 5)

    dev._client = SimpleNamespace(
        write_gatt_char=hanging_write,
        read_gatt_char=AsyncMock(),
        is_connected=True,
    )

    with pytest.raises(BLEStepError) as exc_info:
        # Wrap with a slightly longer outer timeout to catch if GATT_TIMEOUT fires first
        async with asyncio.timeout(GATT_TIMEOUT + 2):
            await dev._execute_command_locked(Protocol().get_model_data(6, 0, 1))

    assert exc_info.value.code in ("write_failed", "connect_failed")


@pytest.mark.asyncio
async def test_adversarial_response_timeout_enforced():
    """Verify that if the peripheral never sends a notification, RESPONSE_TIMEOUT (8s) triggers."""
    dev = make_controller()
    dev._read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"])
    dev._write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write"])
    dev._client = SimpleNamespace(
        write_gatt_char=AsyncMock(),
        read_gatt_char=AsyncMock(),
        is_connected=True,
    )

    with pytest.raises(BLEStepError) as exc_info:
        async with asyncio.timeout(RESPONSE_TIMEOUT + 2):
            await dev._execute_command_locked(Protocol().get_model_data(6, 0, 1))

    assert exc_info.value.code == "no_response"
    assert "response wait" in exc_info.value.step


@pytest.mark.asyncio
async def test_adversarial_safe_disconnect_timeout_enforced():
    """Verify that _safe_disconnect_client finishes within bounded time even if client hangs."""
    dev = make_controller()

    async def hanging_disconnect():
        await asyncio.sleep(DISCONNECT_TIMEOUT + 5)

    client = SimpleNamespace(
        stop_notify=AsyncMock(side_effect=hanging_disconnect),
        disconnect=AsyncMock(side_effect=hanging_disconnect),
        is_connected=True,
    )

    read_c = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify"])

    # Must complete within ~ 2 * DISCONNECT_TIMEOUT without hanging indefinitely
    async with asyncio.timeout(DISCONNECT_TIMEOUT * 2 + 2):
        await dev._safe_disconnect_client(client, read_c)


@pytest.mark.asyncio
async def test_adversarial_mtu_negotiation_hang_investigation():
    """Audit whether _negotiate_mtu is bounded if client.request_mtu hangs."""
    dev = make_controller()

    async def hanging_request_mtu(mtu):
        await asyncio.sleep(30)
        return mtu

    client = SimpleNamespace(
        request_mtu=hanging_request_mtu,
        mtu_size=23,
    )

    # Let's test if _negotiate_mtu hangs when request_mtu hangs:
    # If it is not bounded by a timeout, awaiting it will take 30s.
    start = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(2.0):
            await dev._negotiate_mtu(client)
        elapsed = asyncio.get_running_loop().time() - start
        hung = False
    except TimeoutError:
        hung = True
        elapsed = asyncio.get_running_loop().time() - start

    # Documenting empirical finding: if client.request_mtu returns a hanging coroutine,
    # does _negotiate_mtu time out on its own or does it hang until cancelled?
    # We verify this empirically.
    assert hung, f"Expected _negotiate_mtu to hang when request_mtu hangs, but finished in {elapsed:.2f}s"
