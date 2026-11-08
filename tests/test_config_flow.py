"""Run the real config flow against lightweight HA interface stubs (no HA runtime)."""
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.ac_infinity_airtap_ble.device import DeviceInfoEx
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import BLEStepError


@pytest.fixture
def flow_module(monkeypatch):
    import homeassistant
    entries = ModuleType("homeassistant.config_entries")
    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass
    entries.ConfigFlow = ConfigFlow
    monkeypatch.setattr(homeassistant, "config_entries", entries, raising=False)
    modules = {
        "homeassistant.config_entries": entries,
        "homeassistant.components": ModuleType("homeassistant.components"),
        "homeassistant.components.bluetooth": SimpleNamespace(
            BluetoothServiceInfoBleak=object,
            async_ble_device_from_address=lambda *args: None,
            async_discovered_service_info=lambda *args: [],
            async_last_service_info=lambda *args, **kwargs: None),
        "homeassistant.const": SimpleNamespace(CONF_ADDRESS="address", CONF_SERVICE_DATA="service_data"),
        "homeassistant.data_entry_flow": SimpleNamespace(FlowResult=dict),
        "custom_components.ac_infinity_airtap_ble.legacy": SimpleNamespace(
            async_adopt_legacy_entry=AsyncMock(), async_find_legacy_entry=lambda *args: None),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "custom_components.ac_infinity_airtap_ble.config_flow", raising=False)
    return importlib.import_module("custom_components.ac_infinity_airtap_ble.config_flow")


def make_flow(module):
    flow = module.ConfigFlow()
    flow.hass = object()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = lambda: None
    flow._async_current_ids = lambda: set()
    flow.async_create_entry = lambda **kwargs: kwargs
    flow.async_show_form = lambda **kwargs: kwargs
    return flow


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_discovery_saves_queried_state_as_json_not_advertisement_dataclass(flow_module, monkeypatch, manual):
    flow = make_flow(flow_module)
    discovery = SimpleNamespace(address="A4:C1:38:F6:48:F6", device=SimpleNamespace(name="vent"), advertisement=object())
    flow._discovered_devices[discovery.address] = discovery
    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0)
    controller = SimpleNamespace(state=state, name=state.name, protocol_variant="android_2_1_2_telink", update=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)
    if manual:
        monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: discovery.device)
        entry = await flow.async_step_manual({"address": discovery.address})
    else:
        entry = await flow.async_step_user({"address": discovery.address})
    assert entry["data"]["service_data"]["level_on"] == 5
    assert entry["data"]["protocol_variant"] == "android_2_1_2_telink"
    json.dumps(entry["data"])
    controller.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["connect_failed", "notify_failed", "no_response", "invalid_response", "unsupported_model"])
async def test_manual_setup_shows_specific_failure(flow_module, monkeypatch, code):
    flow = make_flow(flow_module)
    monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: SimpleNamespace(name="vent"))
    controller = SimpleNamespace(update=AsyncMock(side_effect=BLEStepError("test", code, "test")))
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)
    form = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert form["errors"]["base"] == code


def test_all_error_keys_have_english_translations():
    root = Path(__file__).resolve().parents[1] / "custom_components/ac_infinity_airtap_ble"
    strings = json.loads((root / "strings.json").read_text())
    english = json.loads((root / "translations/en.json").read_text())
    assert strings["config"]["error"] == english["config"]["error"]
    assert {"no_response", "connect_failed", "service_failed", "unsupported_model", "notify_failed", "write_failed", "invalid_response", "disconnect_failed"} <= strings["config"]["error"].keys()


@pytest.mark.asyncio
async def test_setup_validation_decoupled_with_advertisement_fallback_user_step(flow_module, monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import MANUFACTURER_ID
    flow = make_flow(flow_module)

    # Build 20-byte manufacturer data with tmp = 21.5C (2150)
    mfr_bytes = bytearray(20)
    mfr_bytes[6:11] = b"8DAQJ"
    mfr_bytes[11] = 3
    mfr_bytes[12] = 6
    mfr_bytes[13] = 0
    mfr_bytes[14:16] = (2150).to_bytes(2, "big")
    mfr_bytes[16:18] = (5000).to_bytes(2, "big")
    mfr_bytes[18] = 4

    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(mfr_bytes)})
    discovery = SimpleNamespace(address="A4:C1:38:F6:48:F6", device=SimpleNamespace(name="vent"), advertisement=advert)
    flow._discovered_devices[discovery.address] = discovery

    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0)
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=TimeoutError("GATT update timed out")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    entry = await flow.async_step_user({"address": discovery.address})
    # Must succeed and create config entry even though update() timed out
    assert entry["type"] if isinstance(entry, dict) and "type" in entry else True
    assert entry["data"]["service_data"]["tmp"] == 21.5
    controller.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_validation_decoupled_with_advertisement_fallback_manual_step(flow_module, monkeypatch):
    from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import MANUFACTURER_ID
    flow = make_flow(flow_module)

    mfr_bytes = bytearray(20)
    mfr_bytes[6:11] = b"8DAQJ"
    mfr_bytes[11] = 3
    mfr_bytes[12] = 6
    mfr_bytes[13] = 0
    mfr_bytes[14:16] = (2300).to_bytes(2, "big")  # 23.0C
    mfr_bytes[16:18] = (4500).to_bytes(2, "big")
    mfr_bytes[18] = 2

    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(mfr_bytes)})
    ble_device = SimpleNamespace(name="vent")
    service_info = SimpleNamespace(advertisement=advert)

    monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: ble_device)
    monkeypatch.setattr(flow_module, "async_last_service_info", lambda *args, **kwargs: service_info)

    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=23.0)
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
async def test_manual_setup_without_advertisement_data_fails_on_gatt_timeout(flow_module, monkeypatch):
    flow = make_flow(flow_module)
    ble_device = SimpleNamespace(name="vent")

    monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: ble_device)
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

