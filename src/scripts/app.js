(function () {
  var data = window.SparkMockData;
  var render = window.SparkRender;
  var SERIES_LIMIT = 10;
  var liveSeries = {};
  var INFERENCE_IDLE_DIM_MS = 2000;
  var INFERENCE_IDLE_MORE_DIM_MS = 10000;
  var INFERENCE_IDLE_CLEAR_MS = 20000;
  var PREFILL_DELTA_MIN_TOKENS = 128;
  var inferenceDisplay = {
    lastActiveAt: 0,
    activeStartedAt: 0,
    lastDurationMs: null,
    lastValues: null,
    prefixCacheTokens: 0,
    prefixComputeTokens: 0,
    prefixHitRate: null,
  };

  var elements = {
    updatedAt: document.querySelector("[data-updated-at]"),
    values: {
      systemTemp: document.querySelector('[data-value="system.temp"]'),
      systemTempLimit: document.querySelector('[data-limit="system.temp"]'),
      systemCpuAvg: document.querySelector('[data-value="system.cpu.avg"]'),
      gpuTemp: document.querySelector('[data-value="gpu.temp"]'),
      gpuTempLimit: document.querySelector('[data-limit="gpu.temp"]'),
      gpuPower: document.querySelector('[data-value="gpu.power"]'),
    },
    cpuCores: document.querySelectorAll("[data-cpu-core]"),
    sparklines: {
      systemTemp: document.querySelectorAll('[data-sparkline-band="system.temp"]'),
      gpuTemp: document.querySelectorAll('[data-sparkline-band="gpu.temp"]'),
      gpuPower: document.querySelector('[data-sparkline="gpu.power"]'),
    },
    gauges: {
      systemMemory: document.querySelector('[data-gauge="system.memory"]'),
      gpuUtilization: document.querySelector('[data-gauge="gpu.utilization"]'),
    },
    histories: {
      gpuUtilization: document.querySelector('[data-history="gpu.utilization"]'),
    },
    ioHistories: {
      network: document.querySelector('[data-io-history="system.network"]'),
      disk: document.querySelector('[data-io-history="system.disk"]'),
    },
    inference: {
      tiles: {
        throughput: document.querySelector('[data-inference-tile="throughput"]'),
        context: document.querySelector('[data-inference-tile="context"]'),
        draft: document.querySelector('[data-inference-tile="draft"]'),
        prefix: document.querySelector('[data-inference-tile="prefix"]'),
        requests: document.querySelector('[data-inference-tile="requests"]'),
        duration: document.querySelector('[data-inference-tile="duration"]'),
      },
      stats: {
        throughput: document.querySelector('[data-inference-stat="throughput"]'),
        context: document.querySelector('[data-inference-stat="context"]'),
        draft: document.querySelector('[data-inference-stat="draft"]'),
        prefix: document.querySelector('[data-inference-stat="prefix"]'),
        requests: document.querySelector('[data-inference-stat="requests"]'),
        duration: document.querySelector('[data-inference-stat="duration"]'),
      },
    },
  };
  var telemetryScales = {
    temp: {
      min: 45,
      max: 105,
      thresholds: {
        warn: 80,
        danger: 95,
      },
    },
    power: {
      min: 0,
      max: 140,
    },
  };

  function updateStatus(source) {
    var now = new Date();
    var time = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });

    if (elements.updatedAt) {
      elements.updatedAt.textContent = source + " - " + time;
    }
  }

  function isNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function asNumber(value) {
    var number = Number(value);

    return Number.isFinite(number) ? number : null;
  }

  function numberOrFallback(value, fallback) {
    return isNumber(value) ? value : fallback;
  }

  function metricText(value, unit) {
    return isNumber(value) ? Math.round(value) + " " + unit : "N/A";
  }

  function percentText(value) {
    return isNumber(value) ? Math.round(value) + "%" : "N/A";
  }

  function setText(element, text) {
    if (element) {
      element.textContent = text;
    }
  }

  function formatTokenCount(value) {
    if (!isNumber(value)) {
      return "--";
    }

    if (value >= 1000) {
      var scaled = value / 1000;
      var digits = scaled >= 100 ? 0 : 1;

      return render.formatNumber(scaled, digits) + "k";
    }

    return String(Math.round(value));
  }

  function formatContextLimit(value) {
    if (!isNumber(value)) {
      return "--";
    }

    return formatTokenCount(value);
  }

  function formatTokPerSecond(value) {
    if (!isNumber(value)) {
      return "--";
    }

    var digits = value >= 100 ? 0 : 1;

    return render.formatNumber(Math.max(value, 0), digits) + " tok/s";
  }

  function formatSpecRate(value) {
    return isNumber(value) ? render.formatNumber(value, 2) : "--";
  }

  function formatSpecLength(value) {
    return isNumber(value) ? render.formatNumber(value, 1) : "--";
  }

  function formatRatioPercent(value) {
    if (!isNumber(value)) {
      return "--";
    }

    var percent = value <= 1 ? value * 100 : value;

    return Math.round(clamp(percent, 0, 100)) + "%";
  }

  function formatDuration(valueMs) {
    if (!isNumber(valueMs)) {
      return "--";
    }

    var totalSeconds = Math.max(Math.floor(valueMs / 1000), 0);

    if (totalSeconds < 60) {
      return totalSeconds + "s";
    }

    var hours = Math.floor(totalSeconds / 3600);
    var minutes = Math.floor((totalSeconds % 3600) / 60);
    var seconds = totalSeconds % 60;

    if (hours > 0) {
      return hours + ":" + String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");
    }

    return minutes + ":" + String(seconds).padStart(2, "0");
  }

  function requestCount(value) {
    return isNumber(value) ? Math.round(Math.max(value, 0)) : null;
  }

  function normalizeCpuCores(cores) {
    var normalized = [];

    for (var index = 0; index < 20; index += 1) {
      var core = Array.isArray(cores) ? cores[index] : null;
      var value = typeof core === "number" ? asNumber(core) : asNumber(core && core.valuePct);
      var coreIndex = core && core.index != null ? core.index : index;

      normalized.push({
        index: coreIndex,
        valuePct: value,
      });
    }

    return normalized;
  }

  function averageCpu(cores) {
    var values = cores
      .map(function (core) {
        return core.valuePct;
      })
      .filter(isNumber);

    if (!values.length) {
      return null;
    }

    return values.reduce(function (sum, value) {
      return sum + value;
    }, 0) / values.length;
  }

  function pushLiveValue(key, value) {
    if (!isNumber(value)) {
      return [];
    }

    if (!liveSeries[key]) {
      liveSeries[key] = [];

      for (var index = 0; index < SERIES_LIMIT; index += 1) {
        liveSeries[key].push(value);
      }
    }

    liveSeries[key].push(value);

    while (liveSeries[key].length > SERIES_LIMIT) {
      liveSeries[key].shift();
    }

    return liveSeries[key].slice();
  }

  function normalizeSnapshot(snapshot) {
    var system = snapshot.system || {};
    var systemMemory = system.memory || {};
    var systemTemp = system.temp || {};
    var systemCpu = system.cpu || {};
    var systemNetwork = system.network || {};
    var systemDisk = system.disk || {};
    var gpu = snapshot.gpu || {};
    var gpuUtilization = gpu.utilization || {};
    var gpuTemp = gpu.temp || {};
    var gpuPower = gpu.power || {};
    var inference = snapshot.inference || {};
    var normalized = {
      system: {
        memory: {
          usedGb: asNumber(systemMemory.usedGb),
          totalGb: asNumber(systemMemory.totalGb),
          residentGb: asNumber(systemMemory.residentGb),
          fileGb: asNumber(systemMemory.fileGb),
          anonGb: asNumber(systemMemory.anonGb),
          cudaGb: asNumber(systemMemory.cudaGb),
          processCount: asNumber(systemMemory.processCount),
        },
        temp: {
          valueC: asNumber(systemTemp.valueC),
          maxC: asNumber(systemTemp.maxC),
        },
        cpu: {
          avgPct: asNumber(systemCpu.avgPct),
          cores: normalizeCpuCores(systemCpu.cores),
        },
        network: {
          rxBytesPerSec: asNumber(systemNetwork.rxBytesPerSec),
          txBytesPerSec: asNumber(systemNetwork.txBytesPerSec),
        },
        disk: {
          readBytesPerSec: asNumber(systemDisk.readBytesPerSec),
          writeBytesPerSec: asNumber(systemDisk.writeBytesPerSec),
        },
      },
      gpu: {
        utilization: {
          valuePct: asNumber(gpuUtilization.valuePct),
          maxPct: asNumber(gpuUtilization.maxPct) || 100,
        },
        temp: {
          valueC: asNumber(gpuTemp.valueC),
          maxC: asNumber(gpuTemp.maxC),
        },
        power: {
          valueW: asNumber(gpuPower.valueW),
          maxW: asNumber(gpuPower.maxW),
        },
      },
      inference: {
        available: inference.available === true,
        runtime: inference.runtime || null,
        model: inference.model || null,
        contextTokens: asNumber(inference.contextTokens),
        genThroughput: asNumber(inference.genThroughput),
        numUsedTokens: asNumber(inference.numUsedTokens),
        maxTotalNumTokens: asNumber(inference.maxTotalNumTokens),
        specAcceptRate: asNumber(inference.specAcceptRate),
        specAcceptLength: asNumber(inference.specAcceptLength),
        prefixHitRate: asNumber(inference.prefixHitRate),
        cacheHitRate: asNumber(inference.cacheHitRate),
        prefillCacheTokens: asNumber(inference.prefillCacheTokens),
        prefillComputeTokens: asNumber(inference.prefillComputeTokens),
        prefillCacheDeltaTokens: asNumber(inference.prefillCacheDeltaTokens),
        prefillComputeDeltaTokens: asNumber(inference.prefillComputeDeltaTokens),
        numRunningReqs: asNumber(inference.numRunningReqs),
        numQueueReqs: asNumber(inference.numQueueReqs),
      },
    };

    normalized.system.memory.series = pushLiveValue(
      "system.memory",
      normalized.system.memory.usedGb,
    );
    normalized.system.temp.series = pushLiveValue("system.temp", normalized.system.temp.valueC);
    normalized.system.cpu.avgPct = numberOrFallback(
      normalized.system.cpu.avgPct,
      averageCpu(normalized.system.cpu.cores),
    );
    normalized.system.network.rxSeries = pushLiveValue(
      "system.network.rx",
      normalized.system.network.rxBytesPerSec,
    );
    normalized.system.network.txSeries = pushLiveValue(
      "system.network.tx",
      normalized.system.network.txBytesPerSec,
    );
    normalized.system.disk.readSeries = pushLiveValue(
      "system.disk.read",
      normalized.system.disk.readBytesPerSec,
    );
    normalized.system.disk.writeSeries = pushLiveValue(
      "system.disk.write",
      normalized.system.disk.writeBytesPerSec,
    );
    normalized.gpu.utilization.series = pushLiveValue(
      "gpu.utilization",
      normalized.gpu.utilization.valuePct,
    );
    normalized.gpu.temp.series = pushLiveValue("gpu.temp", normalized.gpu.temp.valueC);
    normalized.gpu.power.series = pushLiveValue("gpu.power", normalized.gpu.power.valueW);

    return normalized;
  }

  function renderCpuCores(cpu) {
    var cores = cpu && Array.isArray(cpu.cores) ? cpu.cores : [];

    render.renderMetricValue(elements.values.systemCpuAvg, null, percentText(cpu && cpu.avgPct), "");

    Array.prototype.forEach.call(elements.cpuCores, function (element, index) {
      var core = cores[index] || {};
      var value = isNumber(core.valuePct) ? clamp(core.valuePct, 0, 100) : null;
      var fill = element.querySelector(".cpu-core-fill");
      var label = "CPU " + index + " " + percentText(value);

      if (fill) {
        fill.style.height = isNumber(value) ? value + "%" : "0%";
      }

      element.classList.toggle("is-warn", isNumber(value) && value >= 75 && value < 90);
      element.classList.toggle("is-danger", isNumber(value) && value >= 90);
      element.setAttribute("aria-label", label);
      element.setAttribute("title", label);
    });
  }

  function canFetchApi() {
    return window.location.protocol !== "file:" && typeof window.fetch === "function";
  }

  function fetchSnapshot() {
    if (!canFetchApi()) {
      return Promise.resolve({
        snapshot: data.getSnapshot(),
        source: "Mock data",
      });
    }

    return window
      .fetch("/api/snapshot", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("snapshot request failed");
        }

        return response.json();
      })
      .then(function (snapshot) {
        var source = snapshot.source && snapshot.source.mode === "mock" ? "Server mock" : "Live data";

        return {
          snapshot: normalizeSnapshot(snapshot),
          source: source,
        };
      })
      .catch(function () {
        return {
          snapshot: data.getSnapshot(),
          source: "Mock data",
        };
      });
  }

  function renderTelemetry(snapshot) {
    render.renderMetricValue(
      elements.values.systemTemp,
      null,
      metricText(snapshot.system.temp.valueC, "C"),
      "",
    );
    render.renderMetricValue(
      elements.values.gpuTemp,
      null,
      metricText(snapshot.gpu.temp.valueC, "C"),
      "",
    );
    render.renderMetricValue(
      elements.values.gpuPower,
      null,
      metricText(snapshot.gpu.power.valueW, "W"),
      "",
    );

    renderCpuCores(snapshot.system.cpu);
    render.renderSparkline(elements.sparklines.systemTemp, snapshot.system.temp.series, telemetryScales.temp);
    render.renderSparkline(elements.sparklines.gpuTemp, snapshot.gpu.temp.series, telemetryScales.temp);
    render.renderSparkline(elements.sparklines.gpuPower, snapshot.gpu.power.series, telemetryScales.power);
  }

  function renderSystemMemory(snapshot) {
    var memory = snapshot.system.memory;
    var hasMemory = isNumber(memory.usedGb) && isNumber(memory.totalGb) && memory.totalGb > 0;
    var hasResident = hasMemory && isNumber(memory.residentGb);
    var residentGb = hasResident ? Math.max(memory.residentGb, memory.usedGb) : memory.usedGb;
    var hiddenResidentGb = 0;

    if (hasResident) {
      hiddenResidentGb = isNumber(memory.fileGb)
        ? Math.min(memory.fileGb, residentGb)
        : Math.max(residentGb - memory.usedGb, 0);
    }

    var hasHiddenResident = hiddenResidentGb > 0.1;
    var unavailableGb = hasMemory ? memory.usedGb + hiddenResidentGb : 0;
    var pressurePercent = hasMemory ? (memory.usedGb / memory.totalGb) * 100 : 0;
    var residentPercent = hasMemory ? (residentGb / memory.totalGb) * 100 : pressurePercent;
    var usedText = hasMemory ? render.formatNumber(unavailableGb, 1) + " GB" : "N/A";
    var totalText = hasHiddenResident
      ? "(" + render.formatNumber(hiddenResidentGb, 1) + ") + " + render.formatNumber(memory.usedGb, 1)
      : hasMemory
        ? Math.round(memory.totalGb) + " GB total"
        : "Memory unavailable";
    var ariaLabel = hasMemory
      ? "Practical unavailable memory " +
        render.formatNumber(unavailableGb, 2) +
        " GB, memory pressure " +
        render.formatNumber(memory.usedGb, 2) +
        " GB"
      : "System memory unavailable";

    if (hasHiddenResident) {
      ariaLabel +=
        ", hidden resident file pages " +
        render.formatNumber(hiddenResidentGb, 2) +
        " GB of " +
        Math.round(memory.totalGb) +
        " GB";
    } else if (hasMemory) {
      ariaLabel += " of " + Math.round(memory.totalGb) + " GB";
    }

    render.renderMemoryGauge(
      elements.gauges.systemMemory,
      pressurePercent,
      residentPercent,
      usedText,
      totalText,
      ariaLabel,
    );
  }

  function formatThroughput(value) {
    if (!isNumber(value)) {
      return "N/A";
    }

    var units = ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"];
    var scaled = Math.max(value, 0);
    var unitIndex = 0;

    while (scaled >= 1000 && unitIndex < units.length - 1) {
      scaled /= 1000;
      unitIndex += 1;
    }

    var digits = scaled >= 100 || unitIndex === 0 ? 0 : scaled >= 10 ? 1 : 2;

    return render.formatNumber(scaled, digits) + " " + units[unitIndex];
  }

  function renderSystemIo(snapshot) {
    var network = snapshot.system.network || {};
    var disk = snapshot.system.disk || {};
    var rxText = formatThroughput(network.rxBytesPerSec);
    var txText = formatThroughput(network.txBytesPerSec);
    var readText = formatThroughput(disk.readBytesPerSec);
    var writeText = formatThroughput(disk.writeBytesPerSec);

    render.renderDualHistory(
      elements.ioHistories.network,
      network.rxSeries,
      network.txSeries,
      rxText,
      txText,
      "Network receive " + rxText + " and transmit " + txText + " history for the last minute",
    );
    render.renderDualHistory(
      elements.ioHistories.disk,
      disk.readSeries,
      disk.writeSeries,
      readText,
      writeText,
      "SSD read " + readText + " and write " + writeText + " history for the last minute",
    );
  }

  function renderGpuUtilization(snapshot) {
    var utilization = snapshot.gpu.utilization;
    var value = isNumber(utilization.valuePct) ? utilization.valuePct : 0;
    var hasUtilization = isNumber(utilization.valuePct);

    render.renderGauge(
      elements.gauges.gpuUtilization,
      value,
      percentText(utilization.valuePct),
      hasUtilization ? "GPU active" : "GPU unavailable",
      hasUtilization ? "GPU utilization " + Math.round(value) + " percent" : "GPU utilization unavailable",
    );
    render.renderHistory(
      elements.histories.gpuUtilization,
      utilization.series,
      utilization.maxPct,
      Math.round(utilization.maxPct) + "%",
      "GPU utilization history for the last minute",
    );
  }

  function prefillDeltaTotal(inference) {
    var cacheDelta = isNumber(inference.prefillCacheDeltaTokens)
      ? Math.max(inference.prefillCacheDeltaTokens, 0)
      : 0;
    var computeDelta = isNumber(inference.prefillComputeDeltaTokens)
      ? Math.max(inference.prefillComputeDeltaTokens, 0)
      : 0;

    return cacheDelta + computeDelta;
  }

  function resetInferenceDisplay() {
    inferenceDisplay.lastActiveAt = 0;
    inferenceDisplay.activeStartedAt = 0;
    inferenceDisplay.lastDurationMs = null;
    inferenceDisplay.lastValues = null;
    inferenceDisplay.prefixCacheTokens = 0;
    inferenceDisplay.prefixComputeTokens = 0;
    inferenceDisplay.prefixHitRate = null;
  }

  function clearInferenceActivePeriod() {
    inferenceDisplay.lastActiveAt = 0;
    inferenceDisplay.activeStartedAt = 0;
    inferenceDisplay.lastValues = null;
    inferenceDisplay.prefixCacheTokens = 0;
    inferenceDisplay.prefixComputeTokens = 0;
    inferenceDisplay.prefixHitRate = null;
  }

  function updatePrefixHitAggregate(inference) {
    var cacheDelta = isNumber(inference.prefillCacheDeltaTokens)
      ? Math.max(inference.prefillCacheDeltaTokens, 0)
      : 0;
    var computeDelta = isNumber(inference.prefillComputeDeltaTokens)
      ? Math.max(inference.prefillComputeDeltaTokens, 0)
      : 0;
    var totalDelta = cacheDelta + computeDelta;

    if (totalDelta >= PREFILL_DELTA_MIN_TOKENS) {
      inferenceDisplay.prefixCacheTokens += cacheDelta;
      inferenceDisplay.prefixComputeTokens += computeDelta;

      var total = inferenceDisplay.prefixCacheTokens + inferenceDisplay.prefixComputeTokens;
      inferenceDisplay.prefixHitRate = total > 0 ? inferenceDisplay.prefixCacheTokens / total : null;
    } else if (!isNumber(inferenceDisplay.prefixHitRate) && isNumber(inference.prefixHitRate)) {
      inferenceDisplay.prefixHitRate = inference.prefixHitRate;
    }

    return inferenceDisplay.prefixHitRate;
  }

  function buildInferenceValues(inference, prefixHitRate, durationMs) {
    var running = requestCount(inference.numRunningReqs) || 0;
    var queue = requestCount(inference.numQueueReqs) || 0;
    var useGlobalKv = running > 1 || queue > 0;
    var contextDenominator = useGlobalKv
      ? inference.maxTotalNumTokens
      : inference.contextTokens;
    var contextLimitText = useGlobalKv
      ? formatTokenCount(contextDenominator)
      : formatContextLimit(contextDenominator);

    return {
      throughput: formatTokPerSecond(inference.genThroughput),
      context: isNumber(inference.numUsedTokens) && isNumber(contextDenominator)
        ? formatTokenCount(inference.numUsedTokens) + " / " + contextLimitText
        : "--",
      draft: isNumber(inference.specAcceptRate) && isNumber(inference.specAcceptLength)
        ? formatSpecRate(inference.specAcceptRate) + " / " + formatSpecLength(inference.specAcceptLength)
        : "--",
      prefix: isNumber(prefixHitRate)
        ? formatRatioPercent(prefixHitRate)
        : inferenceDisplay.lastValues && inferenceDisplay.lastValues.prefix
          ? inferenceDisplay.lastValues.prefix
          : "--",
      duration: formatDuration(durationMs),
    };
  }

  function inferenceIsActive(inference) {
    var running = requestCount(inference.numRunningReqs) || 0;

    return (
      running > 0 ||
      (isNumber(inference.genThroughput) && inference.genThroughput > 0) ||
      prefillDeltaTotal(inference) >= PREFILL_DELTA_MIN_TOKENS
    );
  }

  function setInferenceState(tile, state) {
    if (!tile) {
      return;
    }

    tile.classList.toggle("is-dim", state === "dim");
    tile.classList.toggle("is-more-dim", state === "more-dim");
    tile.classList.toggle("is-offline", state === "offline");
  }

  function renderInference(snapshot) {
    var inference = snapshot.inference || {};
    var statElements = elements.inference.stats;
    var tiles = elements.inference.tiles;
    var performanceKeys = ["throughput", "context", "draft", "prefix"];
    var running = requestCount(inference.numRunningReqs);

    if (!inference.available) {
      resetInferenceDisplay();
      performanceKeys.concat(["requests", "duration"]).forEach(function (key) {
        setText(statElements[key], "--");
        setInferenceState(tiles[key], "offline");
      });
      return;
    }

    var now = Date.now();
    var active = inferenceIsActive(inference);
    var values = null;
    var state = "live";

    if (active) {
      if (!inferenceDisplay.activeStartedAt) {
        inferenceDisplay.activeStartedAt = now;
      }

      inferenceDisplay.lastDurationMs = now - inferenceDisplay.activeStartedAt;
      values = buildInferenceValues(
        inference,
        updatePrefixHitAggregate(inference),
        inferenceDisplay.lastDurationMs,
      );
      inferenceDisplay.lastValues = values;
      inferenceDisplay.lastActiveAt = now;
    } else if (inferenceDisplay.lastValues && inferenceDisplay.lastActiveAt) {
      var idleMs = now - inferenceDisplay.lastActiveAt;

      if (idleMs <= INFERENCE_IDLE_DIM_MS) {
        values = inferenceDisplay.lastValues;
      } else if (idleMs <= INFERENCE_IDLE_MORE_DIM_MS) {
        values = inferenceDisplay.lastValues;
        state = "dim";
      } else if (idleMs <= INFERENCE_IDLE_CLEAR_MS) {
        values = inferenceDisplay.lastValues;
        state = "more-dim";
      } else {
        clearInferenceActivePeriod();
      }
    }

    var displayState = values ? state : "offline";

    performanceKeys.forEach(function (key) {
      setText(statElements[key], values ? values[key] : "--");
      setInferenceState(tiles[key], displayState);
    });

    setText(statElements.requests, running == null ? "--" : String(running));
    setInferenceState(tiles.requests, running == null ? "offline" : displayState);
    setText(statElements.duration, formatDuration(inferenceDisplay.lastDurationMs || 0));
    setInferenceState(tiles.duration, displayState);
  }

  function renderSnapshot(snapshot) {
    renderTelemetry(snapshot);
    renderSystemMemory(snapshot);
    renderSystemIo(snapshot);
    renderGpuUtilization(snapshot);
    renderInference(snapshot);
  }

  function tick() {
    fetchSnapshot().then(function (result) {
      renderSnapshot(result.snapshot);
      updateStatus(result.source);
    });
  }

  if (!data || !render) {
    return;
  }

  tick();
  window.setInterval(tick, 1000);
})();
