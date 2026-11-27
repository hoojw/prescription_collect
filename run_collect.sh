#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="1.6.1"
PROJECT_DIR="/opt/prescription-collector"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
FLEET_SCRIPT="$PROJECT_DIR/collect_store_fleet.py"
COLLECTION_LOCK_FILE="/run/prescription-collector/collection.lock"

REGISTRY_FILE="/etc/prescription-collector/stores.json"
COS_ENV_FILE="/etc/prescription-collector/cos.env"

RUN_USER="prescription-collector"
RUN_HOME="/var/lib/prescription-collector"

export PRESCRIPTION_OCR_THREADS=3
export PADDLE_PDX_CPU_NUM_THREADS="$PRESCRIPTION_OCR_THREADS"
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1

TEMP_REGISTRY=""
CHECK_CONFIG_ONLY=false

case "${1:-}" in
    --version|-V)
        echo "run_collect.sh v${SCRIPT_VERSION}"
        exit 0
        ;;
    --check-config)
        CHECK_CONFIG_ONLY=true
        ;;
    "")
        ;;
    *)
        echo "用法：$0 [--version|--check-config]" >&2
        exit 2
        ;;
esac

cleanup() {
    if [[ -n "${TEMP_REGISTRY:-}" && -f "$TEMP_REGISTRY" ]]; then
        rm -f "$TEMP_REGISTRY"
    fi
}

trap cleanup EXIT INT TERM

error_exit() {
    echo "错误：$*" >&2
    exit 1
}

validate_date() {
    local value="$1"

    "$PYTHON_BIN" - "$value" <<'PY' >/dev/null
import datetime
import sys

datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d")
PY
}

cd "$PROJECT_DIR"

echo "处方采集启动器 v${SCRIPT_VERSION}"

# --------------------------------------------------
# 基础检查
# --------------------------------------------------

if [[ "$(id -u)" -ne 0 ]]; then
    error_exit "请使用 root 用户运行该脚本。"
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
    error_exit "运行用户不存在：$RUN_USER"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
    error_exit "虚拟环境 Python 不存在或不可执行：$PYTHON_BIN"
fi

if [[ ! -r "$FLEET_SCRIPT" ]]; then
    error_exit "多店铺采集程序不可读取：$FLEET_SCRIPT"
fi

if [[ ! -r "$REGISTRY_FILE" ]]; then
    error_exit "店铺注册表不可读取：$REGISTRY_FILE"
fi

if [[ ! -d "$RUN_HOME" ]]; then
    error_exit "运行目录不存在：$RUN_HOME"
fi

# --------------------------------------------------
# 加载 COS 环境变量
# --------------------------------------------------

if [[ -r "$COS_ENV_FILE" ]]; then
    set -a

    # shellcheck disable=SC1090
    source "$COS_ENV_FILE"

    set +a
else
    echo "警告：无法读取 COS 环境文件：$COS_ENV_FILE"
    echo "如果选中店铺启用了 COS 上传，任务将会失败。"
fi

# --------------------------------------------------
# 读取并显示已启用店铺
# --------------------------------------------------

echo
echo "配置规则预检："

"$PYTHON_BIN" - "$REGISTRY_FILE" <<'PY'
import json
import pathlib
import sys

registry_path = pathlib.Path(sys.argv[1])
registry = json.loads(registry_path.read_text(encoding="utf-8"))
checked = 0

for store in registry.get("stores", []):
    if not isinstance(store, dict) or not bool(store.get("enabled", False)):
        continue
    key = str(store.get("key") or "").strip()
    name = str(store.get("name") or key).strip()
    config_path = pathlib.Path(str(store.get("config") or "")).expanduser()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    platform = str(config.get("platform") or "MEITUAN").upper()
    usage_mode = str(config.get("usage_ocr_preprocessing_mode") or "")

    if platform == "MEITUAN" and usage_mode != "blue_mask":
        raise SystemExit(
            f"{key}: 美团 usage_ocr_preprocessing_mode 必须为 blue_mask"
        )
    checked += 1
    print(f"  {name}: platform={platform} config={config_path}")

if checked == 0:
    raise SystemExit("注册表中没有已启用店铺")
PY

if [[ "$CHECK_CONFIG_ONLY" == true ]]; then
    echo "配置规则预检通过。"
    exit 0
fi

mapfile -t STORE_ROWS < <(
    "$PYTHON_BIN" - "$REGISTRY_FILE" <<'PY'
import json
import pathlib
import sys

registry_path = pathlib.Path(sys.argv[1])
registry = json.loads(registry_path.read_text(encoding="utf-8"))

for store in registry.get("stores", []):
    if not isinstance(store, dict):
        continue

    if not bool(store.get("enabled", False)):
        continue

    key = str(store.get("key") or "").strip()
    config_path = pathlib.Path(
        str(store.get("config") or "")
    ).expanduser()

    store_name = str(store.get("name") or key).strip()
    platform = ""

    try:
        config = json.loads(
            config_path.read_text(encoding="utf-8")
        )

        platform = str(
            config.get("platform") or "MEITUAN"
        ).upper()
    except Exception:
        pass

    print(
        "|".join(
            [
                key,
                store_name,
                platform,
                str(config_path),
            ]
        )
    )
PY
)

if [[ "${#STORE_ROWS[@]}" -eq 0 ]]; then
    error_exit "注册表中没有已启用店铺。"
fi

echo
echo "=========================================="
echo "请选择采集店铺"
echo "=========================================="
echo
echo "  1) 全部已启用店铺"

for index in "${!STORE_ROWS[@]}"; do
    IFS='|' read -r \
        store_key \
        store_name \
        platform \
        config_path \
        <<< "${STORE_ROWS[$index]}"

    option_number=$((index + 2))

    echo
    echo "  ${option_number}) ${store_name}"
    echo "     平台：${platform:-未知}"
    echo "     配置：${config_path}"
done

echo
MAX_OPTION=$((${#STORE_ROWS[@]} + 1))

read -r -p "请输入选项 [1-${MAX_OPTION}]: " STORE_CHOICE

if ! [[ "$STORE_CHOICE" =~ ^[0-9]+$ ]]; then
    error_exit "店铺选项必须是数字。"
fi

if (( STORE_CHOICE < 1 || STORE_CHOICE > MAX_OPTION )); then
    error_exit "店铺选项超出范围。"
fi

SELECTION_MODE=""
SELECTED_KEY=""

if (( STORE_CHOICE == 1 )); then
    SELECTION_MODE="all"
    echo
    echo "已选择：全部已启用店铺"
else
    STORE_INDEX=$((STORE_CHOICE - 2))

    IFS='|' read -r \
        SELECTED_KEY \
        SELECTED_NAME \
        SELECTED_PLATFORM \
        SELECTED_CONFIG \
        <<< "${STORE_ROWS[$STORE_INDEX]}"

    SELECTION_MODE="single"

    echo
    echo "已选择单店："
    echo "  店铺名称：$SELECTED_NAME"
    echo "  平台：${SELECTED_PLATFORM:-未知}"
    echo "  配置文件：$SELECTED_CONFIG"
fi

# --------------------------------------------------
# 创建临时注册表
# --------------------------------------------------

TEMP_REGISTRY="$(
    mktemp \
        "$RUN_HOME/stores-selection.XXXXXX.json"
)"

"$PYTHON_BIN" \
    - "$REGISTRY_FILE" "$TEMP_REGISTRY" \
    "$SELECTION_MODE" "$SELECTED_KEY" <<'PY'
import json
import os
import pathlib
import sys

source_path = pathlib.Path(sys.argv[1])
target_path = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
store_key = sys.argv[4]

payload = json.loads(
    source_path.read_text(encoding="utf-8")
)

if mode == "all":
    payload["selection"] = {
        "mode": "all"
    }
elif mode == "single":
    payload["selection"] = {
        "mode": "single",
        "store_key": store_key,
    }
else:
    raise ValueError(
        f"invalid selection mode: {mode}"
    )

target_path.write_text(
    json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)

os.chmod(target_path, 0o640)
PY

chown "root:$RUN_USER" "$TEMP_REGISTRY"
chmod 640 "$TEMP_REGISTRY"

# --------------------------------------------------
# 零采集预检店铺选择
# --------------------------------------------------

echo
echo "店铺选择预检："

runuser -u "$RUN_USER" -- \
    "$PYTHON_BIN" \
    "$FLEET_SCRIPT" \
    --registry "$TEMP_REGISTRY" \
    --show-selection

# --------------------------------------------------
# 输入日期和运行参数
# --------------------------------------------------

echo
read -r -p \
    "开始日期（MM-DD，直接回车使用各店配置默认范围）: " \
    START_DATE

read -r -p \
    "结束日期（MM-DD，直接回车使用各店配置默认范围）: " \
    END_DATE

CURRENT_YEAR=$(date +%Y)

if [[ -n "$START_DATE" ]]; then
    START_DATE="${CURRENT_YEAR}-${START_DATE}"
fi

if [[ -n "$END_DATE" ]]; then
    END_DATE="${CURRENT_YEAR}-${END_DATE}"
fi

if [[ -n "$START_DATE" || -n "$END_DATE" ]]; then
    if [[ -z "$START_DATE" || -z "$END_DATE" ]]; then
        error_exit "开始日期和结束日期必须同时填写。"
    fi

    if ! validate_date "$START_DATE"; then
        error_exit "开始日期格式错误：$START_DATE"
    fi

    if ! validate_date "$END_DATE"; then
        error_exit "结束日期格式错误：$END_DATE"
    fi

    if [[ "$END_DATE" < "$START_DATE" ]]; then
        error_exit "结束日期不能早于开始日期。"
    fi
fi

# --------------------------------------------------
# 拼接命令
# --------------------------------------------------

COMMAND=(
    "$PYTHON_BIN"
    "$FLEET_SCRIPT"
    "--registry"
    "$TEMP_REGISTRY"
    "--run-user"
    "$RUN_USER"
)

if [[ -n "$START_DATE" && -n "$END_DATE" ]]; then
    COMMAND+=(
        "--start"
        "$START_DATE"
        "--end"
        "$END_DATE"
    )
fi

# --------------------------------------------------
# 显示本次执行信息
# --------------------------------------------------

echo
echo "=========================================="
echo "本次执行信息"
echo "=========================================="

if [[ "$SELECTION_MODE" == "all" ]]; then
    echo "店铺范围：全部已启用店铺"
else
    echo "店铺范围：$SELECTED_NAME（$SELECTED_PLATFORM）"
fi

if [[ -n "$START_DATE" ]]; then
    echo "日期范围：$START_DATE 至 $END_DATE"
else
    echo "日期范围：使用各店配置中的 lookback_days"
fi

echo
echo "即将执行："
printf ' %q' "${COMMAND[@]}"
echo
echo

read -r -p "确认执行？[Y/n]: " CONFIRM

case "${CONFIRM,,}" in
    n|no)
        echo "已取消。"
        exit 0
        ;;
    y|yes|"")
        ;;
    *)
        error_exit "请输入 y、yes、n、no 或直接回车。"
        ;;
esac

# --------------------------------------------------
# 构造低权限运行环境
# --------------------------------------------------

ENVIRONMENT=(
    "HOME=$RUN_HOME"
    "PADDLE_PDX_MODEL_SOURCE=BOS"
)

COS_VARIABLES=(
    COS_BASE_URL
    COS_ACCESS_KEY
    COS_SECRET_KEY
    COS_REGION
    COS_BUCKET
    COS_FOLDER_PREFIX
)

for variable_name in "${COS_VARIABLES[@]}"; do
    if [[ -n "${!variable_name:-}" ]]; then
        ENVIRONMENT+=(
            "$variable_name=${!variable_name}"
        )
    fi
done

# --------------------------------------------------
# 执行采集
# --------------------------------------------------

echo
echo "开始执行多店铺采集……"
echo

mkdir -p "$(dirname "$COLLECTION_LOCK_FILE")"
exec 9>"$COLLECTION_LOCK_FILE"

if ! flock -n 9; then
    echo "已有采集任务正在运行，本次任务已跳过。"
    echo "COLLECTION_SKIPPED reason=busy lock=$COLLECTION_LOCK_FILE"
    exit 0
fi

echo "COLLECTION_LOCK_ACQUIRED lock=$COLLECTION_LOCK_FILE"
echo

if env \
    "${ENVIRONMENT[@]}" \
    "${COMMAND[@]}"
then
    echo
    echo "采集任务执行完成。"
    echo
    echo "本次店铺结果摘要："
    "$PYTHON_BIN" - "$TEMP_REGISTRY" <<'PY'
import json
import pathlib
import sys

registry = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
selection = registry.get("selection") or {"mode": "all"}
selected_key = str(selection.get("store_key") or "")

for store in registry.get("stores", []):
    if not isinstance(store, dict) or not bool(store.get("enabled", False)):
        continue
    key = str(store.get("key") or "").strip()
    if selection.get("mode") == "single" and key != selected_key:
        continue
    config_path = pathlib.Path(str(store.get("config") or "")).expanduser()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    summary_path = pathlib.Path(str(config.get("output_dir") or "")) / "last_run.json"
    if not summary_path.is_file():
        print(f"  {key}: 未找到 last_run.json")
        continue
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(
        f"  {key}: status={summary.get('status')} "
        f"new={summary.get('new_prescriptions', 0)} "
        f"multi_order_skipped={summary.get('multi_prescription_orders_skipped', 0)} "
        f"multi_order_queued={summary.get('multi_prescription_orders_queued', 0)} "
        f"ocr_images={summary.get('ocr_images', 0)} "
        f"ocr_load_s={summary.get('ocr_model_load_seconds', 0)} "
        f"ocr_warmup_s={summary.get('ocr_warmup_seconds', 0)} "
        f"ocr_inference_s={summary.get('ocr_inference_seconds', 0)} "
        f"ocr_avg_s={summary.get('ocr_average_seconds', 0)} "
        f"run_elapsed_s={summary.get('run_elapsed_seconds', 0)} "
        f"cos_success={summary.get('cos_success', 0)} "
        f"need_review={summary.get('need_review_records', 0)} "
        f"upload_records={summary.get('upload_json_records', 0)} "
        f"api_status={summary.get('api_status')}"
    )
PY
else
    exit_code=$?

    echo
    echo "采集任务执行失败，退出码：$exit_code" >&2

    exit "$exit_code"
fi
