import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
# Import device.py without booting HA; only its exception type is needed here.
for name, path in [("custom_components", ROOT / "custom_components"),
                   ("custom_components.ac_infinity_airtap_ble", ROOT / "custom_components/ac_infinity_airtap_ble")]:
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
ha = ModuleType("homeassistant")
ha.__path__ = []
exceptions = ModuleType("homeassistant.exceptions")
exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
sys.modules["homeassistant"] = ha
sys.modules["homeassistant.exceptions"] = exceptions
