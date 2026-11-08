from bleak.exc import BleakError

class CharacteristicMissingError(Exception):
    """Raised when a characteristic is missing."""


class BLEStepError(BleakError):
    """A BLE failure with a config-flow translation key and operation context."""

    def __init__(self, step: str, code: str, detail: str):
        self.step = step
        self.code = code
        super().__init__(f"{step}: {detail}")
