(function () {
  var data = window.SparkMockData;
  var render = window.SparkRender;

  var elements = {
    updatedAt: document.querySelector("[data-updated-at]"),
    values: {
      systemTemp: document.querySelector('[data-value="system.temp"]'),
      systemTempLimit: document.querySelector('[data-limit="system.temp"]'),
      systemPower: document.querySelector('[data-value="system.power"]'),
      systemPowerLimit: document.querySelector('[data-limit="system.power"]'),
      gpuTemp: document.querySelector('[data-value="gpu.temp"]'),
      gpuTempLimit: document.querySelector('[data-limit="gpu.temp"]'),
      gpuPower: document.querySelector('[data-value="gpu.power"]'),
    },
    sparklines: {
      systemTemp: document.querySelectorAll('[data-sparkline-band="system.temp"]'),
      systemPower: document.querySelector('[data-sparkline="system.power"]'),
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

  function updateStatus() {
    var now = new Date();
    var time = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });

    if (elements.updatedAt) {
      elements.updatedAt.textContent = "Mock data - " + time;
    }
  }

  function renderTelemetry(snapshot) {
    render.renderMetricValue(
      elements.values.systemTemp,
      null,
      Math.round(snapshot.system.temp.valueC) + " C",
      "",
    );
    render.renderMetricValue(
      elements.values.systemPower,
      elements.values.systemPowerLimit,
      Math.round(snapshot.system.power.valueW) + " W",
      snapshot.system.power.maxW == null ? "" : "/ " + Math.round(snapshot.system.power.maxW) + " W",
    );
    render.renderMetricValue(
      elements.values.gpuTemp,
      null,
      Math.round(snapshot.gpu.temp.valueC) + " C",
      "",
    );
    render.renderMetricValue(
      elements.values.gpuPower,
      null,
      Math.round(snapshot.gpu.power.valueW) + " W",
      "",
    );

    render.renderSparkline(elements.sparklines.systemTemp, snapshot.system.temp.series, telemetryScales.temp);
    render.renderSparkline(elements.sparklines.systemPower, snapshot.system.power.series, telemetryScales.power);
    render.renderSparkline(elements.sparklines.gpuTemp, snapshot.gpu.temp.series, telemetryScales.temp);
    render.renderSparkline(elements.sparklines.gpuPower, snapshot.gpu.power.series, telemetryScales.power);
  }

  function renderSystemMemory(snapshot) {
    var memory = snapshot.system.memory;
    var percent = (memory.usedGb / memory.totalGb) * 100;

    render.renderGauge(
      elements.gauges.systemMemory,
      percent,
      render.formatNumber(memory.usedGb, 2) + " GB",
      Math.round(memory.totalGb) + " GB total",
      "System memory usage " + render.formatNumber(memory.usedGb, 2) + " GB of " + Math.round(memory.totalGb) + " GB",
    );
    render.renderHistory(
      elements.histories.systemMemory,
      memory.series,
      memory.totalGb,
      Math.round(memory.totalGb) + " GB",
      "System memory history for the last minute",
    );
  }

  function renderGpuUtilization(snapshot) {
    var utilization = snapshot.gpu.utilization;

    render.renderGauge(
      elements.gauges.gpuUtilization,
      utilization.valuePct,
      Math.round(utilization.valuePct) + "%",
      "GPU active",
      "GPU utilization " + Math.round(utilization.valuePct) + " percent",
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
    updateStatus();
  }

  function tick() {
    renderSnapshot(data.getSnapshot());
  }

  if (!data || !render) {
    return;
  }

  tick();
  window.setInterval(tick, 1000);
})();
