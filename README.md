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

Then open `http://192.168.1.123:8088`. The mDNS name
`HAL-9000.local` may also work on networks where `.local` resolution is
available, but it is not required by the dashboard.

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
