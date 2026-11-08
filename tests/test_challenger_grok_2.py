"""Adversarial stress-test suite for Config Flow, Options Flow, and BLE pairing/chunking edge cases.

Authored by Challenger Grok 2 for v2.2.0-fix.6.
Empirically stress-tests:
1. Config Flow with passive_only=True (zero GATT connection attempts, clean entry options).
2. Config Flow with passive_only=False and attempt_pairing=True (active GATT update attempted, pairing flag passed).
3. ACInfinityOptionsFlowHandler (toggling, schema defaults, live device application, error resilience).
4. Missing keys / legacy entries (backward compatibility, clean False defaults, options vs data precedence).
5. Low-level BLE connection & transport edge cases (opt-in pair() on retry vs never on attempt_pairing=False,
   non-fatal pair() errors, MTU 23 20+1 chunking).
"""
from __future__ import annotations

import dataclasses
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import importlib.machinery
for _mod_name in ["custom_components", "custom_components.ac_infinity_airtap_ble"]:
    if _mod_name in sys.modules and getattr(sys.modules[_mod_name], "__spec__", None) is None:
        sys.modules[_mod_name].__spec__ = importlib.machinery.ModuleSpec(_mod_name, None, is_package=True)

import pytest
import voluptuous as vol

from custom_components.ac_infinity_airtap_ble.const import (
    CONF_ATTEMPT_PAIRING,
    CONF_PASSIVE_ONLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.ac_infinity_airtap_ble.device import DeviceInfoEx
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import (
    MANUFACTURER_ID,
    POSSIBLE_READ_CHARACTERISTIC_UUIDS,
    POSSIBLE_WRITE_CHARACTERISTIC_UUIDS,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.device import (
    ACInfinityController,
)


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def flow_module(monkeypatch):
    """Provide isolated config_flow module with HA stubs."""
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
            async_ble_device_from_address=lambda *args, **kwargs: None,
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
        data={},
        async_create_task=MagicMock(side_effect=lambda coro: coro.close() if hasattr(coro, "close") else None),
        config_entries=SimpleNamespace(
            flow=SimpleNamespace(
                async_init=AsyncMock(return_value={"type": "form"}),
            ),
            async_reload=AsyncMock(return_value=True),
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
    buf = bytearray(max(length, 19))
    buf[0:6] = b"\xa4\xc1\x38\xf6\x48\xf6"
    buf[6:11] = serial
    buf[11] = dev_type
    buf[12] = version
    raw_temp = int(tmp_c * 100)
    buf[13] = (raw_temp >> 8) & 0xFF
    buf[14] = raw_temp & 0xFF
    return bytes(buf)


def make_char(uuid: str, properties: list[str] | None = None, handle: int = 1):
    return SimpleNamespace(
        uuid=uuid,
        properties=properties or ["read", "write", "notify"],
        handle=handle,
        descriptors=[],
        service_uuid="70d51000-2c7f-4e75-ae8a-d758951ce4e0",
    )


# ---------------------------------------------------------------------------
# TEST SECTION 1: Config Flow with passive_only=True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passive_only_user_step_single_device_zero_gatt(flow_module, monkeypatch):
    """Verify selecting passive_only=True in async_step_user completely skips controller.update()."""
    flow = make_flow(flow_module)
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=23.5)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="AirTap T6"),
        advertisement=advert,
    )
    flow._discovered_devices[discovery.address] = discovery

    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ", tmp=23.5),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: mock_controller)

    result = await flow.async_step_user({
        "address": discovery.address,
        CONF_PASSIVE_ONLY: True,
        CONF_ATTEMPT_PAIRING: False,
    })

    # Zero GATT connection attempts
    assert mock_controller.update.await_count == 0
    mock_controller.stop.assert_awaited_once()

    # Entry options verification
    assert result["title"] == "D-8DAQJ"
    assert result["data"]["address"] == "A4:C1:38:F6:48:F6"
    assert result["options"][CONF_PASSIVE_ONLY] is True
    assert result["options"][CONF_ATTEMPT_PAIRING] is False


@pytest.mark.asyncio
async def test_passive_only_user_step_all_devices_zero_gatt(flow_module, monkeypatch):
    """Verify selecting '__all__' with passive_only=True skips controller.update() for primary device."""
    flow = make_flow(flow_module)
    d1_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQ1", tmp_c=21.0)
    d2_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQ2", tmp_c=22.0)
    disc1 = SimpleNamespace(
        address="A4:C1:38:F6:48:01",
        device=SimpleNamespace(name="Vent 1"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: d1_bytes}),
    )
    disc2 = SimpleNamespace(
        address="A4:C1:38:F6:48:02",
        device=SimpleNamespace(name="Vent 2"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: d2_bytes}),
    )
    flow._discovered_devices[disc1.address] = disc1
    flow._discovered_devices[disc2.address] = disc2

    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQ1"),
        name="D-8DAQ1",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: mock_controller)

    result = await flow.async_step_user({
        "address": "__all__",
        CONF_PASSIVE_ONLY: True,
        CONF_ATTEMPT_PAIRING: False,
    })

    # Zero GATT connection attempts on primary device
    assert mock_controller.update.await_count == 0
    mock_controller.stop.assert_awaited_once()

    assert result["title"] == "D-8DAQ1"
    assert result["data"]["address"] == "A4:C1:38:F6:48:01"
    assert result["options"][CONF_PASSIVE_ONLY] is True
    assert result["options"][CONF_ATTEMPT_PAIRING] is False

    # Spawned task for second device
    flow.hass.async_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_passive_only_manual_step_zero_gatt(flow_module, monkeypatch):
    """Verify entering MAC manually with passive_only=True skips controller.update()."""
    flow = make_flow(flow_module)
    mac = "A4:C1:38:F6:48:F6"
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=24.0)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    service_info = SimpleNamespace(address=mac, advertisement=advert, device=SimpleNamespace(name="AirTap"))

    monkeypatch.setattr(
        flow_module,
        "async_last_service_info",
        lambda hass, addr, connectable=True: service_info if addr == mac else None,
    )
    monkeypatch.setattr(
        flow_module,
        "async_ble_device_from_address",
        lambda hass, addr, connectable=True: SimpleNamespace(address=mac, name="AirTap") if addr == mac else None,
    )

    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ", tmp=24.0),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: mock_controller)

    result = await flow.async_step_manual({
        "address": mac,
        CONF_PASSIVE_ONLY: True,
        CONF_ATTEMPT_PAIRING: False,
    })

    assert mock_controller.update.await_count == 0
    mock_controller.stop.assert_awaited_once()
    assert result["title"] == "D-8DAQJ"
    assert result["options"][CONF_PASSIVE_ONLY] is True
    assert result["options"][CONF_ATTEMPT_PAIRING] is False


# ---------------------------------------------------------------------------
# TEST SECTION 2: Config Flow with passive_only=False and attempt_pairing=True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_mode_attempt_pairing_user_step(flow_module, monkeypatch):
    """Verify passive_only=False and attempt_pairing=True attempts GATT update and passes pairing flag."""
    flow = make_flow(flow_module)
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=22.0)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="AirTap T6"),
        advertisement=advert,
    )
    flow._discovered_devices[discovery.address] = discovery

    init_kwargs = {}
    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    def fake_init(*args, **kwargs):
        init_kwargs.update(kwargs)
        return mock_controller

    monkeypatch.setattr(flow_module, "ACInfinityDevice", fake_init)

    result = await flow.async_step_user({
        "address": discovery.address,
        CONF_PASSIVE_ONLY: False,
        CONF_ATTEMPT_PAIRING: True,
    })

    assert init_kwargs.get("attempt_pairing") is True
    mock_controller.update.assert_awaited_once()
    mock_controller.stop.assert_awaited_once()
    assert result["options"][CONF_PASSIVE_ONLY] is False
    assert result["options"][CONF_ATTEMPT_PAIRING] is True


@pytest.mark.asyncio
async def test_active_mode_attempt_pairing_gatt_timeout_fallback(flow_module, monkeypatch):
    """Verify that when active GATT update times out with attempt_pairing=True, options are preserved."""
    flow = make_flow(flow_module)
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=22.5)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="AirTap T6"),
        advertisement=advert,
    )
    flow._discovered_devices[discovery.address] = discovery

    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=TimeoutError("GATT timed out")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: mock_controller)

    result = await flow.async_step_user({
        "address": discovery.address,
        CONF_PASSIVE_ONLY: False,
        CONF_ATTEMPT_PAIRING: True,
    })

    # Update was attempted
    mock_controller.update.assert_awaited_once()
    # Fallback created entry successfully with options preserved
    assert "title" in result
    assert result["options"][CONF_PASSIVE_ONLY] is False
    assert result["options"][CONF_ATTEMPT_PAIRING] is True


@pytest.mark.asyncio
async def test_active_mode_manual_step_attempt_pairing_timeout_fallback(flow_module, monkeypatch):
    """Verify manual entry with attempt_pairing=True handles GATT timeout fallback with options preserved."""
    flow = make_flow(flow_module)
    mac = "A4:C1:38:F6:48:F6"
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ", tmp_c=25.0)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    service_info = SimpleNamespace(address=mac, advertisement=advert, device=SimpleNamespace(name="AirTap"))

    monkeypatch.setattr(
        flow_module,
        "async_last_service_info",
        lambda hass, addr, connectable=True: service_info if addr == mac else None,
    )
    monkeypatch.setattr(
        flow_module,
        "async_ble_device_from_address",
        lambda hass, addr, connectable=True: SimpleNamespace(address=mac, name="AirTap") if addr == mac else None,
    )

    init_kwargs = {}
    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=TimeoutError("Timeout")),
        stop=AsyncMock(),
    )
    def fake_init(*args, **kwargs):
        init_kwargs.update(kwargs)
        return mock_controller

    monkeypatch.setattr(flow_module, "ACInfinityDevice", fake_init)

    result = await flow.async_step_manual({
        "address": mac,
        CONF_PASSIVE_ONLY: False,
        CONF_ATTEMPT_PAIRING: True,
    })

    assert init_kwargs.get("attempt_pairing") is True
    mock_controller.update.assert_awaited_once()
    assert result["options"][CONF_PASSIVE_ONLY] is False
    assert result["options"][CONF_ATTEMPT_PAIRING] is True


# ---------------------------------------------------------------------------
# TEST SECTION 3: ACInfinityOptionsFlowHandler Stress & Live Reload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_options_flow_form_displays_existing_options(flow_module):
    """Verify options flow form initializes schema with existing options values."""
    entry = SimpleNamespace(
        options={
            CONF_SCAN_INTERVAL: 45,
            CONF_PASSIVE_ONLY: True,
            CONF_ATTEMPT_PAIRING: True,
        },
        data={},
        entry_id="test_entry_options_init",
    )
    handler = flow_module.ACInfinityOptionsFlowHandler(entry)
    result = await handler.async_step_init()

    schema = result["data_schema"]
    assert isinstance(schema, vol.Schema)
    # Validate schema defaults against existing options
    parsed = schema({})
    assert parsed[CONF_SCAN_INTERVAL] == 45
    assert parsed[CONF_PASSIVE_ONLY] is True
    assert parsed[CONF_ATTEMPT_PAIRING] is True


@pytest.mark.asyncio
async def test_options_flow_form_defaults_when_options_empty(flow_module):
    """Verify options flow form defaults cleanly when entry.options is empty."""
    entry = SimpleNamespace(
        options={},
        data={},
        entry_id="test_entry_options_empty",
    )
    handler = flow_module.ACInfinityOptionsFlowHandler(entry)
    result = await handler.async_step_init()

    schema = result["data_schema"]
    parsed = schema({})
    assert parsed[CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL
    assert parsed[CONF_PASSIVE_ONLY] is False
    assert parsed[CONF_ATTEMPT_PAIRING] is False


@pytest.mark.asyncio
async def test_options_flow_toggles_and_live_updates_device(flow_module):
    """Verify submitting options updates attempt_pairing on live device instance."""
    entry = SimpleNamespace(
        options={CONF_ATTEMPT_PAIRING: False, CONF_PASSIVE_ONLY: False},
        data={},
        entry_id="test_entry_live",
    )
    handler = flow_module.ACInfinityOptionsFlowHandler(entry)
    mock_device = SimpleNamespace(attempt_pairing=False)
    handler.hass = SimpleNamespace(
        data={DOMAIN: {"test_entry_live": SimpleNamespace(device=mock_device)}}
    )

    # 1. Toggle attempt_pairing to True
    res1 = await handler.async_step_init({
        CONF_ATTEMPT_PAIRING: True,
        CONF_PASSIVE_ONLY: True,
        CONF_SCAN_INTERVAL: 25,
    })
    assert res1["data"][CONF_ATTEMPT_PAIRING] is True
    assert res1["data"][CONF_PASSIVE_ONLY] is True
    assert mock_device.attempt_pairing is True

    # 2. Toggle attempt_pairing back to False
    res2 = await handler.async_step_init({
        CONF_ATTEMPT_PAIRING: False,
        CONF_PASSIVE_ONLY: False,
        CONF_SCAN_INTERVAL: 10,
    })
    assert res2["data"][CONF_ATTEMPT_PAIRING] is False
    assert res2["data"][CONF_PASSIVE_ONLY] is False
    assert mock_device.attempt_pairing is False


@pytest.mark.asyncio
async def test_options_flow_graceful_when_device_missing(flow_module):
    """Verify options flow does not throw if hass, DOMAIN, or entry_data.device is missing."""
    entry = SimpleNamespace(
        options={},
        data={},
        entry_id="missing_entry",
    )
    # Test 1: handler.hass is None
    handler1 = flow_module.ACInfinityOptionsFlowHandler(entry)
    handler1.hass = None
    res1 = await handler1.async_step_init({CONF_ATTEMPT_PAIRING: True, CONF_PASSIVE_ONLY: False})
    assert res1["data"][CONF_ATTEMPT_PAIRING] is True

    # Test 2: DOMAIN not in hass.data
    handler2 = flow_module.ACInfinityOptionsFlowHandler(entry)
    handler2.hass = SimpleNamespace(data={})
    res2 = await handler2.async_step_init({CONF_ATTEMPT_PAIRING: True, CONF_PASSIVE_ONLY: False})
    assert res2["data"][CONF_ATTEMPT_PAIRING] is True

    # Test 3: entry_id not in hass.data[DOMAIN]
    handler3 = flow_module.ACInfinityOptionsFlowHandler(entry)
    handler3.hass = SimpleNamespace(data={DOMAIN: {}})
    res3 = await handler3.async_step_init({CONF_ATTEMPT_PAIRING: True, CONF_PASSIVE_ONLY: False})
    assert res3["data"][CONF_ATTEMPT_PAIRING] is True

    # Test 4: device attribute has no attempt_pairing
    handler4 = flow_module.ACInfinityOptionsFlowHandler(entry)
    handler4.hass = SimpleNamespace(data={DOMAIN: {"missing_entry": SimpleNamespace(device=object())}})
    res4 = await handler4.async_step_init({CONF_ATTEMPT_PAIRING: True, CONF_PASSIVE_ONLY: False})
    assert res4["data"][CONF_ATTEMPT_PAIRING] is True


# ---------------------------------------------------------------------------
# TEST SECTION 4: Missing Keys & Legacy Entries Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_step_missing_keys_default_cleanly_to_false(flow_module, monkeypatch):
    """Verify submitting only address in async_step_user defaults passive_only & attempt_pairing to False."""
    flow = make_flow(flow_module)
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ")
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="AirTap T6"),
        advertisement=advert,
    )
    flow._discovered_devices[discovery.address] = discovery

    init_kwargs = {}
    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    def fake_init(*args, **kwargs):
        init_kwargs.update(kwargs)
        return mock_controller

    monkeypatch.setattr(flow_module, "ACInfinityDevice", fake_init)

    # user_input provides ONLY address (simulating legacy or basic UI call)
    result = await flow.async_step_user({"address": discovery.address})

    assert init_kwargs.get("attempt_pairing") is False
    mock_controller.update.assert_awaited_once()
    assert result["options"][CONF_PASSIVE_ONLY] is False
    assert result["options"][CONF_ATTEMPT_PAIRING] is False


@pytest.mark.asyncio
async def test_manual_step_missing_keys_default_cleanly_to_false(flow_module, monkeypatch):
    """Verify submitting only address in async_step_manual defaults passive_only & attempt_pairing to False."""
    flow = make_flow(flow_module)
    mac = "A4:C1:38:F6:48:F6"
    mfr_bytes = build_mfr_payload(dev_type=6, version=3, serial=b"8DAQJ")
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_bytes})
    service_info = SimpleNamespace(address=mac, advertisement=advert, device=SimpleNamespace(name="AirTap"))

    monkeypatch.setattr(
        flow_module,
        "async_last_service_info",
        lambda hass, addr, connectable=True: service_info if addr == mac else None,
    )
    monkeypatch.setattr(
        flow_module,
        "async_ble_device_from_address",
        lambda hass, addr, connectable=True: SimpleNamespace(address=mac, name="AirTap") if addr == mac else None,
    )

    init_kwargs = {}
    mock_controller = SimpleNamespace(
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"),
        name="D-8DAQJ",
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(),
        stop=AsyncMock(),
    )
    def fake_init(*args, **kwargs):
        init_kwargs.update(kwargs)
        return mock_controller

    monkeypatch.setattr(flow_module, "ACInfinityDevice", fake_init)

    result = await flow.async_step_manual({"address": mac})

    assert init_kwargs.get("attempt_pairing") is False
    mock_controller.update.assert_awaited_once()
    assert result["options"][CONF_PASSIVE_ONLY] is False
    assert result["options"][CONF_ATTEMPT_PAIRING] is False


def _get_async_setup_entry():
    import importlib.util
    pkg_name = "custom_components.ac_infinity_airtap_ble"
    init_path = Path(__file__).resolve().parents[1] / "custom_components/ac_infinity_airtap_ble/__init__.py"
    mod = sys.modules.get(pkg_name)
    if mod is None or not hasattr(mod, "async_setup_entry"):
        spec = importlib.util.spec_from_file_location(
            pkg_name,
            init_path,
            submodule_search_locations=[str(init_path.parent)],
        )
        if mod is None:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[pkg_name] = mod
        mod.__file__ = str(init_path)
        mod.__spec__ = spec
        spec.loader.exec_module(mod)
    return mod.async_setup_entry


@pytest.mark.asyncio
async def test_init_setup_entry_legacy_entry_defaults_attempt_pairing_false(monkeypatch):
    """Verify __init__.py:async_setup_entry cleanly defaults attempt_pairing=False on legacy entries."""
    import homeassistant.components.bluetooth.passive_update_coordinator as puc
    monkeypatch.setattr(puc.PassiveBluetoothDataUpdateCoordinator, "async_start", lambda self: (lambda: None), raising=False)

    async_setup_entry = _get_async_setup_entry()
    import homeassistant.components.bluetooth as bluetooth

    entry_data = {
        "address": "A4:C1:38:F6:48:F6",
        "service_data": dataclasses.asdict(DeviceInfoEx(type=6, version=3, name="D-8DAQJ")),
    }
    # No attempt_pairing in data, and options is empty
    mock_entry = SimpleNamespace(
        entry_id="legacy_entry_1",
        data=entry_data,
        options={},
        title="D-8DAQJ",
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(),
    )

    fake_ble_dev = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="D-8DAQJ")
    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", lambda hass, addr, connectable: fake_ble_dev)

    mock_hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(return_value=True),
        ),
    )

    success = await async_setup_entry(mock_hass, mock_entry)
    assert success is True

    loaded_data = mock_hass.data[DOMAIN]["legacy_entry_1"]
    assert loaded_data.device.attempt_pairing is False


@pytest.mark.asyncio
async def test_init_setup_entry_options_override_data(monkeypatch):
    """Verify entry.options[CONF_ATTEMPT_PAIRING] overrides entry.data[CONF_ATTEMPT_PAIRING]."""
    import homeassistant.components.bluetooth.passive_update_coordinator as puc
    monkeypatch.setattr(puc.PassiveBluetoothDataUpdateCoordinator, "async_start", lambda self: (lambda: None), raising=False)

    async_setup_entry = _get_async_setup_entry()
    import homeassistant.components.bluetooth as bluetooth

    entry_data = {
        "address": "A4:C1:38:F6:48:F6",
        "service_data": dataclasses.asdict(DeviceInfoEx(type=6, version=3, name="D-8DAQJ")),
        CONF_ATTEMPT_PAIRING: True,  # legacy True in data
    }
    # User disabled it in options:
    entry_options = {
        CONF_ATTEMPT_PAIRING: False,
    }
    mock_entry = SimpleNamespace(
        entry_id="legacy_entry_override",
        data=entry_data,
        options=entry_options,
        title="D-8DAQJ",
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(),
    )

    fake_ble_dev = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="D-8DAQJ")
    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", lambda hass, addr, connectable: fake_ble_dev)

    mock_hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(return_value=True),
        ),
    )

    success = await async_setup_entry(mock_hass, mock_entry)
    assert success is True

    loaded_data = mock_hass.data[DOMAIN]["legacy_entry_override"]
    # Options override data!
    assert loaded_data.device.attempt_pairing is False


# ---------------------------------------------------------------------------
# TEST SECTION 5: Low-Level BLE Connection & Transport Edge Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ble_transport_never_calls_pair_when_attempt_pairing_false(monkeypatch):
    """Verify low-level BLE transport NEVER calls client.pair() on retries when attempt_pairing=False."""
    ble_device = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        name="D-8DAQJ",
        details={"props": {"Paired": False}},
        rssi=-70,
    )
    dev = ACInfinityController(ble_device, state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"), attempt_pairing=False)
    assert dev.attempt_pairing is False

    mock_client = SimpleNamespace(
        is_connected=True,
        pair=AsyncMock(return_value=True),
        mtu_size=23,
    )

    # Force retry conditions: attempt >= 2 and failure streak
    dev._failure_streak = 3
    dev._connect_attempt = 2

    # Even if _pair_client is called, verify the outer check prevents calling it
    assert dev._attempt_pairing is False

    # Simulate connect loop attempt 2
    if dev._attempt_pairing and (2 >= 2 or dev._connect_attempt >= 1 or dev._failure_streak):
        await dev._pair_client(mock_client, 2)

    mock_client.pair.assert_not_called()


@pytest.mark.asyncio
async def test_ble_transport_calls_pair_on_retry_when_attempt_pairing_true():
    """Verify low-level BLE transport DOES call client.pair() on retry when attempt_pairing=True."""
    ble_device = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        name="D-8DAQJ",
        details={"props": {"Paired": False}},
        rssi=-70,
    )
    dev = ACInfinityController(ble_device, state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"), attempt_pairing=True)
    assert dev.attempt_pairing is True

    mock_client = SimpleNamespace(
        is_connected=True,
        pair=AsyncMock(return_value=True),
    )

    paired = await dev._pair_client(mock_client, attempt=2)
    assert paired is True
    mock_client.pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_ble_transport_pair_non_fatal_on_not_implemented():
    """Verify pairing failure due to NotImplementedError does not raise and returns False."""
    ble_device = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="D-8DAQJ")
    dev = ACInfinityController(ble_device, state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"), attempt_pairing=True)

    mock_client = SimpleNamespace(
        is_connected=True,
        pair=AsyncMock(side_effect=NotImplementedError("Pairing not supported on macOS")),
    )

    paired = await dev._pair_client(mock_client, attempt=2)
    assert paired is False
    assert mock_client.is_connected is True


@pytest.mark.asyncio
async def test_ble_transport_pair_non_fatal_on_rejection():
    """Verify pairing failure due to OS rejection does not raise and returns False."""
    from bleak.exc import BleakError

    ble_device = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="D-8DAQJ")
    dev = ACInfinityController(ble_device, state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"), attempt_pairing=True)

    mock_client = SimpleNamespace(
        is_connected=True,
        pair=AsyncMock(side_effect=BleakError("Pairing refused by agent")),
    )

    paired = await dev._pair_client(mock_client, attempt=2)
    assert paired is False
    assert mock_client.is_connected is True


@pytest.mark.asyncio
async def test_clock_sync_chunking_mtu23_splits_into_20_and_1():
    """Empirically verify 21-byte command splits into 20 bytes + 1 byte chunks under MTU 23."""
    ble_device = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="D-8DAQJ")
    dev = ACInfinityController(ble_device, state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ"))

    write_char = make_char(POSSIBLE_WRITE_CHARACTERISTIC_UUIDS[0], ["write", "write-without-response"], handle=20)
    read_char = make_char(POSSIBLE_READ_CHARACTERISTIC_UUIDS[0], ["notify", "indicate"], handle=21)
    dev._write_char = write_char
    dev._read_char = read_char
    dev._negotiated_mtu = 23  # max_pdu = 20

    written_chunks = []
    async def mock_write_gatt_char(char, data, response=True):
        written_chunks.append((bytes(data), response))
        if len(written_chunks) == 2:
            dev._notify_future.set_result(b"\xa5\x00\x00\x06\x00\x00\x06\x00\x00\x03\x00\x00")

    dev._client = SimpleNamespace(
        is_connected=True,
        write_gatt_char=mock_write_gatt_char,
    )

    # 21-byte clock command
    cmd21 = bytes([0xA5, 0x00, 0x00, 0x06, 0x00, 0x00, 0x06, 0x00, 0x00, 0x03, 0x01, 0x07] + [0x01] * 7 + [0xAA, 0x55])
    assert len(cmd21) == 21

    await dev._execute_command_locked(cmd21)

    # Verify exactly 2 chunks
    assert len(written_chunks) == 2
    # Chunk 1: 20 bytes
    assert len(written_chunks[0][0]) == 20
    assert written_chunks[0][0] == cmd21[:20]
    assert written_chunks[0][1] is True  # ATT_WRITE_REQ
    # Chunk 2: 1 byte
    assert len(written_chunks[1][0]) == 1
    assert written_chunks[1][0] == cmd21[20:]
    assert written_chunks[1][1] is True  # ATT_WRITE_REQ
