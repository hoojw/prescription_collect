#!/usr/bin/env python3
"""Run selected prescription collectors sequentially from one registry."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Any


STORE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SERVICE_PATTERNS = {
    "MEITUAN": re.compile(r"^meituan-store-chrome@[a-z0-9_-]+\.service$"),
    "JINGDONG": re.compile(r"^jingdong-store-chrome@[a-z0-9_-]+\.service$"),
}
OCR_CPU_THREADS = "3"
os.environ["PRESCRIPTION_OCR_THREADS"] = OCR_CPU_THREADS
os.environ["PADDLE_PDX_CPU_NUM_THREADS"] = OCR_CPU_THREADS
for _thread_env_name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_thread_env_name] = "1"


def load_enabled_stores(path: pathlib.Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("store registry schema_version must be 1")
    raw_stores = payload.get("stores")
    if not isinstance(raw_stores, list):
        raise ValueError("store registry stores must be a list")
    selection = payload.get("selection", {"mode": "all"})
    if not isinstance(selection, dict):
        raise ValueError("store registry selection must be an object")
    selection_mode = str(selection.get("mode") or "all").strip().lower()
    if selection_mode not in {"all", "single"}:
        raise ValueError("store registry selection.mode must be 'all' or 'single'")
    selected_key = str(selection.get("store_key") or "").strip()
    if selection_mode == "single" and not selected_key:
        raise ValueError("store registry selection.store_key is required in single mode")

    stores: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_configs: set[str] = set()
    seen_services: set[str] = set()
    seen_ports: set[int] = set()
    selected_entry_found = False
    selected_entry_enabled = False
    for item in raw_stores:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        enabled = bool(item.get("enabled", False))
        if key == selected_key:
            selected_entry_found = True
            selected_entry_enabled = selected_entry_enabled or enabled
        if not enabled:
            continue
        if selection_mode == "single" and key != selected_key:
            continue
        config = str(item.get("config") or "").strip()
        platform = str(item.get("platform") or "MEITUAN").strip().upper()
        name = str(item.get("name") or key).strip()
        browser_service = str(item.get("browser_service") or "").strip()
        if platform not in {"MEITUAN", "JINGDONG"}:
            raise ValueError(f"unsupported enabled store platform: {platform!r}")
        if not STORE_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid enabled store key: {key!r}")
        if not SERVICE_PATTERNS[platform].fullmatch(browser_service):
            raise ValueError(
                f"invalid browser_service for {platform}: {browser_service!r}"
            )
        config_path = pathlib.Path(config).expanduser().resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"store config not found: {config_path}")
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
        chrome_port = int(config_payload.get("chrome_port") or 0)
        if not 1 <= chrome_port <= 65535:
            raise ValueError(f"invalid chrome_port for enabled store {key}: {chrome_port}")
        if (
            key in seen_keys
            or str(config_path) in seen_configs
            or browser_service in seen_services
            or chrome_port in seen_ports
        ):
            raise ValueError(f"duplicate enabled store entry: {key}")
        seen_keys.add(key)
        seen_configs.add(str(config_path))
        seen_services.add(browser_service)
        seen_ports.add(chrome_port)
        stores.append(
            {
                "key": key,
                "name": name,
                "platform": platform,
                "config": str(config_path),
                "browser_service": browser_service,
                "chrome_port": str(chrome_port),
            }
        )

    if selection_mode == "single":
        if not selected_entry_found:
            raise ValueError(f"selected store key not found: {selected_key}")
        if not selected_entry_enabled:
            raise ValueError(f"selected store is disabled: {selected_key}")
    if not stores:
        raise ValueError("store registry selection has no enabled stores")
    return stores


def systemctl(action: str, service: str, *, check: bool = False) -> subprocess.CompletedProcess:
    if action not in {"start", "stop"}:
        raise ValueError(f"unsupported systemctl action: {action}")
    return subprocess.run(
        ["systemctl", action, service],
        check=check,
        text=True,
        capture_output=True,
    )


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_port(port: int, *, open_state: bool, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_open(port) is open_state:
            return
        time.sleep(0.25)
    state = "open" if open_state else "closed"
    raise TimeoutError(f"Chrome port {port} did not become {state} within {timeout}s")


def stop_browser(service: str, port: int, timeout: float) -> None:
    print(f"BROWSER_STOP service={service} port={port}", flush=True)
    completed = systemctl("stop", service)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"failed to stop {service}: {detail}")
    wait_for_port(port, open_state=False, timeout=timeout)


def start_browser(service: str, port: int, timeout: float) -> None:
    # Clear a stale instance first so every collection gets a fresh Chrome process
    # while retaining the store's persistent profile and login state.
    stop_browser(service, port, timeout)
    print(f"BROWSER_START service={service} port={port}", flush=True)
    completed = systemctl("start", service)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"failed to start {service}: {detail}")
    wait_for_port(port, open_state=True, timeout=timeout)
    print(f"BROWSER_READY service={service} port={port}", flush=True)


def format_fleet_summary(
    summary: dict[str, Any],
    *,
    check_login: bool,
) -> str:
    """Return an operations-friendly batch summary for terminal output."""
    results = list(summary.get("results") or [])
    title = "店铺登录预检结果" if check_login else "多店铺采集结果"
    lines = [
        "",
        "==========================================",
        title,
        "==========================================",
        f"店铺总数：{int(summary.get('stores') or 0)}",
        f"成功数量：{int(summary.get('success') or 0)}",
        f"需处理数量：{int(summary.get('failed') or 0)}",
        f"总耗时：{float(summary.get('elapsed_seconds') or 0):.3f} 秒",
        "",
        "店铺明细：",
    ]
    platform_names = {"MEITUAN": "美团", "JINGDONG": "京东"}
    for index, item in enumerate(results, start=1):
        exit_code = int(item.get("exit_code") or 0)
        if check_login:
            if exit_code == 0:
                status = "登录正常"
            elif exit_code == 2:
                status = "需要重新登录或核对店铺"
            else:
                status = "检查失败"
        else:
            status = "采集完成" if exit_code == 0 else "采集失败"
        platform = platform_names.get(
            str(item.get("platform") or "").upper(),
            str(item.get("platform") or "未知"),
        )
        lines.extend(
            [
                f"  {index}. {item.get('name') or item.get('key')}（{platform}）",
                f"     状态：{status}",
                f"     耗时：{float(item.get('elapsed_seconds') or 0):.3f} 秒",
            ]
        )
        if item.get("error"):
            lines.append(f"     错误：{item['error']}")
    lines.extend(
        [
            "",
            (
                "资源限制："
                f"OCR {int(summary.get('ocr_thread_limit') or 0)} 线程，"
                f"BLAS {int(summary.get('ocr_blas_thread_limit') or 0)} 线程"
            ),
            (
                "FLEET_SUMMARY "
                f"stores={int(summary.get('stores') or 0)} "
                f"success={int(summary.get('success') or 0)} "
                f"failed={int(summary.get('failed') or 0)} "
                f"elapsed_seconds={float(summary.get('elapsed_seconds') or 0):.3f}"
            ),
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect configured prescription stores sequentially"
    )
    parser.add_argument("--registry", required=True, type=pathlib.Path)
    parser.add_argument("--start", help="start date YYYY-MM-DD")
    parser.add_argument("--end", help="end date YYYY-MM-DD")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--run-user",
        default="",
        help="run each platform collector as this low-privilege account",
    )
    parser.add_argument(
        "--browser-timeout",
        type=float,
        default=45.0,
        help="seconds to wait for each on-demand browser to start or stop",
    )
    parser.add_argument("--skip-api-submit", action="store_true")
    parser.add_argument("--check-login", action="store_true")
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="also print the full machine-readable fleet summary JSON",
    )
    parser.add_argument(
        "--show-selection",
        action="store_true",
        help="print selected stores and exit without starting collection",
    )
    args = parser.parse_args()
    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be provided together")

    stores = load_enabled_stores(args.registry.expanduser().resolve())
    if args.show_selection:
        print(
            json.dumps(
                {
                    "stores": len(stores),
                    "selected_keys": [store["key"] for store in stores],
                    "selected_stores": [
                        {
                            "key": store["key"],
                            "name": store["name"],
                            "platform": store["platform"],
                        }
                        for store in stores
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    def request_shutdown(signum: int, _frame: Any) -> None:
        print(f"FLEET_STOP signal={signum}", flush=True)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    base = pathlib.Path(__file__).resolve().parent
    collector_map = {
        "MEITUAN": base / "server_meituan_prescription_collect.py",
        "JINGDONG": base / "server_jd_prescription_collect.py",
    }
    results: list[dict[str, Any]] = []
    batch_started = time.perf_counter()
    for store in stores:
        collector = collector_map[store["platform"]]
        if not collector.is_file():
            raise FileNotFoundError(
                f"collector not found for {store['platform']}: {collector}"
            )
        command = [sys.executable, str(collector), "--config", store["config"]]
        if args.run_user:
            command = [
                "runuser",
                "--preserve-environment",
                "-u",
                args.run_user,
                "--",
                *command,
            ]
        if args.start and args.end:
            command.extend(["--start", args.start, "--end", args.end])
        if args.limit is not None:
            command.extend(["--limit", str(max(0, args.limit))])
        if args.skip_api_submit:
            command.append("--skip-api-submit")
        if args.check_login:
            command.append("--check-login")
        started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        store_started = time.perf_counter()
        print(
            f"STORE_START key={store['key']} name={store['name']} "
            f"platform={store['platform']} "
            f"config={store['config']}",
            flush=True,
        )
        service = store["browser_service"]
        port = int(store["chrome_port"])
        exit_code = 1
        error = ""
        browser_started = False
        try:
            start_browser(service, port, args.browser_timeout)
            browser_started = True
            completed = subprocess.run(command, check=False)
            exit_code = int(completed.returncode)
        except (OSError, RuntimeError, TimeoutError) as exc:
            error = str(exc)
            print(f"STORE_ERROR key={store['key']} error={error}", flush=True)
        finally:
            try:
                stop_browser(service, port, args.browser_timeout)
            except (OSError, RuntimeError, TimeoutError) as exc:
                stop_error = str(exc)
                error = f"{error}; {stop_error}".strip("; ")
                exit_code = 1
                print(
                    f"BROWSER_STOP_ERROR service={service} error={stop_error}",
                    flush=True,
                )
        finished = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        results.append(
            {
                "key": store["key"],
                "name": store["name"],
                "platform": store["platform"],
                "config": store["config"],
                "started_at": started,
                "finished_at": finished,
                "elapsed_seconds": round(time.perf_counter() - store_started, 3),
                "browser_service": service,
                "browser_started": browser_started,
                "exit_code": exit_code,
                "error": error,
            }
        )
        print(
            f"STORE_END key={store['key']} exit_code={exit_code}",
            flush=True,
        )

    failed = [item for item in results if item["exit_code"] != 0]
    summary = {
        "stores": len(results),
        "success": len(results) - len(failed),
        "failed": len(failed),
        "elapsed_seconds": round(time.perf_counter() - batch_started, 3),
        "ocr_thread_limit": int(
            os.environ.get("PADDLE_PDX_CPU_NUM_THREADS") or 0
        ),
        "ocr_blas_thread_limit": int(os.environ.get("OMP_NUM_THREADS") or 0),
        "results": results,
    }
    print(format_fleet_summary(summary, check_login=args.check_login), flush=True)
    if args.json_summary:
        print(
            "FLEET_SUMMARY_JSON="
            + json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
