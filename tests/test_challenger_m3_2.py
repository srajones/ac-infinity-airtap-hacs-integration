"""Empirical challenger tests for Milestone 3 requirements:
1. Primary Entity Priority: Ambient air temperature sensor guaranteed registration via advertisement fallback.
2. Self-Healing Availability: Sensor entities do not flip to unavailable/unknown on transient disconnects,
   call _update_attrs() in __init__, and restore state via RestoreSensor.
3. Multi-Proxy Recovery: Switching proxies or adapter resets never drops an advertisement update.
4. Broad Model Compatibility: All 29+ models in DEVICE_MODEL and unmapped models fall back cleanly without KeyError.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ac_infinity_airtap_ble.const import (
    DEVICE_MODEL,
    get_device_model_name,
)
from custom_components.ac_infinity_airtap_ble.coordinator import (
    ACInfinityDataUpdateCoordinator,
)
from custom_components.ac_infinity_airtap_ble.device import (
    ACInfinityDevice,
    DeviceInfoEx,
)
from custom_components.ac_infinity_airtap_ble.sensor import (
    CurrentSpeedSensor,
    FirmwareRevisionSensor,
    HardwareRevisionSensor,
    HumiditySensor,
    OnSpeedSensor,
    OperatingModeSensor,
    ProtocolVersionSensor,
    SignalStrengthSensor,
    SoftwareRevisionSensor,
    TemperatureSensor,
    VpdSensor,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import (
    MANUFACTURER_ID,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.exceptions import (
    BLEStepError,
)
from bleak.exc import BleakError


def make_mfr_payload(tmp_c: float, fan: int, dev_type: int = 6) -> bytes:
    """Build valid 20-byte manufacturer advertisement data."""
    data = bytearray(20)
    data[6:11] = b"8DAQJ"
    data[11] = 3
    data[12] = dev_type
    data[13] = 0
    data[14:16] = int(round(tmp_c * 100)).to_bytes(2, "big")
    data[16:18] = (5000).to_bytes(2, "big")
    data[18] = fan
    return bytes(data)


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
            async_last_service_info=lambda *args, **kwargs: None,
        ),
        "homeassistant.const": SimpleNamespace(
            CONF_ADDRESS="address", CONF_SERVICE_DATA="service_data"
        ),
        "homeassistant.data_entry_flow": SimpleNamespace(FlowResult=dict),
        "custom_components.ac_infinity_airtap_ble.legacy": SimpleNamespace(
            async_adopt_legacy_entry=AsyncMock(),
            async_find_legacy_entry=lambda *args: None,
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(
        sys.modules,
        "custom_components.ac_infinity_airtap_ble.config_flow",
        raising=False,
    )
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


# ---------------------------------------------------------------------------
# CHALLENGE 1: Primary Entity Priority - Ambient Air Temperature Guarantee
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: TimeoutError("GATT update timed out after 30s"),
        lambda: BleakError("BlueZ org.bluez.Error.Failed"),
        lambda: BLEStepError("connect", "connect_failed", "Device not responding"),
        lambda: BLEStepError("subscribe", "notify_failed", "CCCD descriptor write failed"),
        lambda: BLEStepError("read_diagnostics", "service_failed", "Handle 33 read error"),
        lambda: ValueError("Corrupted packet received during GATT handshake"),
    ],
)
async def test_challenge_user_step_fallback_under_all_gatt_errors(flow_module, monkeypatch, error_factory):
    """Verify that ANY GATT failure during setup falls back to advertisement data to register ambient temp."""
    flow = make_flow(flow_module)
    mfr_data = make_mfr_payload(tmp_c=22.8, fan=3)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_data})
    discovery = SimpleNamespace(
        address="A4:C1:38:F6:48:F6",
        device=SimpleNamespace(name="vent"),
        advertisement=advert,
    )
    flow._discovered_devices[discovery.address] = discovery

    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0)
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=error_factory()),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    entry = await flow.async_step_user({"address": discovery.address})

    # Crucial assertion: Config entry MUST be created without error
    assert "data" in entry, f"Config entry creation failed on {error_factory()}: {entry}"
    assert entry["data"]["service_data"]["tmp"] == 22.8
    # Controller must always be cleaned up
    controller.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_challenge_manual_step_fallback_with_cached_advertisement(flow_module, monkeypatch):
    """When user adds by MAC manually and GATT times out, but HA cached an advert, guarantee fallback."""
    flow = make_flow(flow_module)
    mfr_data = make_mfr_payload(tmp_c=19.4, fan=2)
    advert = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: mfr_data})
    ble_device = SimpleNamespace(name="vent")
    service_info = SimpleNamespace(advertisement=advert)

    monkeypatch.setattr(flow_module, "async_ble_device_from_address", lambda *args: ble_device)
    monkeypatch.setattr(flow_module, "async_last_service_info", lambda *args, **kwargs: service_info)

    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=19.4)
    controller = SimpleNamespace(
        state=state,
        name=state.name,
        protocol_variant="android_2_1_2_telink",
        update=AsyncMock(side_effect=TimeoutError("GATT timeout")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(flow_module, "ACInfinityDevice", lambda *args, **kwargs: controller)

    entry = await flow.async_step_manual({"address": "A4:C1:38:F6:48:F6"})
    assert "data" in entry
    assert entry["data"]["service_data"]["tmp"] == 19.4
    controller.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# CHALLENGE 2: Self-Healing Availability & State Restoration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_challenge_sensor_instant_attrs_and_restore_sensor_paths():
    """Verify sensors initialize attrs immediately and restore cleanly via RestoreSensor."""
    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=21.2, fan=4)
    device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)

    coordinator = SimpleNamespace(
        data=None,
        async_add_listener=lambda *args: lambda: None,
        available=True,
    )

    temp_sensor = TemperatureSensor(coordinator, device, "Temperature")
    speed_sensor = CurrentSpeedSensor(coordinator, device, "Current Speed")

    # Native values must be immediately populated on __init__
    assert temp_sensor.native_value == 21.2
    assert speed_sensor.native_value == 4

    # Test RestoreSensor when state is initially None
    state_none = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=None, fan=None)
    device_none = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state_none)

    # Path A: async_get_last_sensor_data() provides value
    s_a = TemperatureSensor(coordinator, device_none, "Temperature")
    s_a.async_get_last_sensor_data = AsyncMock(return_value=SimpleNamespace(native_value=23.1))
    await s_a.async_added_to_hass()
    assert s_a.native_value == 23.1

    # Path B: async_get_last_sensor_data() is None, async_get_last_state() provides string float
    s_b = TemperatureSensor(coordinator, device_none, "Temperature")
    s_b.async_get_last_sensor_data = AsyncMock(return_value=None)
    s_b.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="24.7"))
    await s_b.async_added_to_hass()
    assert s_b.native_value == 24.7

    # Path C: async_get_last_state() is 'unknown' or 'unavailable' (must NOT crash or set invalid value)
    s_c = TemperatureSensor(coordinator, device_none, "Temperature")
    s_c.async_get_last_sensor_data = AsyncMock(return_value=None)
    s_c.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="unavailable"))
    await s_c.async_added_to_hass()
    assert s_c.native_value is None

    # Path D: async_get_last_state() is invalid string (e.g. corrupted text)
    s_d = TemperatureSensor(coordinator, device_none, "Temperature")
    s_d.async_get_last_sensor_data = AsyncMock(return_value=None)
    s_d.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="corrupted_val"))
    await s_d.async_added_to_hass()
    assert s_d.native_value is None


@pytest.mark.asyncio
async def test_challenge_none_clobbering_protection():
    """Verify that a temporary None in device.state does NOT clobber valid sensor values."""
    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=25.0, fan=7)
    device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)
    coordinator = SimpleNamespace(data=None, async_add_listener=lambda *args: lambda: None, available=True)

    temp_sensor = TemperatureSensor(coordinator, device, "Temperature")
    speed_sensor = CurrentSpeedSensor(coordinator, device, "Current Speed")

    assert temp_sensor.native_value == 25.0
    assert speed_sensor.native_value == 7

    # An advertisement without tmp arriving or setting tmp to None must NOT clobber
    device.state.tmp = None
    temp_sensor._update_attrs()
    assert temp_sensor.native_value == 25.0

    # Same for speed
    device.state.current_speed = None
    device.state.fan = None
    speed_sensor._update_attrs()
    assert speed_sensor.native_value == 7


# ---------------------------------------------------------------------------
# CHALLENGE 3: Multi-Proxy Handover and Adapter Resets
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_challenge_multi_proxy_handover_never_drops_advertisement():
    """Verify that switching between proxies or resetting adapter never drops an advertisement update."""
    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0)
    device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)

    hass = SimpleNamespace(loop=SimpleNamespace(time=lambda: 1000.0))
    coordinator = ACInfinityDataUpdateCoordinator.__new__(ACInfinityDataUpdateCoordinator)
    coordinator.hass = hass
    coordinator.logger = SimpleNamespace(debug=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
    coordinator.ble_device = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent")
    coordinator.controller = device
    coordinator._last_adverts = {}
    coordinator._device_ready = SimpleNamespace(set=MagicMock())
    coordinator._malformed_advertisement = False
    coordinator.async_update_listeners = MagicMock()

    # Step 1: Proxy 1 reports temperature 20.0C
    payload_20 = make_mfr_payload(20.0, 3)
    info_p1 = SimpleNamespace(
        source="esphome_proxy_1",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: payload_20}),
    )
    coordinator._handle_manufacturer_data(info_p1, payload_20)
    assert device.temperature == 20.0

    # Step 2: Proxy 2 receives the SAME packet (first time for proxy 2)
    # IN BUGGY CODE: This was dropped because previous was None and not first_overall!
    # IN FIXED CODE: Must be accepted and processed without drop!
    info_p2 = SimpleNamespace(
        source="esphome_proxy_2",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: payload_20}),
    )
    coordinator._handle_manufacturer_data(info_p2, payload_20)
    assert device.temperature == 20.0

    # Step 3: Duplicate packet from Proxy 1 is skipped
    # Mock set_ble_device_and_advertisement_data to observe calls
    mock_set = MagicMock()
    device.set_ble_device_and_advertisement_data = mock_set
    coordinator._handle_manufacturer_data(info_p1, payload_20)
    mock_set.assert_not_called()

    # Step 4: Duplicate packet from Proxy 2 is skipped
    coordinator._handle_manufacturer_data(info_p2, payload_20)
    mock_set.assert_not_called()

    # Step 5: Vent temperature changes to 21.5C. Proxy 1 is offline. Proxy 2 sends update.
    payload_21_5 = make_mfr_payload(21.5, 4)
    info_p2_new = SimpleNamespace(
        source="esphome_proxy_2",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: payload_21_5}),
    )
    # Restore real method
    device.set_ble_device_and_advertisement_data = ACInfinityDevice.set_ble_device_and_advertisement_data.__get__(device, ACInfinityDevice)
    coordinator._handle_manufacturer_data(info_p2_new, payload_21_5)
    assert device.temperature == 21.5

    # Step 6: Proxy 3 (e.g. USB dongle adapter reset with new source id) comes online with 22.0C
    payload_22 = make_mfr_payload(22.0, 5)
    info_p3 = SimpleNamespace(
        source="local_adapter_hci0",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: payload_22}),
    )
    coordinator._handle_manufacturer_data(info_p3, payload_22)
    assert device.temperature == 22.0


# ---------------------------------------------------------------------------
# CHALLENGE 4: Broad Model Compatibility & Safe Fallbacks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_challenge_all_device_model_coverage_and_unmapped_fallbacks():
    """Verify all 29+ models in DEVICE_MODEL and unmapped models never trigger KeyError."""
    assert len(DEVICE_MODEL) >= 29, f"Expected at least 29 models, found {len(DEVICE_MODEL)}"

    # Check each registered model
    for model_id, model_name in DEVICE_MODEL.items():
        assert isinstance(model_id, int)
        assert isinstance(model_name, str) and len(model_name) > 0
        assert get_device_model_name(model_id) == model_name

        # Each model must instantiate ACInfinityDevice cleanly
        state = DeviceInfoEx(type=model_id, version=3, name="TEST_MODEL")
        dev = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)
        assert dev.state.type == model_id

    # Check unmapped future models fall back cleanly without KeyError
    unmapped_ids = [-1, 0, 99, 100, 255, 999]
    for uid in unmapped_ids:
        fallback_name = get_device_model_name(uid)
        assert fallback_name == f"AC Infinity Device (Type {uid})"

    # Check None model fallback
    assert get_device_model_name(None) == "AC Infinity Device"


@pytest.mark.asyncio
async def test_challenge_entity_platforms_device_info_safe_for_all_models():
    """Verify that all sensor types generate DeviceInfo without KeyError for any model."""
    for model_id in DEVICE_MODEL:
        state = DeviceInfoEx(type=model_id, version=3, name="D-VENT", tmp=20.0, fan=2)
        device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)
        coordinator = SimpleNamespace(data=None, async_add_listener=lambda *args: lambda: None, available=True)

        sensors = [
            TemperatureSensor(coordinator, device, "Temperature"),
            CurrentSpeedSensor(coordinator, device, "Current Speed"),
            OperatingModeSensor(coordinator, device, "Operating Mode"),
            OnSpeedSensor(coordinator, device, "On Speed"),
            SignalStrengthSensor(coordinator, device, "Signal Strength"),
            ProtocolVersionSensor(coordinator, device, "Protocol Version"),
            FirmwareRevisionSensor(coordinator, device, "Firmware Revision"),
            SoftwareRevisionSensor(coordinator, device, "Software Revision"),
            HardwareRevisionSensor(coordinator, device, "Hardware Revision"),
            HumiditySensor(coordinator, device, "Humidity"),
            VpdSensor(coordinator, device, "VPD"),
        ]

        expected_model_name = get_device_model_name(model_id)
        for s in sensors:
            assert s._attr_device_info["model"] == expected_model_name

    # Unmapped model test using mock device to verify entity doesn't raise KeyError
    mock_unmapped_device = SimpleNamespace(
        name="D-UNMAPPED",
        address="A4:C1:38:F6:48:F6",
        state=SimpleNamespace(type=999, version=1, tmp=21.0, current_speed=3, fan=3, work_type=1, level_on=2),
        rssi=-70,
        firmware_revision="1.0",
        software_revision="1.0",
        hardware_revision="1.0",
        humidity=None,
        vpd=None,
    )
    coordinator = SimpleNamespace(data=None, async_add_listener=lambda *args: lambda: None, available=True)
    temp_s = TemperatureSensor(coordinator, mock_unmapped_device, "Temperature")
    assert temp_s._attr_device_info["model"] == "AC Infinity Device (Type 999)"


# ---------------------------------------------------------------------------
# CHALLENGE 5: Defensive Parsing & Malformed Packet Isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_challenge_malformed_advertisement_isolation():
    """Ensure malformed or truncated advertisement packets do not raise unhandled exceptions."""
    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ")
    device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)

    hass = SimpleNamespace(loop=SimpleNamespace(time=lambda: 1000.0))
    coordinator = ACInfinityDataUpdateCoordinator.__new__(ACInfinityDataUpdateCoordinator)
    coordinator.hass = hass
    coordinator.logger = SimpleNamespace(debug=MagicMock(), warning=MagicMock())
    coordinator.ble_device = SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent")
    coordinator.controller = device
    coordinator._last_adverts = {}
    coordinator._device_ready = SimpleNamespace(set=MagicMock())
    coordinator._malformed_advertisement = False

    # 1. Truncated packet (under 19 bytes)
    truncated = b"\x00" * 10
    event_truncated = SimpleNamespace(
        source="p1",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: truncated}),
    )
    # Must not raise
    coordinator._async_handle_bluetooth_event(event_truncated, object())
    assert coordinator._malformed_advertisement is True
    coordinator.logger.warning.assert_called_once()

    # 2. Repeated malformed packet does not spam warning
    coordinator.logger.warning.reset_mock()
    coordinator._async_handle_bluetooth_event(event_truncated, object())
    coordinator.logger.warning.assert_not_called()

    # 3. Good packet resets malformed flag
    good_payload = make_mfr_payload(23.5, 4)
    event_good = SimpleNamespace(
        source="p1",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: good_payload}),
    )
    coordinator._async_handle_bluetooth_event(event_good, object())
    assert coordinator._malformed_advertisement is False
    assert device.temperature == 23.5
