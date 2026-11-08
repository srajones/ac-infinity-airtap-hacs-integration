"""Adversarial stress-test suite for UI Config Flow & Stranger Device rejection.

Authored by Challenger 2 for the Final Codebase Audit Pass.
Stress-tests:
1. Stranger device rejection under adversarial advertisement variations.
2. MAC address validation fuzzing (malformed, delimiters, injection, boundaries).
3. Empty discovery fallback behavior and duplicate suppression.
4. Multi-device batch "__all__" handling, subflow task creation, and filtering.
5. OptionsFlow validation and range boundaries.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.ac_infinity_airtap_ble.const import (
    CONF_PASSIVE_ONLY,
    CONF_SCAN_INTERVAL,
)
from custom_components.ac_infinity_airtap_ble.device import DeviceInfoEx
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import MANUFACTURER_ID


@pytest.fixture
def flow_module(monkeypatch):
    """Provide isolated config_flow module with HA stubs."""
    import homeassistant

    entries = ModuleType("homeassistant.config_entries")

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

    class OptionsFlow:
        def __init__(self):
            pass

        def async_show_form(self, **kwargs):
            return kwargs

        def async_create_entry(self, **kwargs):
            return kwargs

    entries.ConfigFlow = ConfigFlow
    entries.OptionsFlow = OptionsFlow
    entries.SOURCE_BLUETOOTH = "bluetooth"
    entries.ConfigEntry = SimpleNamespace

    monkeypatch.setattr(homeassistant, "config_entries", entries, raising=False)
    modules = {
        "homeassistant.config_entries": entries,
        "homeassistant.components": ModuleType("homeassistant.components"),
        "homeassistant.components.bluetooth": SimpleNamespace(
            BluetoothServiceInfoBleak=object,
            async_ble_device_from_address=lambda *args: None,
            async_discovered_service_info=lambda *args, **kwargs: [],
            async_last_service_info=lambda *args, **kwargs: None,
        ),
        "homeassistant.const": SimpleNamespace(
            CONF_ADDRESS="address",
            CONF_SERVICE_DATA="service_data",
        ),
        "homeassistant.core": SimpleNamespace(callback=lambda f: f),
        "homeassistant.data_entry_flow": SimpleNamespace(FlowResult=dict),
        "custom_components.ac_infinity_airtap_ble.legacy": SimpleNamespace(
            async_adopt_legacy_entry=AsyncMock(),
            async_find_legacy_entry=lambda *args: None,
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(
        sys.modules, "custom_components.ac_infinity_airtap_ble.config_flow", raising=False
    )
    return importlib.import_module("custom_components.ac_infinity_airtap_ble.config_flow")


def make_flow(module):
    flow = module.ConfigFlow()
    flow.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda coro: coro.close() if hasattr(coro, "close") else None),
        config_entries=SimpleNamespace(
            flow=SimpleNamespace(
                async_init=AsyncMock(return_value={"type": "form"}),
            )
        ),
    )
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = lambda: None
    flow._async_current_ids = lambda: set()
    flow.async_create_entry = lambda **kwargs: kwargs
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_abort = lambda **kwargs: kwargs
    return flow


def build_mfr_payload(
    dev_type: int = 6,
    version: int = 3,
    serial: bytes = b"8DAQJ",
    tmp_c: float = 21.5,
    length: int = 20,
) -> bytes:
    data = bytearray(max(length, 20))
    data[6:11] = serial
    data[11] = version
    data[12] = dev_type
    data[13] = 0
    data[14:16] = int(round(tmp_c * 100)).to_bytes(2, "big")
    data[16:18] = (5000).to_bytes(2, "big")
    data[18] = 4
    return bytes(data[:length])


def make_service_info(
    address: str = "A4:C1:38:F6:48:F6",
    name: str = "BLE_FAN",
    mfr_id: int | None = MANUFACTURER_ID,
    mfr_payload: bytes | None = None,
    service_uuids: list[str] | None = None,
):
    if mfr_payload is None and mfr_id == MANUFACTURER_ID:
        mfr_payload = build_mfr_payload()
    mfr_dict = {mfr_id: mfr_payload} if mfr_id is not None and mfr_payload is not None else {}
    advert = SimpleNamespace(
        manufacturer_data=mfr_dict,
        service_uuids=service_uuids or [],
    )
    device = SimpleNamespace(name=name, address=address)
    return SimpleNamespace(
        address=address,
        name=name,
        device=device,
        advertisement=advert,
    )


# ==============================================================================
# Adversarial Challenge 1: Stranger Device Filtering & Stress
# ==============================================================================


@pytest.mark.parametrize(
    "stranger_mfr_id",
    [0x004C, 0x0006, 0x0075, 0x0001, 0x0100, 0xFFFF, 2305, 2307, 0],
)
def test_adversarial_stranger_company_ids_rejected(flow_module, stranger_mfr_id):
    """Verify that any advertisement not bearing company ID 2306 (0x0902) is rejected."""
    payload = build_mfr_payload()
    info = make_service_info(mfr_id=stranger_mfr_id, mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize("payload_len", [0, 1, 5, 10, 15, 18])
def test_adversarial_underlength_payloads_rejected(flow_module, payload_len):
    """Payloads shorter than 19 bytes must be strictly rejected."""
    raw = b"\x00" * payload_len
    info = make_service_info(mfr_payload=raw)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize(
    "adversarial_serial",
    [
        b"\x00\x00\x00\x00\x00",  # Null bytes
        b"8D\x00QJ",              # Embedded null
        b"8D QJ",                  # Space
        b"8D\nQJ",                 # Newline
        b"8D\tQJ",                 # Tab
        b"!@#$%",                  # Punctuation symbols
        b"<<>>?",                  # HTML-like brackets
        b"\xff\xfe\xfd\xfc\xfb",  # High-byte non-ASCII
        b"\x80\x81\x82\x83\x84",  # Non-ASCII UTF-8 fragments
        b"     ",                  # All spaces
        b"8-DAQ",                  # Hyphen
        b"8.DAQ",                  # Period
    ],
)
def test_adversarial_malformed_serials_rejected(flow_module, adversarial_serial):
    """Serials containing non-alphanumeric or unprintable characters must be rejected."""
    payload = build_mfr_payload(serial=adversarial_serial)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize(
    "unsupported_model_type",
    [0, 99, 100, 150, 200, 255],
)
def test_adversarial_unsupported_model_types_rejected(flow_module, unsupported_model_type):
    """Model types not present in DEVICE_MODEL must be rejected."""
    payload = build_mfr_payload(dev_type=unsupported_model_type)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize(
    "bogus_version",
    [0, 21, 22, 50, 100, 255],
)
def test_adversarial_bogus_version_rejected(flow_module, bogus_version):
    """Device versions outside [1..20] must be rejected."""
    payload = build_mfr_payload(version=bogus_version)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


def test_adversarial_fuzzed_corrupted_payload_handled_safely(flow_module):
    """A completely random 20-byte payload should not cause an unhandled exception."""
    for byte_val in [0x00, 0xFF, 0x7F, 0x80]:
        info = make_service_info(mfr_payload=bytes([byte_val] * 20))
        assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.asyncio
async def test_adversarial_bluetooth_discovery_aborts_on_stranger(flow_module):
    """Bluetooth discovery aborts with unsupported_model on stranger device."""
    flow = make_flow(flow_module)
    stranger = make_service_info(
        address="11:22:33:44:55:66",
        name="Random Beacon",
        mfr_id=0x004C,
        mfr_payload=b"\x02\x15" + b"\x00" * 18,
    )
    res = await flow.async_step_bluetooth(stranger)
    assert res.get("reason") == "unsupported_model"


# ==============================================================================
# Adversarial Challenge 2: MAC Address Fuzzing & Manual Validation
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fuzzed_mac",
    [
        "",
        " ",
        "   ",
        "\t",
        "\n",
        "A4:C1:38:F6:48",            # 5 bytes
        "A4:C1:38:F6:48:F6:77",      # 7 bytes
        "A4:C1:38:F6:48:F",          # Incomplete nibble
        "A4:C1:38:F6:48:FG",         # Non-hex G
        "ZZ:YY:XX:WW:VV:UU",         # Completely non-hex
        "A4.C1.38.F6.48.F6",         # Dot delimiter
        "A4_C1_38_F6_48_F6",         # Underscore delimiter
        "A4C138F648F6",              # No delimiters
        "00:00:00:00:00:00\x00",     # Null byte injected
        "A4:C1:38:F6:48:F6; rm -rf", # Command injection attempt
        "<script>alert(1)</script>", # XSS payload
        "A4::38:F6:48:F6",           # Missing byte
        ":A4:C1:38:F6:48:F6",        # Leading colon
        "A4:C1:38:F6:48:F6:",        # Trailing colon
    ],
)
async def test_adversarial_fuzzed_mac_validation(flow_module, fuzzed_mac):
    """Ensure every malformed MAC string results in errors['base'] == 'invalid_address'."""
    flow = make_flow(flow_module)
    res = await flow.async_step_manual({"address": fuzzed_mac})
    assert res["step_id"] == "manual"
    assert res["errors"]["base"] == "invalid_address"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "valid_input, expected_normalized",
    [
        ("A4:C1:38:F6:48:F6", "A4:C1:38:F6:48:F6"),
        ("a4:c1:38:f6:48:f6", "A4:C1:38:F6:48:F6"),
        ("A4-C1-38-F6-48-F6", "A4:C1:38:F6:48:F6"),
        ("a4-c1-38-f6-48-f6", "A4:C1:38:F6:48:F6"),
        ("A4:C1:38-F6:48-F6", "A4:C1:38:F6:48:F6"),  # Mixed delimiters normalized cleanly
        ("  A4:C1:38:F6:48:F6  ", "A4:C1:38:F6:48:F6"),
        ("a4:C1:38:f6:48:F6", "A4:C1:38:F6:48:F6"),
    ],
)
async def test_adversarial_valid_mac_normalization(
    flow_module, monkeypatch, valid_input, expected_normalized
):
    """Ensure valid MAC addresses with hyphens, lowercase, and whitespace are normalized."""
    flow = make_flow(flow_module)
    ble_device = SimpleNamespace(name="vent", address=expected_normalized)
    monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: ble_device)
    monkeypatch.setattr(flow_module, "async_last_service_info", lambda *args, **kwargs: None)

    controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    res = await flow.async_step_manual({"address": valid_input})
    assert res["data"]["address"] == expected_normalized


# ==============================================================================
# Adversarial Challenge 3: Discovery Fallbacks & Stranger Suppression
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_discovery_with_only_strangers_falls_back_to_manual(
    flow_module, monkeypatch
):
    """If 10 stranger devices are advertising, discovery dropdown must not show them."""
    flow = make_flow(flow_module)
    strangers = [
        make_service_info(
            address=f"00:11:22:33:44:{i:02X}",
            name=f"Stranger_{i}",
            mfr_id=0x004C,
            mfr_payload=b"\x00" * 20,
        )
        for i in range(10)
    ]
    monkeypatch.setattr(flow_module, "async_discovered_service_info", lambda *args, **kwargs: strangers)

    res = await flow.async_step_user()
    assert res["step_id"] == "manual"


@pytest.mark.asyncio
async def test_adversarial_already_configured_devices_excluded_from_discovery(
    flow_module, monkeypatch
):
    """Configured devices in _async_current_ids() must be excluded from discovery."""
    flow = make_flow(flow_module)
    dev1 = make_service_info(
        address="A4:C1:38:F6:48:F6",
        name="BLE_FAN",
        mfr_payload=build_mfr_payload(dev_type=6, serial=b"8DAQJ"),
    )
    monkeypatch.setattr(flow_module, "async_discovered_service_info", lambda *args, **kwargs: [dev1])
    flow._async_current_ids = lambda: {"A4:C1:38:F6:48:F6"}

    # Since dev1 is already configured, valid_devices is empty -> fallback to manual
    res = await flow.async_step_user()
    assert res["step_id"] == "manual"


# ==============================================================================
# Adversarial Challenge 4: Multi-Device Batch Selection (__all__)
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_batch_add_all_with_stranger_mixed_in(flow_module, monkeypatch):
    """When valid and stranger devices are discovered, __all__ only sets up valid ones."""
    flow = make_flow(flow_module)

    valid1 = make_service_info(
        address="A4:C1:38:00:00:01",
        name="FAN_1",
        mfr_payload=build_mfr_payload(dev_type=6, serial=b"SER01"),
    )
    valid2 = make_service_info(
        address="A4:C1:38:00:00:02",
        name="FAN_2",
        mfr_payload=build_mfr_payload(dev_type=48, serial=b"SER02"),
    )
    stranger = make_service_info(
        address="B0:B1:B2:B3:B4:B5",
        name="AppleTV",
        mfr_id=0x004C,
        mfr_payload=b"\x00" * 20,
    )

    monkeypatch.setattr(
        flow_module,
        "async_discovered_service_info",
        lambda *args, **kwargs: [valid1, stranger, valid2],
    )

    # Initial user step renders dropdown
    res = await flow.async_step_user()
    assert res["step_id"] == "user"
    schema = res["data_schema"].schema
    addr_field = next(k for k in schema if k == "address")
    options = schema[addr_field].container

    assert "__all__" in options
    # Exactly 2 valid devices, stranger ignored!
    assert options["__all__"] == "Add all discovered AC Infinity devices (2)"
    assert "A4:C1:38:00:00:01" in options
    assert "A4:C1:38:00:00:02" in options
    assert "B0:B1:B2:B3:B4:B5" not in options

    # Select __all__
    controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-SER01"),
        name="D-SER01",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    res_all = await flow.async_step_user({"address": "__all__"})
    assert res_all["title"] == "D-SER01"
    assert res_all["data"]["address"] == "A4:C1:38:00:00:01"

    # Exactly 1 subflow task created (for valid2), stranger NEVER spawned!
    assert flow.hass.async_create_task.call_count == 1
    call_args = flow.hass.config_entries.flow.async_init.call_args[1]
    assert call_args["data"].address == "A4:C1:38:00:00:02"


# ==============================================================================
# Adversarial Challenge 5: OptionsFlow Boundary Stress
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_options_flow_schema_and_boundaries(flow_module):
    """Stress-test OptionsFlow validation boundaries."""
    entry = SimpleNamespace(options={}, data={})
    handler = flow_module.ACInfinityOptionsFlowHandler(entry)

    # Valid configurations
    res = await handler.async_step_init()
    schema = res["data_schema"]

    # Valid within [5, 300]
    for valid_interval in [5, 60, 300]:
        val = schema({CONF_SCAN_INTERVAL: valid_interval, CONF_PASSIVE_ONLY: True})
        assert val[CONF_SCAN_INTERVAL] == valid_interval
        assert val[CONF_PASSIVE_ONLY] is True

    # Coercion from string
    val = schema({CONF_SCAN_INTERVAL: "15", CONF_PASSIVE_ONLY: False})
    assert val[CONF_SCAN_INTERVAL] == 15

    # Out of bounds: < 5
    with pytest.raises(vol.Invalid):
        schema({CONF_SCAN_INTERVAL: 4})

    # Out of bounds: > 300
    with pytest.raises(vol.Invalid):
        schema({CONF_SCAN_INTERVAL: 301})

    # Non-integer / invalid string
    with pytest.raises(vol.Invalid):
        schema({CONF_SCAN_INTERVAL: "not_a_number"})
