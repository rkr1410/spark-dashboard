(function () {
  var SERIES_LIMIT = 10;
  var tick = 0;

  var state = {
    system: {
      memory: {
        usedGb: 14,
        residentGb: 84,
        fileGb: 82,
        anonGb: 0.55,
        cudaGb: 8,
        processCount: 1,
        totalGb: 128,
        series: [12.7, 13.1, 13.8, 14.0, 14.8, 14.2, 13.7, 14.1, 14.6, 14.0],
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
      network: {
        rxBytesPerSec: 84000000,
        txBytesPerSec: 2100000,
        rxSeries: [62000000, 74000000, 69000000, 91000000, 86000000, 103000000, 94000000, 88000000, 79000000, 84000000],
        txSeries: [1600000, 1800000, 1700000, 2300000, 1900000, 2600000, 2400000, 2000000, 2200000, 2100000],
      },
      disk: {
        readBytesPerSec: 1200000000,
        writeBytesPerSec: 640000000,
        readSeries: [740000000, 980000000, 860000000, 1320000000, 1100000000, 1440000000, 1250000000, 1080000000, 1160000000, 1200000000],
        writeSeries: [380000000, 520000000, 470000000, 710000000, 580000000, 760000000, 690000000, 610000000, 670000000, 640000000],
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
    inference: {
      available: true,
      runtime: "sglang",
      model: "qwen3.8-27b",
      contextTokens: 262144,
      genThroughput: 42.8,
      numUsedTokens: 67800,
      maxTotalNumTokens: 455439,
      specAcceptRate: 0.61,
      specAcceptLength: 3.8,
      prefixHitRate: 0.84,
      cacheHitRate: 0.84,
      prefillCacheTokens: 62208,
      prefillComputeTokens: 23008,
      prefillCacheDeltaTokens: 900,
      prefillComputeDeltaTokens: 230,
      decodeTokens: 5822,
      decodeDeltaTokens: 43,
      numRunningReqs: 1,
      numQueueReqs: 0,
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
          residentGb: state.system.memory.residentGb,
          fileGb: state.system.memory.fileGb,
          anonGb: state.system.memory.anonGb,
          cudaGb: state.system.memory.cudaGb,
          processCount: state.system.memory.processCount,
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
        network: {
          rxBytesPerSec: state.system.network.rxBytesPerSec,
          txBytesPerSec: state.system.network.txBytesPerSec,
          rxSeries: cloneSeries(state.system.network.rxSeries),
          txSeries: cloneSeries(state.system.network.txSeries),
        },
        disk: {
          readBytesPerSec: state.system.disk.readBytesPerSec,
          writeBytesPerSec: state.system.disk.writeBytesPerSec,
          readSeries: cloneSeries(state.system.disk.readSeries),
          writeSeries: cloneSeries(state.system.disk.writeSeries),
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
      inference: {
        available: state.inference.available,
        runtime: state.inference.runtime,
        model: state.inference.model,
        contextTokens: state.inference.contextTokens,
        genThroughput: state.inference.genThroughput,
        numUsedTokens: state.inference.numUsedTokens,
        maxTotalNumTokens: state.inference.maxTotalNumTokens,
        specAcceptRate: state.inference.specAcceptRate,
        specAcceptLength: state.inference.specAcceptLength,
        prefixHitRate: state.inference.prefixHitRate,
        cacheHitRate: state.inference.cacheHitRate,
        prefillCacheTokens: state.inference.prefillCacheTokens,
        prefillComputeTokens: state.inference.prefillComputeTokens,
        prefillCacheDeltaTokens: state.inference.prefillCacheDeltaTokens,
        prefillComputeDeltaTokens: state.inference.prefillComputeDeltaTokens,
        decodeTokens: state.inference.decodeTokens,
        decodeDeltaTokens: state.inference.decodeDeltaTokens,
        numRunningReqs: state.inference.numRunningReqs,
        numQueueReqs: state.inference.numQueueReqs,
      },
    };
  }

  function updateState() {
    tick += 1;

    state.system.memory.usedGb = round(wave(14, 1.4, 0.31, 0.2), 2);
    state.system.memory.residentGb = round(wave(84, 3.2, 0.19, 0.8), 2);
    state.system.memory.fileGb = round(wave(82, 3.0, 0.19, 0.8), 2);
    state.system.memory.cudaGb = round(wave(8, 0.4, 0.22, 1.5), 2);
    state.system.temp.valueC = round(wave(75, 2.1, 0.42, 1.1), 1);
    state.system.cpu.cores = makeCpuCores(tick);
    state.system.cpu.avgPct = averageCpu(state.system.cpu.cores);
    state.system.network.rxBytesPerSec = Math.round(clamp(wave(84000000, 25000000, 0.47, 0.6), 0, Infinity));
    state.system.network.txBytesPerSec = Math.round(clamp(wave(2100000, 900000, 0.61, 1.3), 0, Infinity));
    state.system.disk.readBytesPerSec = Math.round(clamp(wave(1200000000, 360000000, 0.44, 2.2), 0, Infinity));
    state.system.disk.writeBytesPerSec = Math.round(clamp(wave(640000000, 220000000, 0.53, 0.9), 0, Infinity));
    state.gpu.utilization.valuePct = Math.round(clamp(wave(50, 14, 0.38, 3.1), 0, 100));
    state.gpu.temp.valueC = round(wave(75, 2.0, 0.35, 0.4), 1);
    state.gpu.power.valueW = round(wave(70, 8, 0.58, 2.0), 1);
    state.inference.numRunningReqs = tick % 28 < 14 ? 1 : 0;
    state.inference.genThroughput = state.inference.numRunningReqs
      ? round(wave(42.8, 7.5, 0.41, 0.2), 1)
      : 0;
    state.inference.numUsedTokens = Math.round(clamp(wave(67800, 18000, 0.23, 1.2), 0, Infinity));
    state.inference.specAcceptRate = state.inference.numRunningReqs
      ? round(clamp(wave(0.61, 0.12, 0.37, 0.5), 0, 1), 2)
      : 0;
    state.inference.specAcceptLength = state.inference.numRunningReqs
      ? round(clamp(wave(3.8, 0.8, 0.32, 0.1), 0, Infinity), 1)
      : 0;
    state.inference.cacheHitRate = state.inference.numRunningReqs
      ? round(clamp(wave(0.84, 0.09, 0.29, 0.9), 0, 1), 2)
      : state.inference.cacheHitRate;

    var cacheDelta = state.inference.numRunningReqs
      ? Math.round(clamp(900 + Math.sin(tick * 0.4) * 260, 0, Infinity))
      : 0;
    var computeDelta = state.inference.numRunningReqs
      ? Math.round(clamp(230 + Math.sin(tick * 0.3) * 90, 0, Infinity))
      : 0;
    var totalDelta = cacheDelta + computeDelta;

    state.inference.prefixHitRate = totalDelta > 0 ? cacheDelta / totalDelta : null;
    state.inference.prefillCacheDeltaTokens = cacheDelta;
    state.inference.prefillComputeDeltaTokens = computeDelta;
    state.inference.prefillCacheTokens += cacheDelta;
    state.inference.prefillComputeTokens += computeDelta;
    state.inference.decodeDeltaTokens = state.inference.numRunningReqs
      ? Math.round(clamp(state.inference.genThroughput, 0, Infinity))
      : 0;
    state.inference.decodeTokens += state.inference.decodeDeltaTokens;

    push(state.system.memory.series, state.system.memory.usedGb);
    push(state.system.temp.series, state.system.temp.valueC);
    push(state.system.network.rxSeries, state.system.network.rxBytesPerSec);
    push(state.system.network.txSeries, state.system.network.txBytesPerSec);
    push(state.system.disk.readSeries, state.system.disk.readBytesPerSec);
    push(state.system.disk.writeSeries, state.system.disk.writeBytesPerSec);
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
