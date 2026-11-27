"""Small HTTP server for deployment behind a Jushita SLB or Nginx."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .app import ForwarderApplication
from .config import ConfigurationError, load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alihealth Jushita Qimen forwarder")
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "ALIHEALTH_FORWARDER_CONFIG",
            "/etc/alihealth-forwarder/config.json",
        ),
    )
    return parser


def make_handler(app: ForwarderApplication, max_body_bytes: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "alihealth-jst-forwarder/0.1"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _dispatch(self) -> None:
            parsed = urlsplit(self.path)
            content_length_text = self.headers.get("Content-Length", "0")
            try:
                content_length = int(content_length_text)
            except ValueError:
                content_length = max_body_bytes + 1
            if content_length < 0 or content_length > max_body_bytes:
                raw_body = b"x" * (max_body_bytes + 1)
            else:
                raw_body = self.rfile.read(content_length) if content_length else b""
            headers = {key.lower(): value for key, value in self.headers.items()}
            result = app.handle(
                http_method=self.command,
                path=parsed.path,
                query_string=parsed.query,
                headers=headers,
                raw_body=raw_body,
            )
            self.send_response(result.status)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(result.body)

        do_GET = _dispatch
        do_POST = _dispatch

    return Handler


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = load_settings(args.config)
    except ConfigurationError as exc:
        logging.getLogger(__name__).error("configuration error: %s", exc)
        return 2

    app = ForwarderApplication(settings)
    server = ThreadingHTTPServer(
        (settings.listen_host, settings.listen_port),
        make_handler(app, settings.max_body_bytes),
    )
    server.timeout = 0.5
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logging.getLogger(__name__).info(
        "listening host=%s port=%s",
        settings.listen_host,
        settings.listen_port,
    )
    try:
        while not stop:
            server.handle_request()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
