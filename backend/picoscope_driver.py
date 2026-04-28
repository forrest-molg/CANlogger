from __future__ import annotations

from config import DeviceConfig
from models import WaveformWindow


class PicoScope2204ADriver:
    """Placeholder for PicoSDK-backed implementation.

    This class documents the integration contract used by the capture service.
    Replace methods with real PicoSDK calls once hardware is available.
    """

    def __init__(self) -> None:
        self._opened: dict[str, DeviceConfig] = {}

    def open_device(self, device: DeviceConfig) -> None:
        self._opened[device.serial] = device

    def close_device(self, serial: str) -> None:
        self._opened.pop(serial, None)

    def capture_window(self, serial: str) -> WaveformWindow:
        raise NotImplementedError("PicoScope capture is not implemented yet. Use simulator mode.")
