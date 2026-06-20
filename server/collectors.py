"""Telemetry collectors for the Spark dashboard.

The collectors intentionally use the standard library only. On DGX Spark the
server should read what the OS already exposes and avoid installing packages
just to get the first live dashboard running.
"""

from __future__ import annotations

import csv
import math
import shutil
import subprocess
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


def collect_snapshot(use_mock: bool = False) -> dict[str, Any]:
    if use_mock or should_use_mock_snapshot():
        return mock_snapshot()

    memory = read_system_memory()
    thermal = read_system_thermal()
    cpu = read_cpu_utilization()
    gpu = read_nvidia_smi()

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


def should_use_mock_snapshot() -> bool:
    return not MEMINFO_PATH.exists() and shutil.which("nvidia-smi") is None


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


def read_nvidia_smi() -> dict[str, Any]:
    if shutil.which("nvidia-smi") is None:
        return empty_gpu("unavailable")

    fields = [
        "utilization.gpu",
        "temperature.gpu",
        "power.draw",
        "power.limit",
    ]
    command = [
        "nvidia-smi",
        "--query-gpu=" + ",".join(fields),
        "--format=csv,noheader,nounits",
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return empty_gpu("nvidia_smi_error")

    rows = [row for row in csv.reader(result.stdout.splitlines()) if row]

    if not rows:
        return empty_gpu("nvidia_smi_empty")

    first_gpu = rows[0]

    return {
        "utilizationPct": parse_number(first_gpu, 0),
        "tempC": parse_number(first_gpu, 1),
        "powerW": parse_number(first_gpu, 2),
        "powerLimitW": parse_number(first_gpu, 3),
        "source": "nvidia_smi",
    }


def empty_gpu(source: str) -> dict[str, Any]:
    return {
        "utilizationPct": None,
        "tempC": None,
        "powerW": None,
        "powerLimitW": None,
        "source": source,
    }


def parse_number(row: list[str], index: int) -> float | None:
    if index >= len(row):
        return None

    raw = row[index].strip().replace("[", "").replace("]", "")

    if raw in {"", "N/A", "Not Supported"}:
        return None

    try:
        value = float(raw)
    except ValueError:
        return None

    return value if math.isfinite(value) else None


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


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
