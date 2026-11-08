"""Tests for brand assets, icons.json, and documentation visual identity."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "custom_components" / "ac_infinity_airtap_ble" / "brand"
ICONS_PATH = ROOT / "custom_components" / "ac_infinity_airtap_ble" / "icons.json"
README_PATH = ROOT / "README.md"
INFO_PATH = ROOT / "info.md"


def test_brand_assets_complete() -> None:
    """Verify all 8 standard HACS / Home Assistant brand assets exist and are non-empty."""
    expected_files = [
        "icon.png",
        "icon@2x.png",
        "logo.png",
        "logo@2x.png",
        "dark_icon.png",
        "dark_icon@2x.png",
        "dark_logo.png",
        "dark_logo@2x.png",
    ]
    for filename in expected_files:
        filepath = BRAND_DIR / filename
        assert filepath.is_file(), f"Missing brand asset: {filename}"
        assert filepath.stat().st_size > 0, f"Empty brand asset: {filename}"


def test_icons_json_structure() -> None:
    """Verify icons.json has valid structure matching integration entity platforms."""
    assert ICONS_PATH.is_file(), "icons.json does not exist"
    data = json.loads(ICONS_PATH.read_text(encoding="utf-8"))
    assert "entity" in data, "icons.json must contain 'entity' root key"
    entity = data["entity"]

    required_platforms = ["sensor", "fan", "select", "number", "switch"]
    for platform in required_platforms:
        assert platform in entity, f"Missing platform '{platform}' in icons.json"

    # Check required sensors
    sensors = entity["sensor"]
    assert sensors["temperature"]["default"] == "mdi:thermometer"
    assert sensors["humidity"]["default"] == "mdi:water-percent"
    assert sensors["current_speed"]["default"] == "mdi:fan"
    assert sensors["mode"]["default"] == "mdi:tune-variant"
    assert sensors["vpd"]["default"] == "mdi:water-opacity"
    assert "mdi:" in sensors["signal_strength"]["default"]
    assert sensors["firmware"]["default"] == "mdi:cellphone-arrow-down"

    # Check fan
    assert entity["fan"]["fan"]["default"] == "mdi:fan"

    # Check select
    assert entity["select"]["mode"]["default"] == "mdi:tune-variant"

    # Check number
    assert entity["number"]["speed"]["default"] == "mdi:fan-speed-1"

    # Check switch
    assert entity["switch"]["display"]["default"] == "mdi:brightness-6"


def test_documentation_branding_and_bmac() -> None:
    """Verify README.md and info.md contain brand icon banner and updated BMAC link."""
    readme_text = README_PATH.read_text(encoding="utf-8")
    info_text = INFO_PATH.read_text(encoding="utf-8")

    assert "custom_components/ac_infinity_airtap_ble/brand/icon@2x.png" in readme_text
    assert "custom_components/ac_infinity_airtap_ble/brand/icon@2x.png" in info_text

    assert "https://buymeacoffee.com/srajones" in readme_text
    assert "https://buymeacoffee.com/srajones" in info_text
    assert "JMISm06AD" not in readme_text
    assert "JMISm06AD" not in info_text
