(function () {
  var SERIES_LIMIT = 10;
  var tick = 0;

  var state = {
    system: {
      memory: {
        usedGb: 64,
        totalGb: 128,
        series: [61.8, 62.4, 63.1, 64.0, 65.2, 64.6, 63.7, 64.4, 65.0, 64.2],
      },
      temp: {
        valueC: 75,
        maxC: 104.8,
        series: [73.4, 74.1, 75.2, 76.0, 74.8, 75.6, 76.4, 75.1, 74.3, 75.0],
      },
      cpu: {
        avgPct: 50,
        cores: makeCpuCores(0),
      },
    },
    gpu: {
      utilization: {
        valuePct: 50,
        maxPct: 100,
        series: [47, 49, 52, 51, 48, 50, 53, 51, 49, 50],
      },
      temp: {
        valueC: 75,
        maxC: null,
        series: [73.8, 74.5, 75.0, 75.8, 74.9, 75.5, 76.0, 75.2, 74.6, 75.1],
      },
      power: {
        valueW: 70,
        maxW: null,
        series: [66.0, 68.5, 70.4, 72.0, 69.2, 71.1, 73.0, 70.6, 68.8, 70.2],
      },
    },
  };

  function round(value, digits) {
    var factor = Math.pow(10, digits);
    return Math.round(value * factor) / factor;
  }

  function push(series, value) {
    series.push(value);

    while (series.length > SERIES_LIMIT) {
      series.shift();
    }
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function wave(base, amplitude, speed, phase) {
    return base + Math.sin(tick * speed + phase) * amplitude;
  }

  function cloneSeries(series) {
    return series.slice();
  }

  function makeCpuCores(seed) {
    var cores = [];

    for (var index = 0; index < 20; index += 1) {
      cores.push({
        index: index,
        valuePct: round(clamp(50 + Math.sin(seed * 0.39 + index * 0.71) * 13, 0, 100), 1),
      });
    }

    return cores;
  }

  function averageCpu(cores) {
    var total = cores.reduce(function (sum, core) {
      return sum + core.valuePct;
    }, 0);

    return round(total / cores.length, 1);
  }

  function snapshot() {
    return {
      system: {
        memory: {
          usedGb: state.system.memory.usedGb,
          totalGb: state.system.memory.totalGb,
          series: cloneSeries(state.system.memory.series),
        },
        temp: {
          valueC: state.system.temp.valueC,
          maxC: state.system.temp.maxC,
          series: cloneSeries(state.system.temp.series),
        },
        cpu: {
          avgPct: state.system.cpu.avgPct,
          cores: state.system.cpu.cores.map(function (core) {
            return {
              index: core.index,
              valuePct: core.valuePct,
            };
          }),
        },
      },
      gpu: {
        utilization: {
          valuePct: state.gpu.utilization.valuePct,
          maxPct: state.gpu.utilization.maxPct,
          series: cloneSeries(state.gpu.utilization.series),
        },
        temp: {
          valueC: state.gpu.temp.valueC,
          maxC: state.gpu.temp.maxC,
          series: cloneSeries(state.gpu.temp.series),
        },
        power: {
          valueW: state.gpu.power.valueW,
          maxW: state.gpu.power.maxW,
          series: cloneSeries(state.gpu.power.series),
        },
      },
    };
  }

  function updateState() {
    tick += 1;

    state.system.memory.usedGb = round(wave(64, 2.8, 0.31, 0.2), 2);
    state.system.temp.valueC = round(wave(75, 2.1, 0.42, 1.1), 1);
    state.system.cpu.cores = makeCpuCores(tick);
    state.system.cpu.avgPct = averageCpu(state.system.cpu.cores);
    state.gpu.utilization.valuePct = Math.round(clamp(wave(50, 14, 0.38, 3.1), 0, 100));
    state.gpu.temp.valueC = round(wave(75, 2.0, 0.35, 0.4), 1);
    state.gpu.power.valueW = round(wave(70, 8, 0.58, 2.0), 1);

    push(state.system.memory.series, state.system.memory.usedGb);
    push(state.system.temp.series, state.system.temp.valueC);
    push(state.gpu.utilization.series, state.gpu.utilization.valuePct);
    push(state.gpu.temp.series, state.gpu.temp.valueC);
    push(state.gpu.power.series, state.gpu.power.valueW);
  }

  function getSnapshot() {
    updateState();
    return snapshot();
  }

  window.SparkMockData = {
    getSnapshot: getSnapshot,
  };
})();
