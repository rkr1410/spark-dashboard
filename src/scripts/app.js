(function () {
  var data = window.SparkMockData;
  var render = window.SparkRender;
  var SERIES_LIMIT = 10;
  var liveSeries = {};

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
      systemMemory: document.querySelector('[data-history="system.memory"]'),
      gpuUtilization: document.querySelector('[data-history="gpu.utilization"]'),
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
    var gpu = snapshot.gpu || {};
    var gpuUtilization = gpu.utilization || {};
    var gpuTemp = gpu.temp || {};
    var gpuPower = gpu.power || {};
    var normalized = {
      system: {
        memory: {
          usedGb: asNumber(systemMemory.usedGb),
          totalGb: asNumber(systemMemory.totalGb),
        },
        temp: {
          valueC: asNumber(systemTemp.valueC),
          maxC: asNumber(systemTemp.maxC),
        },
        cpu: {
          avgPct: asNumber(systemCpu.avgPct),
          cores: normalizeCpuCores(systemCpu.cores),
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
    var percent = hasMemory ? (memory.usedGb / memory.totalGb) * 100 : 0;
    var usedText = hasMemory ? render.formatNumber(memory.usedGb, 2) + " GB" : "N/A";
    var totalText = hasMemory ? Math.round(memory.totalGb) + " GB total" : "Memory unavailable";

    render.renderGauge(
      elements.gauges.systemMemory,
      percent,
      usedText,
      totalText,
      hasMemory
        ? "System memory usage " + render.formatNumber(memory.usedGb, 2) + " GB of " + Math.round(memory.totalGb) + " GB"
        : "System memory unavailable",
    );
    render.renderHistory(
      elements.histories.systemMemory,
      memory.series,
      numberOrFallback(memory.totalGb, 1),
      hasMemory ? Math.round(memory.totalGb) + " GB" : "N/A",
      hasMemory ? "System memory history for the last minute" : "System memory unavailable",
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

  function renderSnapshot(snapshot) {
    renderTelemetry(snapshot);
    renderSystemMemory(snapshot);
    renderGpuUtilization(snapshot);
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
