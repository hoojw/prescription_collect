#!/usr/bin/env bash
# Reconcile the current day and the two preceding days after the last daily run.
# Existing orders are skipped by SQLite; only late-arriving eligible orders are processed.
set -euo pipefail

today="$(date +%F)"
start="$(date -d "$today - 2 days" +%F)"

exec /opt/prescription-collector/run_collection_if_idle.sh -- \
  /opt/prescription-collector/.venv/bin/python \
  /opt/prescription-collector/collect_store_fleet.py \
  --registry /etc/prescription-collector/stores/mt-34024037.registry.json \
  --run-user prescription-collector \
  --start "$start" \
  --end "$today"
