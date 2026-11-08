"""Run the real config flow against lightweight HA interface stubs (no HA runtime)."""
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ac_infinity_airtap_ble.const import (
    AC_INFINITY_SERVICE_UUID,
    CONF_PASSIVE_ONLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
)

from custom_components.ac_infinity_airtap_ble.device import DeviceInfoEx
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import MANUFACTURER_ID
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError


@pytest.fixture
def flow_module(monkeypatch):
    import homeassistant
    entries = ModuleType("homeassistant.config_entries")

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

    class OptionsFlow:
        def __init__(self, config_entry=None):
            self.config_entry = config_entry

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
    """Build a valid AC Infinity manufacturer advertisement payload."""
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
    """Create a mock BluetoothServiceInfoBleak for testing."""
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
# 1. Strict AC Infinity Validator Tests (Zero Accidental Adds)
# ==============================================================================

def test_validator_valid_device(flow_module):
    """Test validator accepts valid AC Infinity advertisement."""
    info = make_service_info(mfr_payload=build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ"))
    assert flow_module.is_valid_ac_infinity_advertisement(info) is True


@pytest.mark.parametrize(
    "invalid_info",
    [
        None,
        SimpleNamespace(advertisement=None),
        SimpleNamespace(advertisement=SimpleNamespace(manufacturer_data={})),
        SimpleNamespace(advertisement=SimpleNamespace(manufacturer_data=None)),
    ],
)
def test_validator_none_or_missing_advert(flow_module, invalid_info):
    """Test validator rejects None or empty advertisements."""
    assert flow_module.is_valid_ac_infinity_advertisement(invalid_info) is False


def test_validator_wrong_company_id(flow_module):
    """Test validator rejects advertisements from other vendors (e.g., Apple 0x004C)."""
    info = make_service_info(mfr_id=76, mfr_payload=b"\x02\x15\x01\x02\x03\x04")
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


def test_validator_truncated_payload(flow_module):
    """Test validator rejects payloads shorter than 19 bytes."""
    payload = build_mfr_payload(length=18)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize(
    "bad_serial",
    [
        b"\x00\x00\x00\x00\x00",
        b"!@#$%",
        b"8D QJ",
        b"\xff\xfe\xfd\xfc\xfb",
        b"8D\x00QJ",
    ],
)
def test_validator_invalid_serial_bytes(flow_module, bad_serial):
    """Test validator rejects non-alphanumeric or unprintable serial characters."""
    payload = build_mfr_payload(serial=bad_serial)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize("bogus_type", [99, 150, 255])
def test_validator_unsupported_device_type(flow_module, bogus_type):
    """Test validator rejects device types not in DEVICE_MODEL."""
    payload = build_mfr_payload(dev_type=bogus_type)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


@pytest.mark.parametrize("bad_version", [0, 21, 255])
def test_validator_insane_version(flow_module, bad_version):
    """Test validator rejects bogus protocol versions outside 1..20."""
    payload = build_mfr_payload(version=bad_version)
    info = make_service_info(mfr_payload=payload)
    assert flow_module.is_valid_ac_infinity_advertisement(info) is False


# ==============================================================================
# 2. UI Discovery Screen Tests (async_step_user)
# ==============================================================================

@pytest.mark.asyncio
async def test_user_step_single_device_formatted_dropdown(flow_module, monkeypatch):
    """Test user step with a single valid discovered device."""
    flow = make_flow(flow_module)
    dev1 = make_service_info(
        address="A4:C1:38:F6:48:F6",
        name="BLE_FAN",
        mfr_payload=build_mfr_payload(dev_type=6, serial=b"8DAQJ"),
    )
    monkeypatch.setattr(flow_module, "async_discovered_service_info", lambda *args, **kwargs: [dev1])

    result = await flow.async_step_user()
    assert result["step_id"] == "user"
    schema = result["data_schema"].schema
    addr_field = next(k for k in schema if k == "address")
    options = schema[addr_field].container

    # Formatted as: Name (Address) — Model
    assert "A4:C1:38:F6:48:F6" in options
    assert options["A4:C1:38:F6:48:F6"] == "BLE_FAN (A4:C1:38:F6:48:F6) — Airtap Series"
    assert "__manual__" in options
    assert "__all__" not in options  # Only 1 device discovered
    assert addr_field.default() == "A4:C1:38:F6:48:F6"


@pytest.mark.asyncio
async def test_user_step_multiple_devices_adds_all_option(flow_module, monkeypatch):
    """Test user step with >= 2 valid devices includes the __all__ option."""
    flow = make_flow(flow_module)
    dev1 = make_service_info(
        address="A4:C1:38:F6:48:F6",
        name="BLE_FAN",
        mfr_payload=build_mfr_payload(dev_type=6, serial=b"8DAQJ"),
    )
    dev2 = make_service_info(
        address="A4:C1:38:11:22:33",
        name="BLE_FAN_2",
        mfr_payload=build_mfr_payload(dev_type=48, serial=b"9EBBM"),
    )
    monkeypatch.setattr(flow_module, "async_discovered_service_info", lambda *args, **kwargs: [dev1, dev2])

    result = await flow.async_step_user()
    assert result["step_id"] == "user"
    schema = result["data_schema"].schema
    addr_field = next(k for k in schema if k == "address")
    options = schema[addr_field].container



    assert "__all__" in options
    assert options["__all__"] == "Add all discovered AC Infinity devices (2)"
    assert "A4:C1:38:F6:48:F6" in options
    assert "A4:C1:38:11:22:33" in options
    assert "__manual__" in options


@pytest.mark.asyncio
async def test_user_step_select_all_spawns_subflows(flow_module, monkeypatch):
    """Test selecting __all__ sets up the first device and spawns flows for remaining."""
    flow = make_flow(flow_module)
    dev1 = make_service_info(
        address="A4:C1:38:F6:48:F6",
        name="BLE_FAN",
        mfr_payload=build_mfr_payload(dev_type=6, serial=b"8DAQJ"),
    )
    dev2 = make_service_info(
        address="A4:C1:38:11:22:33",
        name="BLE_FAN_2",
        mfr_payload=build_mfr_payload(dev_type=48, serial=b"9EBBM"),
    )
    flow._discovered_devices = {dev1.address: dev1, dev2.address: dev2}

    controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    result = await flow.async_step_user({"address": "__all__"})
    assert result["title"] == "D-8DAQJ"
    assert result["data"]["address"] == "A4:C1:38:F6:48:F6"
    flow.hass.async_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_user_step_select_manual_redirects_to_manual_step(flow_module):
    """Test selecting __manual__ transfers to async_step_manual."""
    flow = make_flow(flow_module)
    result = await flow.async_step_user({"address": "__manual__"})
    assert result["step_id"] == "manual"


@pytest.mark.asyncio
async def test_user_step_empty_discovery_clean_fallback(flow_module, monkeypatch):
    """Test user step cleanly falls back to manual entry when no devices are found."""
    flow = make_flow(flow_module)
    monkeypatch.setattr(flow_module, "async_discovered_service_info", lambda *args, **kwargs: [])

    result = await flow.async_step_user()
    assert result["step_id"] == "manual"


@pytest.mark.asyncio
async def test_user_step_filters_stranger_devices(flow_module, monkeypatch):
    """Test non-AC-Infinity BLE devices are strictly excluded from the discovery dropdown."""
    flow = make_flow(flow_module)
    stranger = make_service_info(
        address="B0:B1:B2:B3:B4:B5",
        name="Apple Watch",
        mfr_id=76,  # Apple
        mfr_payload=b"\x10\x05\x01\x02\x03\x04",
    )
    monkeypatch.setattr(flow_module, "async_discovered_service_info", lambda *args, **kwargs: [stranger])

    result = await flow.async_step_user()
    # Since only a stranger device was present, it must be ignored and fall back to manual
    assert result["step_id"] == "manual"


@pytest.mark.asyncio
async def test_bluetooth_step_rejects_stranger(flow_module):
    """Test bluetooth discovery step aborts if incoming advertisement is not AC Infinity."""
    flow = make_flow(flow_module)
    stranger = make_service_info(
        address="B0:B1:B2:B3:B4:B5",
        name="Tile Tracker",
        mfr_id=999,
        mfr_payload=b"\x01\x02\x03",
    )
    result = await flow.async_step_bluetooth(stranger)
    assert result["type"] if isinstance(result, dict) and "type" in result else True
    assert result.get("reason") == "unsupported_model"


# ==============================================================================
# 3. Manual Step Tests (async_step_manual)
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_mac",
    [
        "invalid_mac",
        "A4:C1:38:F6:48",
        "A4:C1:38:F6:48:F6:12",
        "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ",
        "A4-C1-38-F6-48",
        "",
        "   ",
    ],
)
async def test_manual_step_invalid_mac_format(flow_module, invalid_mac):
    """Test manual step rejects malformed Bluetooth MAC addresses with invalid_address error."""
    flow = make_flow(flow_module)
    result = await flow.async_step_manual({"address": invalid_mac})
    assert result["errors"]["base"] == "invalid_address"


@pytest.mark.asyncio
async def test_manual_step_normalizes_hyphens_and_case(flow_module, monkeypatch):
    """Test manual step normalizes a valid MAC with hyphens and lowercase."""
    flow = make_flow(flow_module)
    ble_device = SimpleNamespace(name="vent", address="A4:C1:38:F6:48:F6")
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

    result = await flow.async_step_manual({"address": "a4-c1-38-f6-48-f6"})
    assert result["data"]["address"] == "A4:C1:38:F6:48:F6"


@pytest.mark.asyncio
async def test_manual_step_no_hardcoded_stranger_placeholder(flow_module):
    """Test manual step schema has no hardcoded stranger MAC placeholder."""
    flow = make_flow(flow_module)
    result = await flow.async_step_manual()
    schema = result["data_schema"].schema
    addr_field = next(k for k in schema if k == "address")
    # Verify the old stranger default "A4:C1:38:41:4C:80" is NOT used
    default_val = addr_field.default() if callable(getattr(addr_field, "default", None)) else getattr(addr_field, "default", None)
    assert default_val != "A4:C1:38:41:4C:80"


@pytest.mark.asyncio
async def test_manual_step_rejects_cached_stranger_device(flow_module, monkeypatch):
    """Test manual step rejects a MAC if its cached advertisement is from a stranger vendor."""
    flow = make_flow(flow_module)
    stranger = make_service_info(
        address="A4:C1:38:F6:48:F6",
        name="Unknown Speaker",
        mfr_id=76,
        mfr_payload=b"\x01\x02\x03\x04",
        service_uuids=["0000180d-0000-1000-8000-00805f9b34fb"],
    )
    monkeypatch.setattr(flow_module, "async_last_service_info", lambda *args, **kwargs: stranger)

    result = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert result["errors"]["base"] == "not_ac_infinity_device"


@pytest.mark.asyncio
async def test_manual_step_accepts_device_with_ac_service_uuid(flow_module, monkeypatch):
    """Test manual step accepts a device with AC Infinity service UUID even if mfr data is missing."""
    flow = make_flow(flow_module)
    ac_device_info = make_service_info(
        address="A4:C1:38:F6:48:F6",
        name="AirTap",
        mfr_id=None,
        service_uuids=[AC_INFINITY_SERVICE_UUID],
    )
    ble_device = SimpleNamespace(name="vent", address="A4:C1:38:F6:48:F6")
    monkeypatch.setattr(flow_module, "async_last_service_info", lambda *args, **kwargs: ac_device_info)
    monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: ble_device)

    controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    result = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert result["data"]["address"] == "A4:C1:38:F6:48:F6"


# ==============================================================================
# 4. Options Flow Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_options_flow_handler_initialization(flow_module):
    """Test async_get_options_flow returns an ACInfinityOptionsFlowHandler."""
    entry = SimpleNamespace(options={}, data={})
    handler = flow_module.ConfigFlow.async_get_options_flow(entry)
    assert isinstance(handler, flow_module.ACInfinityOptionsFlowHandler)


@pytest.mark.asyncio
async def test_options_flow_step_init_shows_form(flow_module):
    """Test options flow shows form with default scan interval and passive options."""
    entry = SimpleNamespace(options={}, data={})
    handler = flow_module.ACInfinityOptionsFlowHandler(entry)

    result = await handler.async_step_init()
    assert result["step_id"] == "init"
    schema = result["data_schema"].schema

    scan_field = next(k for k in schema if k == CONF_SCAN_INTERVAL)
    passive_field = next(k for k in schema if k == CONF_PASSIVE_ONLY)

    assert scan_field.default() == DEFAULT_SCAN_INTERVAL
    assert passive_field.default() is False


@pytest.mark.asyncio
async def test_options_flow_step_init_saves_user_input(flow_module):
    """Test options flow correctly saves configured options."""
    entry = SimpleNamespace(options={CONF_SCAN_INTERVAL: 15, CONF_PASSIVE_ONLY: False}, data={})
    handler = flow_module.ACInfinityOptionsFlowHandler(entry)

    user_input = {CONF_SCAN_INTERVAL: 30, CONF_PASSIVE_ONLY: True}
    result = await handler.async_step_init(user_input)
    assert result["data"] == user_input


# ==============================================================================
# 5. Translation & Strings Tests
# ==============================================================================

def test_all_error_keys_have_english_translations():
    """Test that all error keys in strings.json and translations/en.json match identically."""
    root = Path(__file__).resolve().parents[1] / "custom_components/ac_infinity_airtap_ble"
    strings = json.loads((root / "strings.json").read_text())
    english = json.loads((root / "translations/en.json").read_text())

    assert strings["config"]["error"] == english["config"]["error"]
    expected_keys = {
        "cannot_connect",
        "connect_failed",
        "disconnect_failed",
        "initialization_failed",
        "invalid_address",
        "invalid_response",
        "no_response",
        "not_ac_infinity_device",
        "notify_failed",
        "service_failed",
        "unknown",
        "unsupported_model",
        "write_failed",
    }
    assert expected_keys <= strings["config"]["error"].keys()


def test_user_and_manual_step_descriptions_exist():
    """Test user-facing descriptions exist for user and manual configuration steps."""
    root = Path(__file__).resolve().parents[1] / "custom_components/ac_infinity_airtap_ble"
    strings = json.loads((root / "strings.json").read_text())
    english = json.loads((root / "translations/en.json").read_text())

    assert "description" in strings["config"]["step"]["user"]
    assert "description" in english["config"]["step"]["user"]
    assert "description" in strings["config"]["step"]["manual"]
    assert "description" in english["config"]["step"]["manual"]

    assert "options" in strings and "step" in strings["options"]
    assert "options" in english and "step" in english["options"]


# ==============================================================================
# 6. Legacy and Existing Protocol Tests
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_discovery_saves_queried_state_as_json_not_advertisement_dataclass(
    flow_module, monkeypatch, manual
):
    flow = make_flow(flow_module)
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="vent"),
        advertisement=object(),
    )
    flow._discovered_devices[discovery.address] = discovery
    state = DeviceInfoEx(
        type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0
    )
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)
    if manual:
        monkeypatch.setattr(
            flow_module, "async_ble_device_from_address", lambda *args: discovery.device
        )
        entry = await flow.async_step_manual({"address": discovery.address})
    else:
        entry = await flow.async_step_user({"address": discovery.address})
    assert entry["data"]["service_data"]["level_on"] == 5
    assert entry["data"]["protocol_variant"] == "android_2_1_2_telink"
    json.dumps(entry["data"])
    controller.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "connect_failed",
        "notify_failed",
        "no_response",
        "invalid_response",
        "unsupported_model",
    ],
)
async def test_manual_setup_shows_specific_failure(flow_module, monkeypatch, code):
    flow = make_flow(flow_module)
    monkeypatch.setattr(
        flow_module,
        "async_ble_device_from_address",
        lambda *args: SimpleNamespace(name="vent"),
    )
    controller = SimpleNamespace(
        update=AsyncMock(side_effect=BLEStepError("test", code, "test"))
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)
    form = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert form["errors"]["base"] == code


@pytest.mark.asyncio
async def test_setup_validation_decoupled_with_advertisement_fallback_user_step(
    flow_module, monkeypatch
):
    flow = make_flow(flow_module)

    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=21.5)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="vent"),
        advertisement=advert,
    )
    flow._discovered_devices[discovery.address] = discovery

    state = DeviceInfoEx(
        type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0
    )
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=TimeoutError("GATT update timed out")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    entry = await flow.async_step_user({"address": discovery.address})
    assert entry["type"] if isinstance(entry, dict) and "type" in entry else True
    assert entry["data"]["service_data"]["tmp"] == 21.5
    controller.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_validation_decoupled_with_advertisement_fallback_manual_step(
    flow_module, monkeypatch
):
    flow = make_flow(flow_module)

    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=23.0)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    ble_device = SimpleNamespace(name="vent")
    service_info = SimpleNamespace(advertisement=advert)

    monkeypatch.setattr(
        flow_module, "async_ble_device_from_address", lambda *args: ble_device
    )
    monkeypatch.setattr(
        flow_module, "async_last_service_info", lambda *args, **kwargs: service_info
    )

    state = DeviceInfoEx(
        type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=23.0
    )
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=BLEStepError("connect", "connect_failed", "timeout")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    entry = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert entry["data"]["service_data"]["tmp"] == 23.0
    controller.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_setup_without_advertisement_data_fails_on_gatt_timeout(
    flow_module, monkeypatch
):
    flow = make_flow(flow_module)
    ble_device = SimpleNamespace(name="vent")

    monkeypatch.setattr(
        flow_module, "async_ble_device_from_address", lambda *args: ble_device
    )
    monkeypatch.setattr(flow_module, "async_last_service_info", lambda *args, **kwargs: None)

    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ")
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=BLEStepError("connect", "connect_failed", "timeout")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    result = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert result["errors"]["base"] == "connect_failed"
    controller.stop.assert_awaited_once()
