# Spark Dashboard

Static dashboard mock for shaping the DGX-like layout before wiring live data.

Open `index.html` directly in a browser. Theme tokens live in
`src/styles/theme.css`; changing the palette should be contained there.

The current runtime is dependency-free:

- `src/scripts/mock-data.js` owns the mock telemetry snapshot.
- `src/scripts/render.js` owns DOM and SVG rendering helpers.
- `src/scripts/app.js` refreshes the page once per second.

When a backend exists, replace `SparkMockData.getSnapshot()` with a fetch from
the local API and keep the renderer unchanged.
