# Spark Dashboard

Dependency-free dashboard prototype for shaping the DGX-like layout and wiring
the first live Spark telemetry.

Open `index.html` directly in a browser. Theme tokens live in
`src/styles/theme.css`; changing the palette should be contained there.

The current runtime is dependency-free:

- `src/scripts/mock-data.js` owns the mock telemetry snapshot.
- `src/scripts/render.js` owns DOM and SVG rendering helpers.
- `src/scripts/app.js` refreshes the page once per second and uses `/api/snapshot`
  when the page is served by the dev server.
- `server/dev_server.py` serves the static UI plus the JSON snapshot API.
- `server/collectors.py` reads `/proc/meminfo`, thermal zones, `/proc/stat`,
  and NVIDIA GPU metrics through NVML when those interfaces are available.

Local mock-only preview:

```sh
open index.html
```

Server preview:

```sh
python3 server/dev_server.py --host 127.0.0.1 --port 8088
```

Mock server preview:

```sh
python3 server/dev_server.py --host 127.0.0.1 --port 8088 --mock
```

On HAL, expose the server on the LAN:

```sh
python3 server/dev_server.py --host 0.0.0.0 --port 8088
```

Then open `http://192.168.1.123:8088`. The fixed IP keeps the dashboard reachable independently of the server hostname.

Private SSH tunnel preview, without using the DGX dashboard port `11000`:

```sh
ssh -N -L 8088:localhost:8088 gx10
```

Then open `http://localhost:8088`. If local port `8088` is already busy, use an
alternate local port while still targeting HAL's `8088`:

```sh
ssh -N -L 18088:localhost:8088 gx10
```

Then open `http://localhost:18088`.

The browser UI and API are served from the same origin; the frontend fetches
`/api/snapshot`.

## Inference telemetry

The collector detects SGLang (`sglang:*`) or llama.cpp (`llamacpp:*`) from
`http://localhost:8000/metrics`. No runtime selection is needed when changing
the server on port 8000. The inference panel shows the detected runtime and
model. Missing metrics appear as `--`; hover a tile for its meaning or limitation.

Enable the exporter when starting the model server: `--enable-metrics` for
SGLang or `--metrics` for `llama-server`. This endpoint is `/metrics`, not
`/v1/metrics`. If metrics are disabled, native metadata endpoints can still
identify the runtime and the dashboard explains what is missing.

| Tile | SGLang | llama.cpp |
| --- | --- | --- |
| Generation | `gen_throughput` | Live changes in `/slots` `next_token.n_decoded` divided by elapsed time; server average as fallback |
| Context | Used tokens and capacity; existing per-request limit is 262,144 | Current token counts and capacity from `/slots`, default limit from `/props` |
| Draft | `spec_accept_rate` / `spec_accept_length` | Accepted / drafted; `1 + accepted / verification steps`, cumulative since startup; unavailable without draft tokens |
| Prefix | Deltas of `realtime_tokens_total` for cached and computed prompt tokens | Current slots: `n_prompt_tokens_cache / (n_prompt_tokens_cache + n_prompt_tokens_processed)`; Prometheus counters as fallback |
| Requests | `num_running_reqs`; queue in tooltip | `requests_processing` (or `/slots`); `requests_deferred` in tooltip |
| Duration | Observed activity time | Observed activity time; running slots also detect long prefills |

llama.cpp's `/slots` is normally enabled; do not pass `--no-slots` if context
usage is needed. Missing `/slots` or `/props` does not disable the other metrics.
`n_tokens_max` is a historical maximum and is never used as current context
occupancy. Prompt and generated text are not included in dashboard snapshots.

Some llama.cpp builds leave Prometheus throughput at zero and commit token
counters only between requests. Live speed therefore uses two slot samples,
tracks task IDs when slots are reused, and does not count a completed request
again when Prometheus catches up. Prompt processing is excluded from generation
tok/s. The first sample or a polling gap over five seconds shows `--` until a
new baseline is available. Slot-based prefix reuse also works when opening the
dashboard halfway through a request, without waiting for counter changes.

Last-run values are retained across page reloads for the same runtime/model,
but cleared when the source changes. Missing or reset counters establish a new
baseline. The duration tile's abort action remains available only for SGLang;
the backend also rejects this action for llama.cpp or an unknown server.

To monitor a different inference address, set `INFERENCE_BASE_URL` (without
`/v1`) when starting the dashboard:

```sh
INFERENCE_BASE_URL=http://192.168.1.123:8000 python3 server/dev_server.py --host 0.0.0.0 --port 8088
```

## Tests

No additional dependencies are required:

```sh
python3 -m unittest discover -s server -v
node --test src/scripts/app.test.js
```
