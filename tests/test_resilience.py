"""Tests for coordinator multi-proxy resilience and entity restoration."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ac_infinity_airtap_ble.const import (
    DEVICE_MODEL,
    TELINK_CHIP_MODELS,
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
    OperatingModeSensor,
    TemperatureSensor,
)
from custom_components.ac_infinity_airtap_ble.vendor.ac_infinity_ble.const import (
    CallbackType,
    MANUFACTURER_ID,
)


def make_mfr_payload(tmp_c: float, fan: int, dev_type: int = 6) -> bytes:
    data = bytearray(20)
    data[6:11] = b"8DAQJ"
    data[11] = 3
    data[12] = dev_type
    data[13] = 0
    data[14:16] = int(tmp_c * 100).to_bytes(2, "big")
    data[16:18] = (5000).to_bytes(2, "big")
    data[18] = fan
    return bytes(data)


def test_device_model_name_mapping_all_models_and_fallback():
    # Verify all 29+ models in DEVICE_MODEL map to distinct strings
    assert len(DEVICE_MODEL) >= 29
    for model_id, model_name in DEVICE_MODEL.items():
        assert get_device_model_name(model_id) == model_name

    # Telink chip models
    assert {4, 5, 6, 14, 15, 24, 25} <= TELINK_CHIP_MODELS

    # Unknown model fallback
    assert get_device_model_name(999) == "AC Infinity Device (Type 999)"
    assert get_device_model_name(None) == "AC Infinity Device"


@pytest.mark.asyncio
async def test_coordinator_multi_proxy_no_drop_and_device_callback():
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

    # Proxy 1 advertisement: 21.5C, fan 4
    data1 = make_mfr_payload(21.5, 4)
    service_info_1 = SimpleNamespace(
        source="esp32_proxy_living_room",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent_p1"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: data1}),
    )

    coordinator._handle_manufacturer_data(service_info_1, data1)
    assert device.temperature == 21.5

    # Proxy 2 sends advertisement from a different source (hallway proxy)
    # The fix ensures this is not dropped despite being a new proxy source
    data2 = make_mfr_payload(22.0, 5)
    service_info_2 = SimpleNamespace(
        source="esp32_proxy_hallway",
        device=SimpleNamespace(address="A4:C1:38:F6:48:F6", name="vent_p2"),
        advertisement=SimpleNamespace(manufacturer_data={MANUFACTURER_ID: data2}),
    )

    coordinator._handle_manufacturer_data(service_info_2, data2)
    assert device.temperature == 22.0

    # Test coordinator listener update on device callback
    coordinator._handle_device_callback(state, CallbackType.UPDATE_RESPONSE)
    coordinator.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_sensor_restore_and_none_protection():
    state = DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=22.5, fan=6)
    device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)

    coordinator = SimpleNamespace(
        data=None,
        async_add_listener=lambda *args: lambda: None,
        last_update_success=True,
    )

    temp_sensor = TemperatureSensor(coordinator, device, "Temperature")
    speed_sensor = CurrentSpeedSensor(coordinator, device, "Current Speed")
    mode_sensor = OperatingModeSensor(coordinator, device, "Operating Mode")

    # Initial values populated from __init__
    assert temp_sensor.native_value == 22.5
    assert speed_sensor.native_value == 6
    assert mode_sensor.native_value == "ON"

    # Test RestoreSensor restore path when current value is None
    device_uninit = ACInfinityDevice(
        SimpleNamespace(address="A4:C1:38:F6:48:F6"),
        state=DeviceInfoEx(type=6, version=3, name="D-8DAQJ", work_type=2, level_on=5, level_off=0, tmp=None),
    )
    temp_sensor_restore = TemperatureSensor(coordinator, device_uninit, "Temperature")
    assert temp_sensor_restore.native_value is None
    temp_sensor_restore.async_get_last_sensor_data = AsyncMock(return_value=SimpleNamespace(native_value=23.4))
    await temp_sensor_restore.async_added_to_hass()
    assert temp_sensor_restore.native_value == 23.4

    # Test None clobber guard: If an update arrives with None, value is preserved
    device.state.tmp = None
    temp_sensor._update_attrs()
    assert temp_sensor.native_value == 22.5

    device.state.fan = None
    device.state.current_speed = None
    speed_sensor._update_attrs()
    assert speed_sensor.native_value == 6


@pytest.mark.asyncio
async def test_passive_airtap_sensor_names_and_filtering():
    """Verify AirTap sensors have explicit _attr_name, fallback values, and skip 0.0% humidity/VPD."""
    from custom_components.ac_infinity_airtap_ble.sensor import (
        FirmwareRevisionSensor,
        HardwareRevisionSensor,
        OnSpeedSensor,
        ProtocolVersionSensor,
        SoftwareRevisionSensor,
        async_setup_entry as sensor_setup_entry,
    )

    state = DeviceInfoEx(
        type=6,
        version=3,
        name="D-8DAQJ",
        work_type=None,
        level_on=None,
        tmp=17.07,
        hum=0.0,
        vpd=0.0,
        fan=9,
        current_speed=9,
    )
    device = ACInfinityDevice(SimpleNamespace(address="A4:C1:38:F6:48:F6"), state=state)
    coordinator = SimpleNamespace(
        data=None,
        async_add_listener=lambda *args: lambda: None,
        last_update_success=True,
    )

    mode_s = OperatingModeSensor(coordinator, device, "Operating Mode")
    on_speed_s = OnSpeedSensor(coordinator, device, "On Speed")
    proto_s = ProtocolVersionSensor(coordinator, device, "Protocol Version")
    fw_s = FirmwareRevisionSensor(coordinator, device, "Firmware Revision")
    sw_s = SoftwareRevisionSensor(coordinator, device, "Software Revision")
    hw_s = HardwareRevisionSensor(coordinator, device, "Hardware Revision")

    assert mode_s._attr_name == "Operating Mode"
    assert mode_s.native_value == "ON"
    assert on_speed_s._attr_name == "On Speed"
    assert on_speed_s.native_value == 9
    assert proto_s._attr_name == "Protocol Version"
    assert proto_s.native_value == 3
    assert fw_s._attr_name == "Firmware Revision"
    assert fw_s.native_value == "SDK_3.4.2"
    assert sw_s._attr_name == "Software Revision"
    assert sw_s.native_value == "2.2.14"
    assert hw_s._attr_name == "Hardware Revision"
    assert hw_s.native_value == "2.0"

    added = []
    hass = SimpleNamespace(
        data={
            "ac_infinity_airtap_ble": {
                "entry1": SimpleNamespace(device=device, coordinator=coordinator)
            }
        }
    )
    entry = SimpleNamespace(entry_id="entry1")
    await sensor_setup_entry(hass, entry, lambda ents: added.extend(ents))
    names = [e._attr_name for e in added]
    assert "Humidity" not in names
    assert "VPD" not in names
    assert "Temperature" in names
    assert "Current Speed" in names
    assert "Operating Mode" in names
    assert "On Speed" in names
    assert "Firmware Revision" in names

