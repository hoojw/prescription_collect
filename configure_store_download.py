#!/usr/bin/env python3
"""Set a store Chrome profile's default download directory safely."""

from __future__ import annotations

import argparse
import json
import os
import pathlib


PROFILE_ROOT = pathlib.Path("/opt/meituan-chrome-profiles")
DOWNLOAD_ROOT = pathlib.Path("/var/lib/prescription-collector/catalog-inbox")


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def configure(profile_dir: pathlib.Path, download_dir: pathlib.Path) -> pathlib.Path:
    profile = profile_dir.expanduser().resolve()
    download = download_dir.expanduser().resolve()
    if not _inside(profile, PROFILE_ROOT):
        raise ValueError(f"profile must be below {PROFILE_ROOT}")
    if not _inside(download, DOWNLOAD_ROOT):
        raise ValueError(f"download directory must be below {DOWNLOAD_ROOT}")
    download.mkdir(parents=True, exist_ok=True)
    download.chmod(0o700)
    default_dir = profile / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    preferences = default_dir / "Preferences"
    payload = {}
    if preferences.exists():
        loaded = json.loads(preferences.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    settings = payload.setdefault("download", {})
    if not isinstance(settings, dict):
        settings = {}
        payload["download"] = settings
    settings.update(
        {
            "default_directory": str(download),
            "directory_upgrade": True,
            "prompt_for_download": False,
        }
    )
    temp = preferences.with_suffix(".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temp.chmod(0o600)
    os.replace(temp, preferences)
    preferences.chmod(0o600)
    return preferences


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-dir", required=True, type=pathlib.Path)
    parser.add_argument("--download-dir", required=True, type=pathlib.Path)
    args = parser.parse_args()
    preferences = configure(args.profile_dir, args.download_dir)
    print(
        json.dumps(
            {
                "profile_dir": str(args.profile_dir.resolve()),
                "download_dir": str(args.download_dir.resolve()),
                "preferences": str(preferences),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
