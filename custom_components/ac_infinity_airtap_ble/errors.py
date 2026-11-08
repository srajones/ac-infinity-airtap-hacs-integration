"""Stable setup error keys, independent of Home Assistant's config-flow UI."""
from .vendor.ac_infinity_ble.exceptions import BLEStepError


def config_error(error: Exception) -> str:
    if isinstance(error, BLEStepError):
        return error.code
    if isinstance(error, TimeoutError):
        return "no_response"
    if isinstance(error, ValueError):
        return "unsupported_model"
    return "connect_failed"
