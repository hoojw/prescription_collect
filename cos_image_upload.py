#!/usr/bin/env python3
"""Upload prescription images to Tencent COS without embedding credentials.

Credentials and bucket settings are read from environment variables. The command
prints only the resulting object metadata and never prints access credentials.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import quote


SAFE_ID_RE = re.compile(r"[^0-9A-Za-z._-]+")
BUSINESS_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?")


@dataclass(frozen=True)
class CosSettings:
    base_url: str
    access_key: str
    secret_key: str
    region: str
    bucket: str
    folder_prefix: str = "/files"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "CosSettings":
        values = os.environ if env is None else env
        settings = cls(
            base_url=str(values.get("COS_BASE_URL") or "").strip(),
            access_key=str(values.get("COS_ACCESS_KEY") or values.get("COS_SECRET_ID") or "").strip(),
            secret_key=str(values.get("COS_SECRET_KEY") or "").strip(),
            region=str(values.get("COS_REGION") or "").strip(),
            bucket=str(values.get("COS_BUCKET") or "").strip(),
            folder_prefix=str(values.get("COS_FOLDER_PREFIX") or "/files").strip(),
        )
        missing = [
            name
            for name, value in (
                ("COS_BASE_URL", settings.base_url),
                ("COS_ACCESS_KEY", settings.access_key),
                ("COS_SECRET_KEY", settings.secret_key),
                ("COS_REGION", settings.region),
                ("COS_BUCKET", settings.bucket),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing COS environment variables: {', '.join(missing)}")
        if not settings.base_url.lower().startswith("https://"):
            raise ValueError("COS_BASE_URL must use https://")
        return settings


def safe_object_name(prescription_id: str, image_path: pathlib.Path, image_index: int = 0) -> str:
    cleaned = SAFE_ID_RE.sub("_", str(prescription_id).strip()).strip("._-")
    if not cleaned:
        raise ValueError("prescription_id does not contain a safe object name")
    suffix = image_path.suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in {".png", ".jpg", ".webp"}:
        suffix = ".png"
    index_suffix = "" if int(image_index) == 0 else f"_{int(image_index)}"
    return f"{cleaned}{index_suffix}{suffix}"


def build_object_key(folder_prefix: str, object_name: str) -> str:
    folder = str(folder_prefix or "").strip().strip("/")
    name = str(object_name).lstrip("/")
    return f"{folder}/{name}" if folder else name


def normalize_business_date(value: Any) -> str:
    """Return an ISO date parsed from a source prescription timestamp."""
    match = BUSINESS_DATE_RE.search(str(value or "").strip())
    if not match:
        raise ValueError(f"business date is missing or unsupported: {value!r}")
    try:
        return dt.date(*(int(part) for part in match.groups())).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid business date: {value!r}") from exc


def build_scoped_folder_prefix(
    folder_prefix: str,
    platform: str,
    store_id: str,
    business_date: Any,
) -> str:
    """Build /root/platform/store/date without accepting unsafe path segments."""
    platform_segment = SAFE_ID_RE.sub("-", str(platform or "").strip().lower()).strip("._-")
    store_segment = SAFE_ID_RE.sub("-", str(store_id or "").strip()).strip("._-")
    if not platform_segment:
        raise ValueError("platform does not contain a safe COS path segment")
    if not store_segment:
        raise ValueError("store_id does not contain a safe COS path segment")
    root = str(folder_prefix or "/files").strip().strip("/")
    date_segment = normalize_business_date(business_date)
    parts = [part for part in (root, platform_segment, store_segment, date_segment) if part]
    return "/".join(parts)


def build_public_url(base_url: str, object_key: str) -> str:
    return f"{str(base_url).rstrip('/')}/{quote(object_key.lstrip('/'), safe='/')}"


def create_cos_client(settings: Optional[CosSettings] = None) -> tuple[CosSettings, Any]:
    cfg = settings or CosSettings.from_env()
    from qcloud_cos import CosConfig, CosS3Client

    cos_config = CosConfig(
        Region=cfg.region,
        SecretId=cfg.access_key,
        SecretKey=cfg.secret_key,
        Scheme="https",
    )
    return cfg, CosS3Client(cos_config)


def upload_image(
    image_path: pathlib.Path,
    prescription_id: str,
    image_index: int = 0,
    settings: Optional[CosSettings] = None,
    client: Any = None,
    platform: Optional[str] = None,
    store_id: Optional[str] = None,
    business_date: Any = None,
) -> dict[str, Any]:
    path = pathlib.Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    cfg = settings or CosSettings.from_env()
    object_name = safe_object_name(prescription_id, path, image_index)
    scope_values = (platform, store_id, business_date)
    if any(value not in (None, "") for value in scope_values):
        if any(value in (None, "") for value in scope_values):
            raise ValueError(
                "platform, store_id and business_date are all required for scoped COS upload"
            )
        folder_prefix = build_scoped_folder_prefix(
            cfg.folder_prefix, str(platform), str(store_id), business_date
        )
    else:
        folder_prefix = cfg.folder_prefix
    object_key = build_object_key(folder_prefix, object_name)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    if client is None:
        cfg, client = create_cos_client(cfg)

    with path.open("rb") as body:
        response = client.put_object(
            Bucket=cfg.bucket,
            Body=body,
            Key=object_key,
            ContentType=content_type,
        )
    client.head_object(Bucket=cfg.bucket, Key=object_key)
    return {
        "object_key": object_key,
        "image_file": object_name,
        "image_file_url": build_public_url(cfg.base_url, object_key),
        "content_type": content_type,
        "etag": str((response or {}).get("ETag") or "").strip('"'),
        "size": path.stat().st_size,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload one prescription image to Tencent COS")
    parser.add_argument("--image", required=True, type=pathlib.Path)
    parser.add_argument("--prescription-id", required=True)
    parser.add_argument("--image-index", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = upload_image(args.image, args.prescription_id, args.image_index)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
