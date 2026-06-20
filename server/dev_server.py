"""Development server for the Spark dashboard."""

from __future__ import annotations

import argparse
import json
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from collectors import collect_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SparkDashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, force_mock: bool = False, **kwargs):
        self.force_mock = force_mock
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/snapshot":
            self.send_snapshot()
            return

        super().do_GET()

    def send_snapshot(self) -> None:
        payload = json.dumps(collect_snapshot(use_mock=self.force_mock)).encode("utf-8")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Spark dashboard dev server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8088, type=int)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="serve mock telemetry even when live collectors are available",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    handler = partial(
        SparkDashboardHandler,
        directory=str(PROJECT_ROOT),
        force_mock=args.mock,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"Serving Spark Dashboard on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
