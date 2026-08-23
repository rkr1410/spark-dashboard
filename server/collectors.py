"""Telemetry collectors for the Spark dashboard.

The collectors intentionally use the standard library only. On DGX Spark the
server should read what the OS already exposes and avoid installing packages
just to get the first live dashboard running.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECTED_TOTAL_GB = 128
CPU_CORE_COUNT = 20
THERMAL_ROOT = Path("/sys/class/thermal")
MEMINFO_PATH = Path("/proc/meminfo")
PROC_STAT_PATH = Path("/proc/stat")
NET_DEV_PATH = Path("/proc/net/dev")
DISKSTATS_PATH = Path("/proc/diskstats")
SYS_BLOCK_ROOT = Path("/sys/block")
SGLANG_METRICS_URL = "http://localhost:8000/metrics"
SGLANG_CONTEXT_TOKENS = 262_144
CPU_PREVIOUS: dict[int, tuple[int, int]] = {}
IO_PREVIOUS: dict[str, tuple[float, int, int]] = {}
PROCESS_MEMORY_CACHE_TTL_SECONDS = 5.0
PROCESS_MEMORY_CACHE: tuple[float, dict[str, Any]] | None = None
SGLANG_PREFILL_COUNTERS_PREVIOUS: tuple[float, float] | None = None
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
    process_memory = read_cuda_process_memory()
    thermal = read_system_thermal()
    cpu = read_cpu_utilization()
    network = read_network_io()
    disk = read_disk_io()
    gpu = read_gpu_nvml()
    inference = read_sglang_metrics()

    return {
        "timestamp": utc_timestamp(),
        "source": {
            "mode": "live",
            "systemMemory": memory["source"],
            "processMemory": process_memory["source"],
            "systemTemp": thermal["source"],
            "systemCpu": cpu["source"],
            "systemNetwork": network["source"],
            "systemDisk": disk["source"],
            "gpu": gpu["source"],
            "inference": inference["source"],
        },
        "system": {
            "memory": {
                "usedGb": memory["usedGb"],
                "totalGb": memory["totalGb"],
                "residentGb": process_memory["residentGb"],
                "fileGb": process_memory["fileGb"],
                "anonGb": process_memory["anonGb"],
                "cudaGb": process_memory["cudaGb"],
                "processCount": process_memory["processCount"],
                "processes": process_memory["processes"],
            },
            "temp": {
                "valueC": thermal["valueC"],
                "maxC": thermal["maxC"],
            },
            "cpu": {
                "avgPct": cpu["avgPct"],
                "cores": cpu["cores"],
            },
            "network": {
                "rxBytesPerSec": network["rxBytesPerSec"],
                "txBytesPerSec": network["txBytesPerSec"],
            },
            "disk": {
                "readBytesPerSec": disk["readBytesPerSec"],
                "writeBytesPerSec": disk["writeBytesPerSec"],
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
        "inference": inference,
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
            f"resident {format_gb(system['memory'].get('residentGb'))}, "
            f"cuda {format_gb(system['memory'].get('cudaGb'))}, "
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
        (
            "  initial inference: "
            f"source {source.get('inference', 'unavailable')}, "
            f"runtime {snapshot.get('inference', {}).get('runtime', 'N/A')}, "
            f"available {snapshot.get('inference', {}).get('available', False)}"
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


def read_sglang_metrics() -> dict[str, Any]:
    global SGLANG_PREFILL_COUNTERS_PREVIOUS

    try:
        with urllib.request.urlopen(SGLANG_METRICS_URL, timeout=0.8) as response:
            body = response.read(1_000_000).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, TimeoutError):
        return empty_inference("sglang_metrics_unavailable")

    values = parse_prometheus_metrics(
        body,
        {
            "sglang:gen_throughput": "genThroughput",
            "sglang:num_used_tokens": "numUsedTokens",
            "sglang:max_total_num_tokens": "maxTotalNumTokens",
            "sglang:spec_accept_rate": "specAcceptRate",
            "sglang:spec_accept_length": "specAcceptLength",
            "sglang:cache_hit_rate": "cacheHitRate",
            "sglang:num_running_reqs": "numRunningReqs",
            "sglang:num_queue_reqs": "numQueueReqs",
        },
    )
    realtime_tokens = parse_sglang_realtime_tokens(body)
    prefix_hit_rate = prefix_hit_rate_from_counters(
        realtime_tokens.get("prefill_cache"),
        realtime_tokens.get("prefill_compute"),
    )

    required = ("genThroughput", "numRunningReqs", "numQueueReqs")

    if any(values.get(key) is None for key in required):
        return empty_inference("sglang_metrics_missing")

    return {
        "available": True,
        "runtime": "sglang",
        "model": parse_prometheus_label(body, "model_name") or "qwen3.8-27b",
        "contextTokens": SGLANG_CONTEXT_TOKENS,
        "genThroughput": values.get("genThroughput"),
        "numUsedTokens": values.get("numUsedTokens"),
        "maxTotalNumTokens": values.get("maxTotalNumTokens"),
        "specAcceptRate": values.get("specAcceptRate"),
        "specAcceptLength": values.get("specAcceptLength"),
        "prefixHitRate": prefix_hit_rate,
        "cacheHitRate": values.get("cacheHitRate"),
        "prefillCacheTokens": realtime_tokens.get("prefill_cache"),
        "prefillComputeTokens": realtime_tokens.get("prefill_compute"),
        "numRunningReqs": values.get("numRunningReqs"),
        "numQueueReqs": values.get("numQueueReqs"),
        "source": "sglang_metrics",
    }


def empty_inference(source: str) -> dict[str, Any]:
    return {
        "available": False,
        "runtime": "sglang",
        "model": None,
        "contextTokens": SGLANG_CONTEXT_TOKENS,
        "genThroughput": None,
        "numUsedTokens": None,
        "maxTotalNumTokens": None,
        "specAcceptRate": None,
        "specAcceptLength": None,
        "prefixHitRate": None,
        "cacheHitRate": None,
        "prefillCacheTokens": None,
        "prefillComputeTokens": None,
        "numRunningReqs": None,
        "numQueueReqs": None,
        "source": source,
    }


def parse_prometheus_metrics(body: str, names: dict[str, str]) -> dict[str, float | None]:
    values: dict[str, float | None] = {target: None for target in names.values()}

    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue

        metric, _, rest = line.partition(" ")
        metric_name = metric.split("{", 1)[0]
        target = names.get(metric_name)

        if target is None:
            continue

        value_text = rest.strip().split(" ", 1)[0]
        value = parse_float(value_text)

        if value is not None:
            values[target] = value

    return values


def parse_sglang_realtime_tokens(body: str) -> dict[str, float]:
    counters: dict[str, float] = {}

    for line in body.splitlines():
        if not line.startswith("sglang:realtime_tokens_total{"):
            continue

        mode = parse_metric_label(line, "mode")

        if mode not in {"prefill_cache", "prefill_compute"}:
            continue

        _, _, rest = line.partition(" ")
        value_text = rest.strip().split(" ", 1)[0]
        value = parse_float(value_text)

        if value is not None:
            counters[mode] = value

    return counters


def prefix_hit_rate_from_counters(
    prefill_cache: float | None,
    prefill_compute: float | None,
) -> float | None:
    global SGLANG_PREFILL_COUNTERS_PREVIOUS

    if prefill_cache is None or prefill_compute is None:
        return None

    previous = SGLANG_PREFILL_COUNTERS_PREVIOUS
    SGLANG_PREFILL_COUNTERS_PREVIOUS = (prefill_cache, prefill_compute)

    if previous is None:
        total = prefill_cache + prefill_compute

        return prefill_cache / total if total > 0 else None

    previous_cache, previous_compute = previous
    cache_delta = prefill_cache - previous_cache
    compute_delta = prefill_compute - previous_compute

    if cache_delta < 0 or compute_delta < 0:
        total = prefill_cache + prefill_compute

        return prefill_cache / total if total > 0 else None

    total_delta = cache_delta + compute_delta

    return cache_delta / total_delta if total_delta > 0 else None


def parse_prometheus_label(body: str, label_name: str) -> str | None:
    needle = f'{label_name}="'

    for line in body.splitlines():
        if line.startswith("#") or needle not in line:
            continue

        value = parse_metric_label(line, label_name)

        if value is not None:
            return value

    return None


def parse_metric_label(line: str, label_name: str) -> str | None:
    needle = f'{label_name}="'

    if needle not in line:
        return None

    after = line.split(needle, 1)[1]
    value, separator, _ = after.partition('"')

    return value if separator else None


def read_cuda_process_memory() -> dict[str, Any]:
    global PROCESS_MEMORY_CACHE

    now = time.monotonic()

    if PROCESS_MEMORY_CACHE and now - PROCESS_MEMORY_CACHE[0] < PROCESS_MEMORY_CACHE_TTL_SECONDS:
        return PROCESS_MEMORY_CACHE[1]

    processes = query_cuda_processes()
    totals = {
        "residentGb": 0.0,
        "fileGb": 0.0,
        "anonGb": 0.0,
        "cudaGb": 0.0,
        "processCount": len(processes),
        "processes": processes,
        "source": "nvidia_smi_proc" if processes else "nvidia_smi_proc_empty",
    }

    for process in processes:
        pid = process["pid"]
        proc_memory = read_proc_process_memory(pid)

        process.update(proc_memory)
        totals["residentGb"] += proc_memory.get("residentGb") or 0
        totals["fileGb"] += proc_memory.get("fileGb") or 0
        totals["anonGb"] += proc_memory.get("anonGb") or 0
        totals["cudaGb"] += process.get("cudaGb") or 0

    for key in ("residentGb", "fileGb", "anonGb", "cudaGb"):
        totals[key] = round(totals[key], 2)

    PROCESS_MEMORY_CACHE = (now, totals)
    return totals


def query_cuda_processes() -> list[dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            encoding="utf-8",
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    processes: list[dict[str, Any]] = []

    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]

        if len(parts) != 3 or not parts[0].isdigit():
            continue

        used_mib = parse_float(parts[2])

        processes.append(
            {
                "pid": int(parts[0]),
                "name": short_process_name(parts[1]),
                "cudaGb": round(used_mib / 1024, 2) if used_mib is not None else None,
            },
        )

    return processes


def read_proc_process_memory(pid: int) -> dict[str, float | None]:
    values = read_proc_key_values(Path("/proc") / str(pid) / "smaps_rollup")

    if values:
        resident_kb = values.get("Pss") or values.get("Rss")
        file_kb = values.get("Pss_File")
        anon_kb = values.get("Pss_Anon") or values.get("Anonymous")

        return {
            "residentGb": kb_to_gb(resident_kb),
            "fileGb": kb_to_gb(file_kb),
            "anonGb": kb_to_gb(anon_kb),
        }

    status = read_proc_key_values(Path("/proc") / str(pid) / "status")

    return {
        "residentGb": kb_to_gb(status.get("VmRSS")),
        "fileGb": kb_to_gb(status.get("RssFile")),
        "anonGb": kb_to_gb(status.get("RssAnon")),
    }


def read_proc_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values

    for line in lines:
        key, separator, rest = line.partition(":")

        if not separator:
            continue

        parts = rest.strip().split()

        if parts and parts[0].isdigit():
            values[key] = int(parts[0])

    return values


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def kb_to_gb(value: int | None) -> float | None:
    return round(value / 1_000_000, 2) if value is not None else None


def short_process_name(value: str) -> str:
    return Path(value).name or value


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


def read_network_io() -> dict[str, Any]:
    rx_bytes = 0
    tx_bytes = 0
    interface_count = 0

    try:
        lines = NET_DEV_PATH.read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return unavailable_io("rxBytesPerSec", "txBytesPerSec")

    for line in lines:
        interface, separator, values_text = line.partition(":")

        if not separator or interface.strip() == "lo":
            continue

        values = values_text.split()

        if len(values) < 9 or not values[0].isdigit() or not values[8].isdigit():
            continue

        rx_bytes += int(values[0])
        tx_bytes += int(values[8])
        interface_count += 1

    if interface_count == 0:
        return unavailable_io("rxBytesPerSec", "txBytesPerSec")

    rx_rate, tx_rate = rates_from_counters("network", rx_bytes, tx_bytes)

    return {
        "rxBytesPerSec": rx_rate,
        "txBytesPerSec": tx_rate,
        "source": "proc_net_dev",
    }


def read_disk_io() -> dict[str, Any]:
    sectors_read = 0
    sectors_written = 0
    device_count = 0

    try:
        lines = DISKSTATS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return unavailable_io("readBytesPerSec", "writeBytesPerSec")

    for line in lines:
        parts = line.split()

        if len(parts) < 10:
            continue

        device = parts[2]

        if not is_physical_block_device(device):
            continue

        if not parts[5].isdigit() or not parts[9].isdigit():
            continue

        sectors_read += int(parts[5])
        sectors_written += int(parts[9])
        device_count += 1

    if device_count == 0:
        return unavailable_io("readBytesPerSec", "writeBytesPerSec")

    # Linux diskstats always reports sectors in 512-byte units.
    read_rate, write_rate = rates_from_counters(
        "disk",
        sectors_read * 512,
        sectors_written * 512,
    )

    return {
        "readBytesPerSec": read_rate,
        "writeBytesPerSec": write_rate,
        "source": "proc_diskstats",
    }


def is_physical_block_device(name: str) -> bool:
    excluded_prefixes = ("loop", "ram", "fd", "sr", "dm-", "md")

    return not name.startswith(excluded_prefixes) and (SYS_BLOCK_ROOT / name).exists()


def rates_from_counters(key: str, first: int, second: int) -> tuple[float, float]:
    now = time.monotonic()
    previous = IO_PREVIOUS.get(key)
    IO_PREVIOUS[key] = (now, first, second)

    if previous is None:
        return 0.0, 0.0

    previous_time, previous_first, previous_second = previous
    elapsed = now - previous_time

    if elapsed <= 0:
        return 0.0, 0.0

    first_delta = max(first - previous_first, 0)
    second_delta = max(second - previous_second, 0)

    return round(first_delta / elapsed, 1), round(second_delta / elapsed, 1)


def unavailable_io(first_key: str, second_key: str) -> dict[str, Any]:
    return {
        first_key: None,
        second_key: None,
        "source": "unavailable",
    }


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
            "processMemory": "mock",
            "systemTemp": "mock",
            "systemCpu": "mock",
            "systemNetwork": "mock",
            "systemDisk": "mock",
            "gpu": "mock",
            "inference": "mock",
        },
        "system": {
            "memory": {
                "usedGb": round(14 + math.sin(t * 0.31) * 1.4, 2),
                "totalGb": PROJECTED_TOTAL_GB,
                "residentGb": round(84 + math.sin(t * 0.19 + 0.8) * 3.2, 2),
                "fileGb": round(82 + math.sin(t * 0.19 + 0.8) * 3.0, 2),
                "anonGb": 0.55,
                "cudaGb": round(8 + math.sin(t * 0.22 + 1.5) * 0.4, 2),
                "processCount": 1,
                "processes": [
                    {
                        "pid": 2823992,
                        "name": "ds4-server",
                        "residentGb": round(84 + math.sin(t * 0.19 + 0.8) * 3.2, 2),
                        "fileGb": round(82 + math.sin(t * 0.19 + 0.8) * 3.0, 2),
                        "anonGb": 0.55,
                        "cudaGb": round(8 + math.sin(t * 0.22 + 1.5) * 0.4, 2),
                    },
                ],
            },
            "temp": {
                "valueC": round(75 + math.sin(t * 0.42 + 1.1) * 2.1, 1),
                "maxC": 104.8,
            },
            "cpu": {
                "avgPct": average_cpu(cpu_cores),
                "cores": cpu_cores,
            },
            "network": {
                "rxBytesPerSec": round(max(84_000_000 + math.sin(t * 0.47 + 0.6) * 25_000_000, 0)),
                "txBytesPerSec": round(max(2_100_000 + math.sin(t * 0.61 + 1.3) * 900_000, 0)),
            },
            "disk": {
                "readBytesPerSec": round(max(1_200_000_000 + math.sin(t * 0.44 + 2.2) * 360_000_000, 0)),
                "writeBytesPerSec": round(max(640_000_000 + math.sin(t * 0.53 + 0.9) * 220_000_000, 0)),
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
        "inference": {
            "available": True,
            "runtime": "sglang",
            "model": "qwen3.8-27b",
            "contextTokens": SGLANG_CONTEXT_TOKENS,
            "genThroughput": round(max(42.8 + math.sin(t * 0.41 + 0.2) * 7.5, 0), 1),
            "numUsedTokens": round(max(67_800 + math.sin(t * 0.23 + 1.2) * 18_000, 0)),
            "maxTotalNumTokens": 455_439,
            "specAcceptRate": round(clamp_pct(61 + math.sin(t * 0.37 + 0.5) * 12) / 100, 2),
            "specAcceptLength": round(max(3.8 + math.sin(t * 0.32 + 0.1) * 0.8, 0), 1),
            "prefixHitRate": round(clamp_pct(84 + math.sin(t * 0.29 + 0.9) * 9) / 100, 2),
            "cacheHitRate": round(clamp_pct(84 + math.sin(t * 0.29 + 0.9) * 9) / 100, 2),
            "prefillCacheTokens": 62_208,
            "prefillComputeTokens": 23_008,
            "numRunningReqs": 1,
            "numQueueReqs": 0,
            "source": "mock",
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
