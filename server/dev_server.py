"""Development server for the Spark dashboard."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from collectors import build_startup_report, collect_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SGLANG_ABORT_URL = "http://localhost:8000/pause_generation"
SGLANG_CONTINUE_URL = "http://localhost:8000/continue_generation"


class SparkDashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, force_mock: bool = False, **kwargs):
        self.force_mock = force_mock
        super().__init__(*args, **kwargs)

    def send_head(self):
        # This is a local dev dashboard. Always send fresh static files so the
        # browser cannot mix old JS/CSS with new HTML after rsync.
        for header in ("If-Modified-Since", "If-None-Match"):
            if header in self.headers:
                del self.headers[header]

        return super().send_head()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/snapshot":
            self.send_snapshot()
            return

        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/api/inference/abort":
            self.send_inference_abort()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "File not found")

    def send_snapshot(self) -> None:
        self.send_json(collect_snapshot(use_mock=self.force_mock))

    def send_inference_abort(self) -> None:
        if self.force_mock:
            self.send_json({"ok": True, "source": "mock"})
            return

        abort_error = self.post_sglang_control(SGLANG_ABORT_URL, {"mode": "abort"})
        continue_error = self.post_sglang_control(SGLANG_CONTINUE_URL, {})

        if abort_error or continue_error:
            self.send_json(
                {
                    "ok": False,
                    "abortError": abort_error,
                    "continueError": continue_error,
                    "source": "sglang_pause_continue",
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return

        self.send_json({"ok": True, "source": "sglang_pause_continue"})

    def post_sglang_control(self, url: str, body: object) -> str | None:
        request_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                response.read(200_000)
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            return str(error)

        return None

    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value).encode("utf-8")

        self.send_response(status)
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
    for line in build_startup_report(use_mock=args.mock):
        print(line)
    server.serve_forever()


if __name__ == "__main__":
    main()
