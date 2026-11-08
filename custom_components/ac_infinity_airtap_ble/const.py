from __future__ import annotations

from bleak.exc import BleakError

DOMAIN = "ac_infinity_airtap_ble"
VERSION = "2.2.0-fix.6"

# Domain used up to 1.x. It collided with dalinicus/homeassistant-acinfinity,
# so 2.0.0 moved to the name above. Entries created by 1.x are still in
# .storage/core.config_entries under this domain and are adopted on discovery;
# see legacy.py.
LEGACY_DOMAIN = "ac_infinity"

MANUFACTURER = "AC Infinity"

DEVICE_TIMEOUT = 30
UPDATE_SECONDS = 15

BLEAK_EXCEPTIONS = (AttributeError, BleakError, TimeoutError)

DEVICE_MODEL: dict[int, str] = {
    1: "Controller 67",
    2: "Controller 76",
    3: "Smart Hygrometer",
    4: "Smart Hygrometer B2",
    5: "Smart Hygrometer B1",
    6: "Airtap Series",
    7: "Controller 69 Bluetooth",
    8: "Controller 69 Wi-Fi",
    9: "Controller 79",
    11: "Controller 69 Pro",
    12: "Controller 75",
    14: "Mini Smart Hygrometer A2",
    15: "Mini Smart Hygrometer A1",
    16: "Controller 79 Pro",
    17: "Controller 75 Pro",
    18: "Controller 69 Pro+",
    20: "Controller AI+",
    21: "Outlet AI",
    22: "Outlet AI+",
    23: "Airtitan Series",
    24: "VPD Smart Hygrometer B2",
    25: "VPD Smart Hygrometer B1",
    33: "Through-Wall Fan",
    39: "Smart AC 8K",
    40: "Smart AC 12K",
    48: "Airtap AI Series",
    49: "Desktop Fan",
    50: "Pedestal Fan",
    64: "Cloudcom 6",
}

TELINK_CHIP_MODELS: frozenset[int] = frozenset({4, 5, 6, 14, 15, 24, 25})
LEGACY_MODELS: frozenset[int] = frozenset({1, 2, 6})
AIRTAP_MODELS: frozenset[int] = frozenset({6, 48})
FAMILY_E_MODELS: set[int] = {7, 8, 9, 11, 12, 16, 17, 18, 20}
MULTIPORT_MODELS: frozenset[int] = frozenset({7, 8, 9, 11, 12, 16, 17, 18, 20})
TELINK_DEVICE_TYPES: frozenset[int] = frozenset({4, 5, 6, 14, 15, 24, 25})
TELINK_OTA_FORBIDDEN_HANDLES: frozenset[int] = frozenset({27, 28})
TELINK_OTA_FORBIDDEN_UUIDS: frozenset[str] = frozenset({
    "00010203-0405-0607-0809-0a0b0c0d1912",
    "00010203-0405-0607-0809-0a0b0c0d2b12",
})


def get_device_model_name(device_type: int | None) -> str:
    """Return the display model name for a device type with safe fallback."""
    if device_type is None:
        return "AC Infinity Device"
    return DEVICE_MODEL.get(device_type, f"AC Infinity Device (Type {device_type})")


CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 15
CONF_PASSIVE_ONLY = "passive_only"
CONF_ATTEMPT_PAIRING = "attempt_pairing"
AC_INFINITY_SERVICE_UUID = "70d51000-2c7f-4e75-ae8a-d758951ce4e0"

