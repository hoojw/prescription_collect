"""Small standard-library HTTP server intended for loopback or Nginx use."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .app import TmallBatchApplication
from .config import ConfigurationError, load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tmall prescription extension receiver")
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "TMALL_EXTENSION_RECEIVER_CONFIG",
            "/etc/prescription-collector/tmall-extension-receiver.json",
        ),
    )
    return parser


def make_handler(app: TmallBatchApplication, max_body_bytes: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tmall-extension-receiver/0.1"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _dispatch(self) -> None:
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = max_body_bytes + 1
            if self.headers.get("Transfer-Encoding") or length < 0 or length > max_body_bytes:
                raw_body = b"x" * (max_body_bytes + 1)
            else:
                raw_body = self.rfile.read(length) if length else b""
            result = app.handle(
                http_method=self.command,
                path=path,
                headers={key.lower(): value for key, value in self.headers.items()},
                raw_body=raw_body,
            )
            self.send_response(result.status)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.end_headers()
            self.wfile.write(result.body)

        do_GET = _dispatch
        do_POST = _dispatch
        do_OPTIONS = _dispatch

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
    server = ThreadingHTTPServer(
        (settings.listen_host, settings.listen_port),
        make_handler(TmallBatchApplication(settings), settings.max_body_bytes),
    )
    server.daemon_threads = True
    server.timeout = 0.5
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logging.getLogger(__name__).info(
        "listening host=%s port=%s path=%s",
        settings.listen_host,
        settings.listen_port,
        settings.ingest_path,
    )
    try:
        while not stop_requested:
            server.handle_request()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

