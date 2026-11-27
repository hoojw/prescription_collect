#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="${PRESCRIPTION_COLLECTION_LOCK_FILE:-/run/prescription-collector/collection.lock}"

if [[ "${1:-}" == "--" ]]; then
    shift
fi

if [[ "$#" -eq 0 ]]; then
    echo "用法：$0 -- <采集命令> [参数...]" >&2
    exit 2
fi

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "COLLECTION_SKIPPED reason=busy lock=$LOCK_FILE"
    echo "已有采集任务正在运行，本次任务已跳过。"
    exit 0
fi

echo "COLLECTION_LOCK_ACQUIRED lock=$LOCK_FILE"
exec "$@"
