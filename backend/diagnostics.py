from __future__ import annotations

import ctypes
import logging
import os
import stat
from ctypes.util import find_library
from pathlib import Path

logger = logging.getLogger(__name__)


def _status_name(code: int) -> str:
    try:
        from picosdk.constants import PICO_STATUS  # type: ignore

        for name, value in PICO_STATUS.items():
            if value == code:
                return name
    except Exception:
        pass
    return f"UNKNOWN_{code}"


def _check_library(name: str) -> dict:
    result: dict = {
        "name": name,
        "find_library": None,
        "loaded": False,
        "load_error": None,
    }
    found = find_library(name)
    result["find_library"] = found
    if not found:
        return result

    try:
        ctypes.CDLL(found)
        result["loaded"] = True
    except Exception as exc:
        result["load_error"] = str(exc)
    return result


def _scan_usb_sysfs() -> list[dict]:
    devices: list[dict] = []
    sys_usb = Path("/sys/bus/usb/devices")
    if not sys_usb.exists():
        return devices

    for dev in sys_usb.iterdir():
        id_vendor = dev / "idVendor"
        id_product = dev / "idProduct"
        if not id_vendor.exists() or not id_product.exists():
            continue

        try:
            vendor = id_vendor.read_text(encoding="utf-8").strip().lower()
            product = id_product.read_text(encoding="utf-8").strip().lower()
        except Exception:
            continue

        if vendor != "0ce9":
            continue

        product_name = ""
        manufacturer = ""
        serial = ""
        busnum = ""
        devnum = ""
        driver = ""
        for key, path in (
            ("product", dev / "product"),
            ("manufacturer", dev / "manufacturer"),
            ("serial", dev / "serial"),
            ("busnum", dev / "busnum"),
            ("devnum", dev / "devnum"),
        ):
            try:
                value = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            except Exception:
                value = ""
            if key == "product":
                product_name = value
            elif key == "manufacturer":
                manufacturer = value
            elif key == "serial":
                serial = value
            elif key == "busnum":
                busnum = value
            else:
                devnum = value

        try:
            driver_link = dev / "driver"
            if driver_link.exists():
                driver = driver_link.resolve().name
        except Exception:
            driver = ""

        devnode = ""
        devnode_info = {
            "exists": False,
            "readable": False,
            "writable": False,
            "mode": None,
        }
        if busnum and devnum:
            devnode = f"/dev/bus/usb/{int(busnum):03d}/{int(devnum):03d}"
            devnode_path = Path(devnode)
            devnode_info["exists"] = devnode_path.exists()
            devnode_info["readable"] = os.access(devnode, os.R_OK)
            devnode_info["writable"] = os.access(devnode, os.W_OK)
            if devnode_path.exists():
                try:
                    mode = stat.S_IMODE(devnode_path.stat().st_mode)
                    devnode_info["mode"] = oct(mode)
                except Exception:
                    pass

        devices.append(
            {
                "sysfs_node": dev.name,
                "vendor_id": vendor,
                "product_id": product,
                "manufacturer": manufacturer,
                "product": product_name,
                "serial": serial,
                "busnum": busnum,
                "devnum": devnum,
                "devnode": devnode,
                "devnode_access": devnode_info,
                "kernel_driver": driver,
            }
        )

    return devices


def _probe_ps2000a() -> dict:
    info: dict = {
        "import_ok": False,
        "enumerate": None,
        "open_unit": None,
    }

    try:
        from picosdk.ps2000a import ps2000a as ps  # type: ignore

        info["import_ok"] = True

        count = ctypes.c_int16(0)
        serials_len = ctypes.c_int16(1024)
        serials = ctypes.create_string_buffer(1024)
        enum_status = ps.ps2000aEnumerateUnits(ctypes.byref(count), serials, ctypes.byref(serials_len))
        serial_list = [s for s in serials.value.decode("utf-8", errors="ignore").split(",") if s]
        info["enumerate"] = {
            "status": int(enum_status),
            "status_name": _status_name(int(enum_status)),
            "count": int(count.value),
            "serials": serial_list,
        }

        chandle = ctypes.c_int16()
        open_status = int(ps.ps2000aOpenUnit(ctypes.byref(chandle), None))
        open_payload = {
            "status": open_status,
            "status_name": _status_name(open_status),
            "handle": int(chandle.value),
            "changed_power_source": False,
            "change_power_source_status": None,
            "change_power_source_status_name": None,
        }

        # Follows PicoTech examples that retry with ChangePowerSource for power-related statuses.
        if open_status in (282, 286):
            try:
                ps_status = int(ps.ps2000aChangePowerSource(chandle, open_status))
                open_payload["changed_power_source"] = True
                open_payload["change_power_source_status"] = ps_status
                open_payload["change_power_source_status_name"] = _status_name(ps_status)
            except Exception as exc:
                open_payload["change_power_source_error"] = str(exc)

        if chandle.value > 0:
            try:
                close_status = int(ps.ps2000aCloseUnit(chandle))
                open_payload["close_status"] = close_status
                open_payload["close_status_name"] = _status_name(close_status)
            except Exception as exc:
                open_payload["close_error"] = str(exc)

        info["open_unit"] = open_payload
    except Exception as exc:
        info["error"] = str(exc)

    return info


def _probe_ps2000() -> dict:
    info: dict = {
        "import_ok": False,
        "open_unit": None,
    }
    try:
        from picosdk.ps2000 import ps2000 as ps  # type: ignore

        info["import_ok"] = True

        handle = int(ps.ps2000_open_unit())
        payload = {
            "handle_or_status": handle,
        }

        if handle > 0:
            try:
                close_status = int(ps.ps2000_close_unit(ctypes.c_int16(handle)))
                payload["close_status"] = close_status
            except Exception as exc:
                payload["close_error"] = str(exc)

        info["open_unit"] = payload
    except Exception as exc:
        info["error"] = str(exc)

    return info


def run_pico_diagnostics(include_driver_probes: bool = False) -> dict:
    logger.info("Running Pico diagnostics endpoint")

    usb_nodes = sorted(Path("/dev/bus/usb").glob("*/*")) if Path("/dev/bus/usb").exists() else []
    picoscope_libs = []
    pico_lib_dir = Path("/opt/picoscope/lib")
    if pico_lib_dir.exists():
        picoscope_libs = sorted(p.name for p in pico_lib_dir.glob("libps*.so*"))

    result = {
        "environment": {
            "ld_library_path": os.getenv("LD_LIBRARY_PATH", ""),
            "runtime": {
                "uid": os.getuid(),
                "gid": os.getgid(),
                "groups": os.getgroups(),
            },
            "paths": {
                "/dev/bus/usb_exists": Path("/dev/bus/usb").exists(),
                "/run/udev_exists": Path("/run/udev").exists(),
                "/opt/picoscope/lib_exists": pico_lib_dir.exists(),
            },
            "usb_node_count": len(usb_nodes),
        },
        "usb_visibility": {
            "picotech_devices": _scan_usb_sysfs(),
        },
        "library_resolution": {
            "checked": [
                _check_library("ps2000a"),
                _check_library("ps2000"),
                _check_library("picoipp"),
                _check_library("usb-1.0"),
            ],
            "opt_picoscope_libs": picoscope_libs,
        },
        "driver_probes_enabled": include_driver_probes,
    }

    if include_driver_probes:
        result["driver_probes"] = {
            "ps2000a": _probe_ps2000a(),
            "ps2000": _probe_ps2000(),
        }

    logger.debug("Pico diagnostics result: %s", result)
    return result
