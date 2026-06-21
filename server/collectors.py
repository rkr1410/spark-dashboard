"""Telemetry collectors for the Spark dashboard.

The collectors intentionally use the standard library only. On DGX Spark the
server should read what the OS already exposes and avoid installing packages
just to get the first live dashboard running.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECTED_TOTAL_GB = 128
CPU_CORE_COUNT = 20
THERMAL_ROOT = Path("/sys/class/thermal")
MEMINFO_PATH = Path("/proc/meminfo")
PROC_STAT_PATH = Path("/proc/stat")
CPU_PREVIOUS: dict[int, tuple[int, int]] = {}
NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
NVML: ctypes.CDLL | None = None
NVML_LOAD_ATTEMPTED = False
NVML_LOAD_ERROR: str | None = None
NVML_LIBRARY_PATH: str | None = None


class NvmlUtilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("memory", ctypes.c_uint),
    ]


def collect_snapshot(use_mock: bool = False) -> dict[str, Any]:
    if use_mock or should_use_mock_snapshot():
        return mock_snapshot()

    memory = read_system_memory()
    thermal = read_system_thermal()
    cpu = read_cpu_utilization()
    gpu = read_gpu_nvml()

    return {
        "timestamp": utc_timestamp(),
        "source": {
            "mode": "live",
            "systemMemory": memory["source"],
            "systemTemp": thermal["source"],
            "systemCpu": cpu["source"],
            "gpu": gpu["source"],
        },
        "system": {
            "memory": {
                "usedGb": memory["usedGb"],
                "totalGb": memory["totalGb"],
            },
            "temp": {
                "valueC": thermal["valueC"],
                "maxC": thermal["maxC"],
            },
            "cpu": {
                "avgPct": cpu["avgPct"],
                "cores": cpu["cores"],
            },
            "power": {
                "valueW": None,
                "maxW": None,
                "available": False,
            },
        },
        "gpu": {
            "utilization": {
                "valuePct": gpu["utilizationPct"],
                "maxPct": 100,
            },
            "temp": {
                "valueC": gpu["tempC"],
                "maxC": None,
            },
            "power": {
                "valueW": gpu["powerW"],
                "maxW": gpu["powerLimitW"],
            },
        },
    }


def build_startup_report(use_mock: bool = False) -> list[str]:
    snapshot = collect_snapshot(use_mock=use_mock)
    source = snapshot["source"]
    system = snapshot["system"]
    gpu = snapshot["gpu"]

    return [
        "Telemetry startup:",
        f"  mode: {source['mode']}",
        f"  NVML: {nvml_status_text(use_mock=use_mock)}",
        (
            "  initial system: "
            f"memory {format_gb(system['memory']['usedGb'])}/{format_gb(system['memory']['totalGb'])}, "
            f"temp {format_c(system['temp']['valueC'])}, "
            f"cpu {format_pct(system['cpu']['avgPct'])}"
        ),
        (
            "  initial GPU: "
            f"util {format_pct(gpu['utilization']['valuePct'])}, "
            f"temp {format_c(gpu['temp']['valueC'])}, "
            f"power {format_w(gpu['power']['valueW'])}, "
            f"limit {format_w(gpu['power']['maxW'])}, "
            f"source {source['gpu']}"
        ),
    ]


def should_use_mock_snapshot() -> bool:
    return not MEMINFO_PATH.exists() and load_nvml() is None


def read_system_memory() -> dict[str, Any]:
    meminfo = read_meminfo()
    total_kb = meminfo.get("MemTotal")
    available_kb = meminfo.get("MemAvailable")

    if total_kb is None or available_kb is None:
        return {
            "usedGb": None,
            "totalGb": None,
            "source": "unavailable",
        }

    used_kb = max(total_kb - available_kb, 0)

    return {
        # /proc/meminfo labels KiB as kB. Dividing the reported value by 1e6
        # matches the DGX dashboard's 128 GB-style display better than GiB.
        "usedGb": round(used_kb / 1_000_000, 2),
        "totalGb": round(total_kb / 1_000_000) or PROJECTED_TOTAL_GB,
        "source": "proc_meminfo",
    }


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}

    try:
        with MEMINFO_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()

                if parts and parts[0].isdigit():
                    values[key] = int(parts[0])
    except OSError:
        return {}

    return values


def read_system_thermal() -> dict[str, Any]:
    temps: list[float] = []
    criticals: list[float] = []

    if not THERMAL_ROOT.exists():
        return {
            "valueC": None,
            "maxC": None,
            "source": "unavailable",
        }

    for zone in sorted(THERMAL_ROOT.glob("thermal_zone*"), key=thermal_zone_index):
        temp = read_millivalue(zone / "temp")

        if temp is not None:
            temps.append(temp / 1000)

        critical = read_critical_trip(zone)

        if critical is not None:
            criticals.append(critical / 1000)

    return {
        "valueC": round(max(temps), 1) if temps else None,
        "maxC": round(min(criticals), 1) if criticals else None,
        "source": "thermal_zones" if temps else "unavailable",
    }


def thermal_zone_index(path: Path) -> int:
    suffix = path.name.removeprefix("thermal_zone")

    return int(suffix) if suffix.isdigit() else 0


def read_critical_trip(zone: Path) -> int | None:
    for trip_type in sorted(zone.glob("trip_point_*_type")):
        try:
            if trip_type.read_text(encoding="utf-8").strip() != "critical":
                continue
        except OSError:
            continue

        temp_path = zone / trip_type.name.replace("_type", "_temp")
        critical = read_millivalue(temp_path)

        if critical is not None:
            return critical

    return None


def read_millivalue(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    return int(value) if value.lstrip("-").isdigit() else None


def read_cpu_utilization() -> dict[str, Any]:
    raw_times = read_cpu_times()
    cores: list[dict[str, float | int | None]] = []

    for index in range(CPU_CORE_COUNT):
        times = raw_times.get(index)
        value: float | None = None

        if times is not None:
            idle, total = times
            previous = CPU_PREVIOUS.get(index)

            if previous is not None:
                previous_idle, previous_total = previous
                total_delta = total - previous_total
                idle_delta = idle - previous_idle

                if total_delta > 0:
                    value = clamp_pct(((total_delta - idle_delta) / total_delta) * 100)
            elif total > 0:
                value = clamp_pct(((total - idle) / total) * 100)

            CPU_PREVIOUS[index] = times

        cores.append(
            {
                "index": index,
                "valuePct": round(value, 1) if value is not None else None,
            },
        )

    values = [core["valuePct"] for core in cores if isinstance(core["valuePct"], float)]

    return {
        "avgPct": round(sum(values) / len(values), 1) if values else None,
        "cores": cores,
        "source": "proc_stat" if raw_times else "unavailable",
    }


def read_cpu_times() -> dict[int, tuple[int, int]]:
    times: dict[int, tuple[int, int]] = {}

    try:
        lines = PROC_STAT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return times

    for line in lines:
        parts = line.split()

        if not parts or not parts[0].startswith("cpu") or parts[0] == "cpu":
            continue

        suffix = parts[0][3:]

        if not suffix.isdigit():
            continue

        values = [int(part) for part in parts[1:] if part.lstrip("-").isdigit()]

        if len(values) < 4:
            continue

        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        times[int(suffix)] = (idle, total)

    return times


def clamp_pct(value: float) -> float:
    return min(max(value, 0.0), 100.0)


def read_gpu_nvml() -> dict[str, Any]:
    nvml = load_nvml()

    if nvml is None:
        return empty_gpu("nvml_unavailable")

    handle = nvml_device_handle(nvml, 0)

    if handle is None:
        return empty_gpu("nvml_no_device")

    return {
        "utilizationPct": nvml_gpu_utilization(nvml, handle),
        "tempC": nvml_gpu_temperature(nvml, handle),
        "powerW": nvml_gpu_power_watts(nvml, handle, "nvmlDeviceGetPowerUsage"),
        "powerLimitW": nvml_gpu_power_watts(nvml, handle, "nvmlDeviceGetPowerManagementLimit"),
        "source": "nvml",
    }


def load_nvml() -> ctypes.CDLL | None:
    global NVML, NVML_LIBRARY_PATH, NVML_LOAD_ATTEMPTED, NVML_LOAD_ERROR

    if NVML_LOAD_ATTEMPTED:
        return NVML

    NVML_LOAD_ATTEMPTED = True
    candidates = [ctypes.util.find_library("nvidia-ml"), "libnvidia-ml.so.1", "libnvidia-ml.so"]

    for candidate in candidates:
        if not candidate:
            continue

        try:
            nvml = ctypes.CDLL(candidate)
            configure_nvml(nvml)
            result = nvml.nvmlInit_v2()

            if result == NVML_SUCCESS:
                NVML = nvml
                NVML_LIBRARY_PATH = str(candidate)
                NVML_LOAD_ERROR = None
                return NVML

            NVML_LOAD_ERROR = f"nvmlInit_v2 returned {result}"
        except OSError as error:
            NVML_LOAD_ERROR = str(error)
        except AttributeError as error:
            NVML_LOAD_ERROR = str(error)

    return None


def nvml_status_text(use_mock: bool = False) -> str:
    if use_mock:
        return "skipped (--mock)"

    nvml = load_nvml()

    if nvml is not None:
        return f"loaded {NVML_LIBRARY_PATH or getattr(nvml, '_name', 'libnvidia-ml')}"

    return f"unavailable ({short_text(NVML_LOAD_ERROR or 'library not found')})"


def configure_nvml(nvml: ctypes.CDLL) -> None:
    nvml.nvmlInit_v2.restype = ctypes.c_int

    nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int

    nvml.nvmlDeviceGetUtilizationRates.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(NvmlUtilization),
    ]
    nvml.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int

    nvml.nvmlDeviceGetTemperature.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint),
    ]
    nvml.nvmlDeviceGetTemperature.restype = ctypes.c_int

    configure_optional_uint_getter(nvml, "nvmlDeviceGetPowerUsage")
    configure_optional_uint_getter(nvml, "nvmlDeviceGetPowerManagementLimit")


def configure_optional_uint_getter(nvml: ctypes.CDLL, name: str) -> None:
    function = getattr(nvml, name, None)

    if function is None:
        return

    function.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    function.restype = ctypes.c_int


def nvml_device_handle(nvml: ctypes.CDLL, index: int) -> ctypes.c_void_p | None:
    handle = ctypes.c_void_p()
    result = nvml.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(index), ctypes.byref(handle))

    return handle if result == NVML_SUCCESS else None


def nvml_gpu_utilization(nvml: ctypes.CDLL, handle: ctypes.c_void_p) -> float | None:
    utilization = NvmlUtilization()
    result = nvml.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(utilization))

    if result != NVML_SUCCESS:
        return None

    return float(utilization.gpu)


def nvml_gpu_temperature(nvml: ctypes.CDLL, handle: ctypes.c_void_p) -> float | None:
    temperature = ctypes.c_uint()
    result = nvml.nvmlDeviceGetTemperature(
        handle,
        ctypes.c_uint(NVML_TEMPERATURE_GPU),
        ctypes.byref(temperature),
    )

    if result != NVML_SUCCESS:
        return None

    return float(temperature.value)


def nvml_gpu_power_watts(
    nvml: ctypes.CDLL,
    handle: ctypes.c_void_p,
    function_name: str,
) -> float | None:
    value = ctypes.c_uint()
    function = getattr(nvml, function_name, None)

    if function is None:
        return None

    result = function(handle, ctypes.byref(value))

    if result != NVML_SUCCESS:
        return None

    return round(value.value / 1000, 2)


def empty_gpu(source: str) -> dict[str, Any]:
    return {
        "utilizationPct": None,
        "tempC": None,
        "powerW": None,
        "powerLimitW": None,
        "source": source,
    }


def mock_snapshot() -> dict[str, Any]:
    t = time.monotonic()
    cpu_cores = mock_cpu_cores(t)

    return {
        "timestamp": utc_timestamp(),
        "source": {
            "mode": "mock",
            "systemMemory": "mock",
            "systemTemp": "mock",
            "systemCpu": "mock",
            "gpu": "mock",
        },
        "system": {
            "memory": {
                "usedGb": round(64 + math.sin(t * 0.31) * 2.8, 2),
                "totalGb": PROJECTED_TOTAL_GB,
            },
            "temp": {
                "valueC": round(75 + math.sin(t * 0.42 + 1.1) * 2.1, 1),
                "maxC": 104.8,
            },
            "cpu": {
                "avgPct": average_cpu(cpu_cores),
                "cores": cpu_cores,
            },
        },
        "gpu": {
            "utilization": {
                "valuePct": round(clamp_pct(50 + math.sin(t * 0.38 + 3.1) * 14)),
                "maxPct": 100,
            },
            "temp": {
                "valueC": round(75 + math.sin(t * 0.35 + 0.4) * 2.0, 1),
                "maxC": None,
            },
            "power": {
                "valueW": round(70 + math.sin(t * 0.58 + 2.0) * 8, 1),
                "maxW": None,
            },
        },
    }


def mock_cpu_cores(t: float) -> list[dict[str, float | int]]:
    cores: list[dict[str, float | int]] = []

    for index in range(CPU_CORE_COUNT):
        cores.append(
            {
                "index": index,
                "valuePct": round(clamp_pct(50 + math.sin(t * 0.39 + index * 0.71) * 13), 1),
            },
        )

    return cores


def average_cpu(cores: list[dict[str, float | int]]) -> float:
    values = [float(core["valuePct"]) for core in cores]

    return round(sum(values) / len(values), 1)


def format_gb(value: Any) -> str:
    return f"{value:.2f} GB" if isinstance(value, int | float) else "N/A"


def format_c(value: Any) -> str:
    return f"{value:.1f} C" if isinstance(value, int | float) else "N/A"


def format_w(value: Any) -> str:
    return f"{value:.2f} W" if isinstance(value, int | float) else "N/A"


def format_pct(value: Any) -> str:
    return f"{value:.1f}%" if isinstance(value, int | float) else "N/A"


def short_text(value: str, max_length: int = 140) -> str:
    return value if len(value) <= max_length else value[: max_length - 3] + "..."


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
