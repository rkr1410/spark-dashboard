const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const keys = ["throughput", "context", "draft", "prefix", "requests", "duration"];

function inference(overrides = {}) {
  return {
    available: true,
    runtime: "sglang",
    model: "same-model",
    supportsAbort: true,
    genThroughput: 40,
    contextTokens: 262144,
    maxTotalNumTokens: 400000,
    numUsedTokens: 1000,
    specAcceptRate: 0.75,
    specAcceptLength: 4,
    prefixHitRate: 0.8,
    prefillCacheTokens: 800,
    prefillComputeTokens: 200,
    prefillCacheDeltaTokens: 0,
    prefillComputeDeltaTokens: 0,
    decodeDeltaTokens: 40,
    numRunningReqs: 1,
    numQueueReqs: 0,
    ...overrides,
  };
}

async function dashboard(initial, storage = new Map()) {
  let current = initial;
  let clock = 100000;
  let tick;
  const posts = [];
  const elements = new Map();
  for (const selector of ["[data-inference-source]", ...keys.flatMap(key => [
    `[data-inference-stat="${key}"]`, `[data-inference-tile="${key}"]`,
  ])]) {
    const classes = new Set();
    elements.set(selector, {
      textContent: "",
      attributes: {},
      listeners: {},
      styles: {},
      classList: {
        add: name => classes.add(name),
        remove: name => classes.delete(name),
        toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
        contains: name => classes.has(name),
      },
      style: { setProperty(name, value) { elements.get(selector).styles[name] = value; } },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
    });
  }
  const context = vm.createContext({
    Date: class extends Date { static now() { return clock; } },
    document: {
      querySelector: selector => elements.get(selector) || null,
      querySelectorAll: () => [],
    },
    window: {
      SparkMockData: { getSnapshot() { throw new Error("Unexpected fallback to mock telemetry"); } },
      location: { protocol: "http:" },
      localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
      setInterval: callback => { tick = callback; },
      setTimeout: () => 1,
      clearTimeout: () => {},
      fetch: async (url, options) => {
        if (options.method === "POST") posts.push(url);
        return { ok: true, json: async () => ({ inference: current, source: { mode: "live" } }) };
      },
    },
  });
  for (const file of ["render.js", "app.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, file), "utf8"), context, { filename: file });
  }
  await new Promise(setImmediate);
  return {
    posts, storage,
    source: elements.get("[data-inference-source]"),
    value: key => elements.get(`[data-inference-stat="${key}"]`).textContent,
    tile: key => elements.get(`[data-inference-tile="${key}"]`),
    async step(next, elapsed = 1000) {
      current = next;
      clock += elapsed;
      tick();
      await new Promise(setImmediate);
    },
  };
}

test("missing llama fields render as unavailable, not zero", async () => {
  const page = await dashboard(inference({
    runtime: "llamacpp", supportsAbort: false,
    genThroughput: null, contextTokens: null, numUsedTokens: null,
    specAcceptRate: null, specAcceptLength: null, prefixHitRate: null,
    prefillCacheTokens: null, prefillComputeTokens: null,
    prefillCacheDeltaTokens: null, prefillComputeDeltaTokens: null,
  }));
  for (const key of ["throughput", "context", "draft", "prefix"]) {
    assert.equal(page.value(key), "--", key);
    assert.equal(page.tile(key).classList.contains("is-offline"), true, key);
  }
  assert.match(page.source.textContent, /llama.cpp/);
  assert.equal(page.tile("duration").classList.contains("is-abort-enabled"), false);
});

test("llama's stale throughput does not start activity, but a running prefill does", async () => {
  const idle = inference({ runtime: "llamacpp", supportsAbort: false, numRunningReqs: 0, decodeDeltaTokens: 0 });
  const page = await dashboard(idle);
  assert.equal(page.value("throughput"), "--");
  await page.step(idle, 5000);
  assert.equal(page.value("duration"), "0s");
  const prefill = { ...idle, numRunningReqs: 1 };
  await page.step(prefill);
  await page.step(prefill);
  assert.equal(page.value("requests"), "1");
  assert.equal(page.value("duration"), "1s");
});

test("switching runtimes with the same model alias clears retained SGLang values", async () => {
  const page = await dashboard(inference());
  assert.equal(page.value("draft"), "0.75 / 4.0");
  assert.equal(page.value("prefix"), "80%");
  const llama = inference({
    runtime: "llamacpp", supportsAbort: false, numRunningReqs: 0, decodeDeltaTokens: 0,
    specAcceptRate: null, specAcceptLength: null, prefixHitRate: null,
    prefillCacheTokens: null, prefillComputeTokens: null,
  });
  await page.step(llama);
  assert.equal(page.value("draft"), "--");
  assert.equal(page.value("prefix"), "--");
  assert.equal(page.value("throughput"), "--");
  assert.equal(page.value("duration"), "0s");
});

test("last run survives reload for the same identity but never a different runtime", async () => {
  const page = await dashboard(inference());
  await page.step(inference());
  const reloaded = await dashboard(inference({ numRunningReqs: 0, decodeDeltaTokens: 0 }), page.storage);
  assert.equal(reloaded.value("throughput"), "40.0 tok/s");
  const switched = await dashboard(inference({ runtime: "llamacpp", supportsAbort: false, numRunningReqs: 0, decodeDeltaTokens: 0 }), page.storage);
  assert.equal(switched.value("throughput"), "--");
});

test("legacy saved state without an identity cannot leak to a new runtime", async () => {
  const storage = new Map([["spark-dashboard.inference.last-run.v1", JSON.stringify({
    savedAt: 100000, lastActiveAt: 100000, lastValues: { throughput: "999 tok/s" },
  })]]);
  const page = await dashboard(inference({ runtime: "llamacpp", numRunningReqs: 0, decodeDeltaTokens: 0 }), storage);
  assert.equal(page.value("throughput"), "--");
});

test("abort remains available for SGLang and is inert for llama.cpp", async () => {
  const page = await dashboard(inference());
  page.tile("duration").listeners.click();
  page.tile("duration").listeners.click();
  assert.deepEqual(page.posts, ["/api/inference/abort"]);
  const llama = await dashboard(inference({ runtime: "llamacpp", supportsAbort: false }));
  llama.tile("duration").listeners.click();
  llama.tile("duration").listeners.click();
  assert.deepEqual(llama.posts, []);
});

test("missing activity signals do not display a fabricated duration", async () => {
  const page = await dashboard(inference({
    runtime: "llamacpp", numRunningReqs: null, numQueueReqs: null, decodeDeltaTokens: null,
    prefillCacheDeltaTokens: null, prefillComputeDeltaTokens: null,
  }));
  assert.equal(page.value("duration"), "--");
  assert.equal(page.value("requests"), "--");
  assert.equal(page.value("throughput"), "40.0 tok/s");
});

test("disabled metrics explain the missing source while hardware still renders", async () => {
  const page = await dashboard(inference({ available: false, message: "Metrics disabled: start llama-server with --metrics", runtime: "llamacpp" }));
  assert.match(page.source.textContent, /--metrics/);
  for (const key of keys) assert.equal(page.value(key), "--");
});

test("live slot speed and current-request cache replace frozen Prometheus values", async () => {
  const snapshot = inference({
    runtime: "llamacpp", supportsAbort: false, throughputSource: "slots", prefixHitRateSource: "slots",
    genThroughput: null, decodeDeltaTokens: null, prefixHitRate: 0.963,
  });
  const page = await dashboard(snapshot);
  assert.equal(page.value("throughput"), "--");
  assert.equal(page.value("prefix"), "96%");
  await page.step({ ...snapshot, genThroughput: 18, decodeDeltaTokens: 18 });
  assert.equal(page.value("throughput"), "18.0 tok/s");
  // A new request's cache rate must replace the old one even before the
  // aggregate Prometheus counters or their deltas advance.
  await page.step({ ...snapshot, genThroughput: 19, decodeDeltaTokens: 19, prefixHitRate: 0.1 });
  assert.equal(page.value("prefix"), "10%");
});
