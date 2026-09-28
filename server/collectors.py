"""Telemetry collectors for the Spark dashboard.

The collectors intentionally use the standard library only. On DGX Spark the
server should read what the OS already exposes and avoid installing packages
just to get the first live dashboard running.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import re
import subprocess
import threading
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
INFERENCE_BASE_URL = os.environ.get("INFERENCE_BASE_URL", "http://localhost:8000").rstrip("/")
SGLANG_CONTEXT_TOKENS = 262_144
INFERENCE_LOCK = threading.Lock()
INFERENCE_CACHE: tuple[float, dict[str, Any]] | None = None
INFERENCE_IDENTITY: str | None = None
LLAMACPP_SLOTS_PREVIOUS: tuple[float, dict[int, tuple[int | None, float, bool]]] | None = None
PROMETHEUS_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{.*\})?\s+(\S+)(?:\s+.*)?$')
CPU_PREVIOUS: dict[int, tuple[int, int]] = {}
IO_PREVIOUS: dict[str, tuple[float, int, int]] = {}
PROCESS_MEMORY_CACHE_TTL_SECONDS = 5.0
PROCESS_MEMORY_CACHE: tuple[float, dict[str, Any]] | None = None
PREFILL_COUNTERS_PREVIOUS: tuple[float, float] | None = None
DECODE_COUNTER_PREVIOUS: float | None = None
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
    inference = read_inference_metrics()

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


def read_inference_endpoint(path: str, *, parse_json: bool = False) -> tuple[Any, int | None]:
    try:
        with urllib.request.urlopen(INFERENCE_BASE_URL + path, timeout=0.8) as response:
            body = response.read(1_000_000).decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        error.close()
        return None, error.code
    except (OSError, urllib.error.URLError, TimeoutError):
        return None, None

    if parse_json:
        try:
            return json.loads(body), status
        except ValueError:
            return None, status

    return body, status


def read_inference_metrics(*, force: bool = False) -> dict[str, Any]:
    global INFERENCE_CACHE, INFERENCE_IDENTITY, PREFILL_COUNTERS_PREVIOUS, DECODE_COUNTER_PREVIOUS
    global LLAMACPP_SLOTS_PREVIOUS

    # Snapshot requests can overlap or come from several browser tabs. Share a
    # sample so concurrent scrapes do not consume each other's counter deltas.
    with INFERENCE_LOCK:
        if not force and INFERENCE_CACHE and time.monotonic() - INFERENCE_CACHE[0] < 0.5:
            return INFERENCE_CACHE[1]

        body, status = read_inference_endpoint("/metrics")
        body = body if isinstance(body, str) else ""
        names = {
            match.group(1)
            for line in body.splitlines()
            if (match := PROMETHEUS_SAMPLE.match(line.strip()))
        }
        runtime = None
        if any(name.startswith("sglang:") for name in names):
            runtime = "sglang"
        elif any(name.startswith("llamacpp:") for name in names):
            runtime = "llamacpp"

        props: dict[str, Any] = {}
        model_info: dict[str, Any] = {}
        # Introspection also identifies llama.cpp when --metrics is disabled.
        # Do not probe further after a connection or authentication failure.
        if runtime == "llamacpp" or (runtime is None and status not in (None, 401, 403)):
            payload, _ = read_inference_endpoint("/props", parse_json=True)
            if isinstance(payload, dict) and "default_generation_settings" in payload and "total_slots" in payload:
                props = payload
                runtime = "llamacpp"
        if runtime is None and status not in (None, 401, 403):
            payload, _ = read_inference_endpoint("/get_model_info", parse_json=True)
            if isinstance(payload, dict) and "model_path" in payload and "is_generation" in payload:
                model_info = payload
                runtime = "sglang"

        model = (
            props.get("model_alias") or props.get("model_path")
            if runtime == "llamacpp"
            else parse_prometheus_label(body, "model_name") or model_info.get("model_path")
        )
        identity = "|".join((INFERENCE_BASE_URL, runtime or "unknown", str(props.get("model_path") or model or "")))
        if identity != INFERENCE_IDENTITY or not names:
            PREFILL_COUNTERS_PREVIOUS = None
            DECODE_COUNTER_PREVIOUS = None
        if identity != INFERENCE_IDENTITY:
            LLAMACPP_SLOTS_PREVIOUS = None
        INFERENCE_IDENTITY = identity

        if runtime == "sglang":
            inference = parse_sglang_metrics(body)
            inference["model"] = model
            inference["supportsAbort"] = inference["available"]
        elif runtime == "llamacpp":
            slots, _ = read_inference_endpoint("/slots", parse_json=True)
            inference = parse_llamacpp_metrics(body, props, slots)
        else:
            inference = empty_inference("inference_metrics_unavailable")

        inference["identity"] = identity
        inference["metricsHttpStatus"] = status
        if not names:
            if status in (401, 403):
                inference["message"] = "Metrics require authentication"
            elif status == 503:
                inference["message"] = "Inference server is loading or unavailable (HTTP 503)"
            elif status is None:
                inference["message"] = "Inference server is unreachable"
            elif runtime == "llamacpp" and props.get("endpoint_metrics") is False:
                inference["message"] = "Metrics disabled: start llama-server with --metrics"
            elif runtime == "sglang" and status in (404, 501):
                inference["message"] = "Metrics unavailable: start SGLang with --enable-metrics"
            else:
                inference["message"] = "No supported inference metrics returned by /metrics"
        elif runtime is None:
            inference["message"] = "Unrecognized metrics format (expected SGLang or llama.cpp)"

        INFERENCE_CACHE = (time.monotonic(), inference)
        return inference


def parse_sglang_metrics(body: str) -> dict[str, Any]:
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
    prefix_counters = prefix_stats_from_counters(
        realtime_tokens.get("prefill_cache"),
        realtime_tokens.get("prefill_compute"),
    )
    decode_delta_tokens = decode_delta_from_counter(realtime_tokens.get("decode"))

    return {
        **empty_inference("sglang_metrics", "sglang"),
        "available": any(value is not None for value in values.values()) or bool(realtime_tokens),
        "runtime": "sglang",
        "model": parse_prometheus_label(body, "model_name"),
        "contextTokens": SGLANG_CONTEXT_TOKENS,
        "genThroughput": values.get("genThroughput"),
        "numUsedTokens": values.get("numUsedTokens"),
        "maxTotalNumTokens": values.get("maxTotalNumTokens"),
        "specAcceptRate": values.get("specAcceptRate"),
        "specAcceptLength": values.get("specAcceptLength"),
        "prefixHitRate": prefix_counters["prefixHitRate"],
        "cacheHitRate": values.get("cacheHitRate"),
        "prefillCacheTokens": realtime_tokens.get("prefill_cache"),
        "prefillComputeTokens": realtime_tokens.get("prefill_compute"),
        "prefillCacheDeltaTokens": prefix_counters["prefillCacheDeltaTokens"],
        "prefillComputeDeltaTokens": prefix_counters["prefillComputeDeltaTokens"],
        "decodeTokens": realtime_tokens.get("decode"),
        "decodeDeltaTokens": decode_delta_tokens,
        "numRunningReqs": values.get("numRunningReqs"),
        "numQueueReqs": values.get("numQueueReqs"),
        "source": "sglang_metrics",
    }


def parse_llamacpp_metrics(body: str, props: dict[str, Any], slots: Any) -> dict[str, Any]:
    values = parse_prometheus_metrics(body, {
        "llamacpp:predicted_tokens_seconds": "genThroughput",
        "llamacpp:prompt_tokens_seconds": "prefillThroughput",
        "llamacpp:requests_processing": "numRunningReqs",
        "llamacpp:requests_deferred": "numQueueReqs",
        "llamacpp:tokens_predicted_total": "decodeTokens",
        "llamacpp:prompt_tokens_total": "prefillComputeTokens",
        "llamacpp:prompt_tokens_cached_total": "prefillCacheTokens",
        "llamacpp:spec_decode_num_draft_tokens_total": "draftTokens",
        "llamacpp:spec_decode_num_accepted_tokens_total": "acceptedTokens",
        "llamacpp:spec_decode_num_drafts_total": "draftSteps",
    })
    inference = {
        **empty_inference("llamacpp_metrics", "llamacpp"),
        **{key: value for key, value in values.items() if key not in {"draftTokens", "acceptedTokens", "draftSteps"}},
        "model": props.get("model_alias") or props.get("model_path"),
        "contextTokens": None,
        "decodeDeltaTokens": decode_delta_from_counter(values["decodeTokens"]),
        **prefix_stats_from_counters(values["prefillCacheTokens"], values["prefillComputeTokens"]),
        "serverGenThroughput": values["genThroughput"],
        "throughputSource": "metrics",
        "prefixHitRateSource": "metrics",
    }
    settings = props.get("default_generation_settings")
    if isinstance(settings, dict):
        inference["contextTokens"] = finite_nonnegative(settings.get("n_ctx"))

    notes = inference["metricNotes"]
    notes["throughput"] = "Fallback: llama.cpp server average; some builds update metrics only between requests. Live speed requires /slots with next_token.n_decoded"
    notes["context"] = "Context occupancy requires /slots with token counts; n_tokens_max is a historical maximum, not current usage"
    notes["prefix"] = "Prefix reuse requires prompt_tokens_cached_total and prompt_tokens_total; older llama.cpp builds may not export both"
    notes["draft"] = "Draft acceptance and mean length (including the target token), cumulative since server start; unavailable before draft tokens exist"
    notes["duration"] = "Observed activity time; abort from the dashboard is supported only for SGLang"

    activity = llamacpp_slot_activity(slots)
    if activity is not None:
        inference.update(activity)
        inference["throughputSource"] = "slots"
        notes["throughput"] = "Live generated tokens/s from changes in /slots next_token.n_decoded; needs two samples, excludes prompt processing"

    if isinstance(slots, list) and slots and all(isinstance(slot, dict) for slot in slots):
        active_slots = [slot for slot in slots if slot.get("is_processing") is True]
        if inference["numRunningReqs"] is None and all(isinstance(slot.get("is_processing"), bool) for slot in slots):
            inference["numRunningReqs"] = len(active_slots)
        limits = [finite_nonnegative(slot.get("n_ctx")) for slot in slots]
        if all(limit is not None and limit > 0 for limit in limits):
            inference["maxTotalNumTokens"] = sum(limits)
            if len(active_slots) == 1:
                inference["contextTokens"] = finite_nonnegative(active_slots[0].get("n_ctx"))
            elif len(set(limits)) == 1:
                inference["contextTokens"] = limits[0]
        # Current llama.cpp counts both prompt and generated tokens in this
        # field. Adding next_token.n_decoded would count generation twice.
        used = [finite_nonnegative(slot.get("n_prompt_tokens")) for slot in active_slots or slots]
        if all(value is not None for value in used):
            inference["numUsedTokens"] = sum(used)
            notes["context"] = "Current tokens in active llama.cpp slots / context capacity"
        if all(slot.get("speculative") is False for slot in slots):
            notes["draft"] = "Speculative decoding is disabled"

        # These counters describe the current prompt even when the dashboard
        # attaches mid-request and Prometheus has not committed its counters yet.
        cached = [finite_nonnegative(slot.get("n_prompt_tokens_cache")) for slot in active_slots]
        computed = [finite_nonnegative(slot.get("n_prompt_tokens_processed")) for slot in active_slots]
        if active_slots and all(value is not None for value in cached + computed):
            total = sum(cached) + sum(computed)
            inference["prefixHitRate"] = sum(cached) / total if total > 0 else None
            inference["prefixHitRateSource"] = "slots"
            notes["prefix"] = "Current active slots: cached / (cached + processed) prompt tokens"

    drafted, accepted, steps = (values[key] for key in ("draftTokens", "acceptedTokens", "draftSteps"))
    if drafted is not None and drafted > 0 and accepted is not None:
        inference["specAcceptRate"] = accepted / drafted
        if steps is not None and steps > 0:
            inference["specAcceptLength"] = 1 + accepted / steps
    if values["prefillCacheTokens"] is not None and inference["prefixHitRateSource"] != "slots":
        notes["prefix"] = "Cached / (cached + computed) prompt tokens in the observed activity period"

    inference["available"] = any(inference[key] is not None for key in (
        "genThroughput", "numRunningReqs", "numQueueReqs", "numUsedTokens", "decodeTokens",
    ))
    return inference


def llamacpp_slot_activity(slots: Any) -> dict[str, float | None] | None:
    global LLAMACPP_SLOTS_PREVIOUS

    samples: dict[int, tuple[int | None, float, bool]] = {}
    if not isinstance(slots, list) or not slots:
        LLAMACPP_SLOTS_PREVIOUS = None
        return None

    for slot in slots:
        if not isinstance(slot, dict):
            LLAMACPP_SLOTS_PREVIOUS = None
            return None
        slot_id, task_id, processing = slot.get("id"), slot.get("id_task"), slot.get("is_processing")
        next_token = slot.get("next_token")
        # Older llama.cpp versions return an object; this build returns [object].
        if isinstance(next_token, list):
            next_token = next_token[0] if len(next_token) == 1 else None
        decoded = finite_nonnegative(next_token.get("n_decoded")) if isinstance(next_token, dict) else None
        if (
            type(slot_id) is not int or type(processing) is not bool
            or slot_id in samples
            or (processing and (type(task_id) is not int or decoded is None))
        ):
            LLAMACPP_SLOTS_PREVIOUS = None
            return None
        samples[slot_id] = (task_id, decoded or 0.0, processing)

    now = time.monotonic()
    previous = LLAMACPP_SLOTS_PREVIOUS
    LLAMACPP_SLOTS_PREVIOUS = (now, samples)
    waiting = {"genThroughput": None, "decodeDeltaTokens": None}
    if previous is None:
        if not any(sample[2] for sample in samples.values()):
            return {"genThroughput": 0.0, "decodeDeltaTokens": 0.0}
        return waiting

    elapsed = now - previous[0]
    if elapsed <= 0 or elapsed > 5 or samples.keys() != previous[1].keys():
        return waiting

    delta = 0.0
    for slot_id, (task_id, decoded, processing) in samples.items():
        previous_task, previous_decoded, _ = previous[1][slot_id]
        if task_id != previous_task:
            # A slot was reused since the previous sample. Its new task's
            # generated tokens belong to this interval, not the preceding task.
            delta += decoded if processing else 0.0
        elif decoded >= previous_decoded:
            delta += decoded - previous_decoded
        elif processing:
            # Counter reset/restart: establish a fresh baseline, never a spike.
            return waiting
        # Idle slots can clear their final counters on release. Do not subtract
        # the old count or use the now-committed Prometheus total a second time.

    return {"genThroughput": delta / elapsed, "decodeDeltaTokens": delta}


def finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def empty_inference(source: str, runtime: str | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "runtime": runtime,
        "model": None,
        "contextTokens": SGLANG_CONTEXT_TOKENS if runtime == "sglang" else None,
        "supportsAbort": False,
        "message": None,
        "metricNotes": {},
        "genThroughput": None,
        "numUsedTokens": None,
        "maxTotalNumTokens": None,
        "specAcceptRate": None,
        "specAcceptLength": None,
        "prefixHitRate": None,
        "cacheHitRate": None,
        "prefillCacheTokens": None,
        "prefillComputeTokens": None,
        "prefillCacheDeltaTokens": None,
        "prefillComputeDeltaTokens": None,
        "decodeTokens": None,
        "decodeDeltaTokens": None,
        "numRunningReqs": None,
        "numQueueReqs": None,
        "source": source,
    }


def parse_prometheus_metrics(body: str, names: dict[str, str]) -> dict[str, float | None]:
    values: dict[str, float | None] = {target: None for target in names.values()}

    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue

        sample = PROMETHEUS_SAMPLE.match(line.strip())
        if sample is None:
            continue
        target = names.get(sample.group(1))

        if target is None:
            continue

        value = parse_float(sample.group(2))

        if value is not None and value >= 0:
            values[target] = value

    return values


def parse_sglang_realtime_tokens(body: str) -> dict[str, float]:
    counters: dict[str, float] = {}

    for line in body.splitlines():
        sample = PROMETHEUS_SAMPLE.match(line.strip())
        if sample is None or sample.group(1) != "sglang:realtime_tokens_total":
            continue

        mode = parse_metric_label(line, "mode")

        if mode not in {"prefill_cache", "prefill_compute", "decode"}:
            continue

        value = parse_float(sample.group(2))

        if value is not None and value >= 0:
            counters[mode] = value

    return counters


def prefix_stats_from_counters(
    prefill_cache: float | None,
    prefill_compute: float | None,
) -> dict[str, float | None]:
    global PREFILL_COUNTERS_PREVIOUS

    empty = {
        "prefixHitRate": None,
        "prefillCacheDeltaTokens": None,
        "prefillComputeDeltaTokens": None,
    }

    if prefill_cache is None or prefill_compute is None:
        PREFILL_COUNTERS_PREVIOUS = None
        return empty

    previous = PREFILL_COUNTERS_PREVIOUS
    PREFILL_COUNTERS_PREVIOUS = (prefill_cache, prefill_compute)
    total = prefill_cache + prefill_compute

    if previous is None:
        return {
            "prefixHitRate": prefill_cache / total if total > 0 else None,
            "prefillCacheDeltaTokens": 0.0,
            "prefillComputeDeltaTokens": 0.0,
        }

    previous_cache, previous_compute = previous
    cache_delta = prefill_cache - previous_cache
    compute_delta = prefill_compute - previous_compute

    if cache_delta < 0 or compute_delta < 0:
        return {
            "prefixHitRate": prefill_cache / total if total > 0 else None,
            "prefillCacheDeltaTokens": 0.0,
            "prefillComputeDeltaTokens": 0.0,
        }

    total_delta = cache_delta + compute_delta

    return {
        "prefixHitRate": cache_delta / total_delta if total_delta > 0 else None,
        "prefillCacheDeltaTokens": cache_delta,
        "prefillComputeDeltaTokens": compute_delta,
    }


def decode_delta_from_counter(decode_tokens: float | None) -> float | None:
    global DECODE_COUNTER_PREVIOUS

    if decode_tokens is None:
        DECODE_COUNTER_PREVIOUS = None
        return None

    previous = DECODE_COUNTER_PREVIOUS
    DECODE_COUNTER_PREVIOUS = decode_tokens

    if previous is None:
        return 0.0

    delta = decode_tokens - previous

    return delta if delta >= 0 else 0.0


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
    match = re.search(r'(?:\{|,)\s*' + re.escape(label_name) + r'="((?:\\.|[^"\\])*)"', line)
    if match is None:
        return None
    try:
        return json.loads('"' + match.group(1) + '"')
    except ValueError:
        return None


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
        number = float(value)
        return number if math.isfinite(number) else None
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
            "supportsAbort": True,
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
            "prefillCacheDeltaTokens": round(max(900 + math.sin(t * 0.4) * 260, 0)),
            "prefillComputeDeltaTokens": round(max(230 + math.sin(t * 0.3) * 90, 0)),
            "decodeTokens": 5_822,
            "decodeDeltaTokens": round(max(42.8 + math.sin(t * 0.41 + 0.2) * 7.5, 0)),
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
