#!/usr/bin/env python3
"""Create an isolated login-first store instance and a disabled draft config."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
from typing import Any


PROJECT_DIR = pathlib.Path("/opt/prescription-collector")
CONFIG_DIR = pathlib.Path("/etc/prescription-collector")
STATE_DIR = pathlib.Path("/var/lib/prescription-collector/onboarding")
INSTANCE_REGISTRY = pathlib.Path("/etc/store-instances/instances.json")
PLATFORMS = {
    "MEITUAN": {
        "slug": "mt",
        "port_range": range(9350, 9400),
        "profile_root": pathlib.Path("/opt/meituan-chrome-profiles"),
        "login_service": "meituan-store-login@{instance}.service",
        "browser_service": "meituan-store-chrome@{instance}.service",
        "collector": "server_meituan_prescription_collect.py",
        "template": "new-organization-meituan.json",
        "identity_field": "current_poi_id",
        "expected_field": "expected_poi_id",
    },
    "JINGDONG": {
        "slug": "jd",
        "port_range": range(9450, 9500),
        "profile_root": pathlib.Path("/opt/jingdong-chrome-profiles"),
        "login_service": "jingdong-store-login@{instance}.service",
        "browser_service": "jingdong-store-chrome@{instance}.service",
        "collector": "server_jd_prescription_collect.py",
        "template": "new-organization-jingdong.json",
        "identity_field": "current_vender_id",
        "expected_field": "expected_vender_id",
    },
}
INSTANCE_RE = re.compile(r"(?:store-[0-9]+|onboard-(?:mt|jd)-[0-9]{14}(?:-[0-9]+)?)")


def atomic_json(path: pathlib.Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)


def normalize_platform(value: str) -> str:
    platform = value.strip().upper()
    aliases = {"美团": "MEITUAN", "京东": "JINGDONG", "JD": "JINGDONG"}
    platform = aliases.get(platform, platform)
    if platform not in PLATFORMS:
        raise ValueError("platform must be MEITUAN/美团 or JINGDONG/京东")
    return platform


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocated_ports(browser_dir: pathlib.Path) -> set[int]:
    ports: set[int] = set()
    if not browser_dir.is_dir():
        return ports
    for path in browser_dir.glob("*.env"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CHROME_PORT="):
                try:
                    ports.add(int(line.partition("=")[2].strip()))
                except ValueError:
                    pass
    return ports


def choose_port(platform: str, browser_dir: pathlib.Path) -> int:
    used = allocated_ports(browser_dir)
    for port in PLATFORMS[platform]["port_range"]:
        if port not in used and port_is_free(port):
            return port
    raise RuntimeError(f"no free onboarding Chrome port for {platform}")


def choose_meituan_instance(
    browser_dir: pathlib.Path, profile_root: pathlib.Path
) -> str:
    used: set[int] = set()
    for path in list(browser_dir.glob("store-*.env")) + list(profile_root.glob("store-*")):
        match = re.fullmatch(r"store-([0-9]+)(?:\.env)?", path.name)
        if match:
            used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return f"store-{number}"


def replace_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: replace_placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    return value


def build_draft_config(
    platform: str,
    identity: dict[str, Any],
    chrome_port: int,
    template: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    settings = PLATFORMS[platform]
    identity_value = str(identity.get(settings["identity_field"]) or "").strip()
    if not identity_value:
        raise ValueError(f"discovery result is missing {settings['identity_field']}")
    store_key = f"{settings['slug']}-{identity_value}"
    replacements = {
        "REPLACE_MEITUAN_POI_ID": identity_value,
        "REPLACE_JD_VENDER_ID": identity_value,
        "REPLACE_JD_EXACT_SHOP_NAME": str(identity.get("current_shop_name") or "").strip(),
    }
    config = replace_placeholders(template, replacements)
    config["chrome_port"] = chrome_port
    config["platform"] = platform
    config["cos_upload_enabled"] = False
    config["api_submit_enabled"] = False
    return store_key, config


def add_disabled_registry_entry(
    registry: dict[str, Any],
    *,
    store_key: str,
    platform: str,
    name: str,
    config_path: pathlib.Path,
    browser_service: str,
) -> dict[str, Any]:
    stores = registry.setdefault("stores", [])
    if not isinstance(stores, list):
        raise ValueError("store registry stores must be a list")
    if any(str(item.get("key") or "") == store_key for item in stores if isinstance(item, dict)):
        raise ValueError(f"store already exists in registry: {store_key}")
    stores.append(
        {
            "key": store_key,
            "name": name or store_key,
            "platform": platform,
            "enabled": False,
            "browser_service": browser_service,
            "config": str(config_path),
        }
    )
    return registry


def add_discovered_instance(
    registry: dict[str, Any],
    *,
    store_key: str,
    platform: str,
    identity: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    instances = registry.setdefault("instances", [])
    if not isinstance(instances, list):
        raise ValueError("store instance registry instances must be a list")
    if any(
        str(item.get("key") or "") == store_key
        for item in instances
        if isinstance(item, dict)
    ):
        raise ValueError(f"store instance already exists: {store_key}")
    settings = PLATFORMS[platform]
    instances.append(
        {
            "key": store_key,
            "name": str(identity.get("current_shop_name") or store_key).strip(),
            "platform": platform,
            "identity": identity,
            "instance": state["instance"],
            "chrome_port": int(state["chrome_port"]),
            "chrome_profile": state["chrome_profile"],
            "browser_service": settings["browser_service"].format(
                instance=state["instance"]
            ),
            "confirmed": False,
            "capabilities": [],
        }
    )
    return registry


def prepare(platform: str) -> int:
    if os.geteuid() != 0:
        raise PermissionError("prepare must run as root")
    settings = PLATFORMS[platform]
    browser_dir = CONFIG_DIR / "browsers"
    port = choose_port(platform, browser_dir)
    if platform == "MEITUAN":
        instance = choose_meituan_instance(browser_dir, settings["profile_root"])
    else:
        timestamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        instance = f"onboard-{settings['slug']}-{timestamp}"
        suffix = 1
        while (STATE_DIR / f"{instance}.json").exists():
            instance = f"onboard-{settings['slug']}-{timestamp}-{suffix}"
            suffix += 1
    profile = settings["profile_root"] / instance
    env_path = browser_dir / f"{instance}.env"
    profile.mkdir(parents=True, mode=0o700)
    browser_dir.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        f"CHROME_PORT={port}\nCHROME_PROFILE={profile}\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    state = {
        "schema_version": 1,
        "instance": instance,
        "platform": platform,
        "status": "awaiting_login",
        "chrome_port": port,
        "chrome_profile": str(profile),
        "browser_env": str(env_path),
        "login_service": settings["login_service"].format(instance=instance),
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_json(STATE_DIR / f"{instance}.json", state)
    subprocess.run(["systemctl", "start", state["login_service"]], check=True)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def discover(instance: str) -> int:
    if os.geteuid() != 0:
        raise PermissionError("discover must run as root")
    if not INSTANCE_RE.fullmatch(instance):
        raise ValueError("invalid onboarding instance")
    state_path = STATE_DIR / f"{instance}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    platform = normalize_platform(str(state.get("platform") or ""))
    settings = PLATFORMS[platform]
    temporary_config = {
        "chrome_port": int(state["chrome_port"]),
        "expected_poi_id": "",
        "expected_vender_id": "",
        "expected_shop_name": "",
    }
    temp_path = pathlib.Path("/run/prescription-collector") / f"{instance}.json"
    atomic_json(temp_path, temporary_config)
    command = [
        str(PROJECT_DIR / ".venv/bin/python"),
        str(PROJECT_DIR / settings["collector"]),
        "--config",
        str(temp_path),
        "--discover-store",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, encoding="utf-8")
    lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "store discovery failed")
    identity = json.loads(lines[-1])
    state["discovery"] = identity
    state["status"] = "identity_discovered" if completed.returncode == 0 else "awaiting_identity"
    atomic_json(state_path, state)
    if completed.returncode != 0:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 2

    identity_value = str(identity.get(settings["identity_field"]) or "").strip()
    store_key = f"{settings['slug']}-{identity_value}"
    registry = (
        json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
        if INSTANCE_REGISTRY.is_file()
        else {"schema_version": 1, "instances": []}
    )
    add_discovered_instance(
        registry,
        store_key=store_key,
        platform=platform,
        identity=identity,
        state=state,
    )
    atomic_json(INSTANCE_REGISTRY, registry)
    state.update(
        {
            "status": "identity_discovered_unconfirmed",
            "store_key": store_key,
            "instance_registry": str(INSTANCE_REGISTRY),
        }
    )
    atomic_json(state_path, state)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def provision_prescription(instance: str) -> int:
    if os.geteuid() != 0:
        raise PermissionError("provision-prescription must run as root")
    if not INSTANCE_RE.fullmatch(instance):
        raise ValueError("invalid onboarding instance")
    state_path = STATE_DIR / f"{instance}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    identity = state.get("discovery") or {}
    platform = normalize_platform(str(state.get("platform") or ""))
    settings = PLATFORMS[platform]
    template_path = PROJECT_DIR / "deploy/linux/onboarding" / settings["template"]
    template = json.loads(template_path.read_text(encoding="utf-8"))
    store_key, config = build_draft_config(
        platform, identity, int(state["chrome_port"]), template
    )
    config_path = CONFIG_DIR / "stores" / f"{store_key}.json"
    if config_path.exists():
        raise FileExistsError(f"refusing to overwrite existing store config: {config_path}")
    registry_path = CONFIG_DIR / "stores.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    display_name = str(identity.get("current_shop_name") or store_key).strip()
    browser_service = settings["browser_service"].format(instance=instance)
    add_disabled_registry_entry(
        registry,
        store_key=store_key,
        platform=platform,
        name=display_name,
        config_path=config_path,
        browser_service=browser_service,
    )
    atomic_json(config_path, config)
    atomic_json(registry_path, registry)
    state.update(
        {
            "status": "prescription_draft_created_disabled",
            "store_config": str(config_path),
            "browser_service": browser_service,
        }
    )
    atomic_json(state_path, state)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="create and open a visible login instance")
    prepare_parser.add_argument("--platform", required=True)
    discover_parser = subparsers.add_parser("discover", help="discover and register identity without enabling a collector")
    discover_parser.add_argument("--instance", required=True)
    prescription_parser = subparsers.add_parser(
        "provision-prescription",
        help="explicitly add a disabled prescription-collector draft",
    )
    prescription_parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            return prepare(normalize_platform(args.platform))
        if args.command == "discover":
            return discover(args.instance)
        return provision_prescription(args.instance)
    except Exception as exc:
        print(f"onboarding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
