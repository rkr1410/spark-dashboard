(function () {
  var SERIES_LIMIT = 10;
  var tick = 0;

  var state = {
    system: {
      memory: {
        usedGb: 3.31,
        totalGb: 128,
        series: [3.18, 3.2, 3.19, 3.23, 3.24, 3.28, 3.26, 3.31, 3.29, 3.31],
      },
      temp: {
        valueC: 46,
        maxC: 104,
        series: [45.5, 45.7, 45.2, 46.1, 45.9, 46.6, 46.0, 46.8, 46.3, 46.7],
      },
      power: {
        valueW: 15,
        maxW: 140,
        series: [14.0, 14.5, 14.4, 15.0, 14.9, 16.1, 15.5, 15.9, 15.3, 15.8],
      },
    },
    gpu: {
      utilization: {
        valuePct: 0,
        maxPct: 100,
        series: [0, 0, 0, 1, 0, 0, 1, 0, 0, 0],
      },
      temp: {
        valueC: 42,
        maxC: 104,
        series: [41.8, 41.9, 41.8, 42.0, 41.9, 42.1, 42.0, 42.2, 42.1, 42.2],
      },
      power: {
        valueW: 3,
        maxW: null,
        series: [2.8, 2.7, 2.9, 2.8, 3.1, 3.0, 3.2, 3.1, 3.3, 3.2],
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

  function wave(base, amplitude, speed, phase) {
    return base + Math.sin(tick * speed + phase) * amplitude;
  }

  function cloneSeries(series) {
    return series.slice();
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
        power: {
          valueW: state.system.power.valueW,
          maxW: state.system.power.maxW,
          series: cloneSeries(state.system.power.series),
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

    state.system.memory.usedGb = round(wave(3.31, 0.06, 0.31, 0.2), 2);
    state.system.temp.valueC = round(wave(46.2, 0.8, 0.42, 1.1), 1);
    state.system.power.valueW = round(wave(15.3, 1.1, 0.47, 2.4), 1);
    state.gpu.utilization.valuePct = Math.max(0, Math.round(wave(0.4, 0.7, 0.38, 3.1)));
    state.gpu.temp.valueC = round(wave(42.1, 0.35, 0.35, 0.4), 1);
    state.gpu.power.valueW = round(wave(3.1, 0.35, 0.58, 2.0), 1);

    push(state.system.memory.series, state.system.memory.usedGb);
    push(state.system.temp.series, state.system.temp.valueC);
    push(state.system.power.series, state.system.power.valueW);
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
