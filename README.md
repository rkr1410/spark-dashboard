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
- `server/collectors.py` reads `/proc/meminfo`, thermal zones, and `nvidia-smi`
  when those interfaces are available.

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

The API endpoint is `http://HAL-9000.local:8088/api/snapshot`.
