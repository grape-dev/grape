#!/bin/bash
# upload_report_obs.sh — 上传测试报告到 OBS 桶并生成回源下载 URL
#
# 桶: obs-hd-dev-static，路径: testReport/
# 回源域名: http://skills.topxtopx.com/（私有桶回源访问）
#
# 用法:
#   bash upload_report_obs.sh <本地报告文件> [--skill <skill名>] [--date <YYYY-MM-DD>] [--region <region>]
#
# 环境变量（统一读取，不硬编码）:
#   TEST_REPORT_OBS_AK / TEST_REPORT_OBS_SK （报告 OBS 专用凭证，优先）
#   HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY （兜底）
#   HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK （最后兜底）
#   OBS_REGION (可选，默认 cn-north-4)
#
# 流程: MD 报告 → 上传 OBS（直接上传 .md，不转 PDF）
# 输出:
#   1. 上传成功后的 OBS 对象路径
#   2. 回源下载 URL: http://skills.topxtopx.com/testReport/<文件名.md>
# 退出码: 0 成功; 1 参数错误; 2 凭证缺失; 3 转换/上传失败

set -uo pipefail

HCLOUD="${HCLOUD:-hcloud}"
BUCKET="obs-hd-dev-static"
OBS_PREFIX="testReport"
ORIGIN_DOMAIN="http://skills.topxtopx.com"
REGION="${OBS_REGION:-cn-north-4}"
ENDPOINT="https://obs.$REGION.myhuaweicloud.com"

# --- 参数解析 ---
FILE=""
SKILL_NAME=""
DATE_STR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skill) SKILL_NAME="$2"; shift 2 ;;
        --date) DATE_STR="$2"; shift 2 ;;
        --region) REGION="$2"; ENDPOINT="https://obs.$REGION.myhuaweicloud.com"; shift 2 ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) FILE="$1"; shift ;;
    esac
done

if [[ -z "$FILE" || ! -f "$FILE" ]]; then
    echo "错误: 需要指定存在的本地报告文件" >&2
    exit 1
fi

# --- 读取 AK/SK（报告 OBS 专用凭证优先，避免误用无权限的 SDK 凭证）---
AK="${TEST_REPORT_OBS_AK:-${HUAWEI_ACCESS_KEY:-${HUAWEICLOUD_SDK_AK:-}}}"
SK="${TEST_REPORT_OBS_SK:-${HUAWEI_SECRET_KEY:-${HUAWEICLOUD_SDK_SK:-}}}"
if [[ -z "$AK" || -z "$SK" ]]; then
    echo "错误: 缺少 AK/SK（设置 TEST_REPORT_OBS_AK/SK 或 HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY）" >&2
    exit 2
fi

# --- 检查 hcloud ---
if ! command -v "$HCLOUD" >/dev/null 2>&1; then
    if [[ -x "$HOME/.local/hcloud/hcloud" ]]; then
        HCLOUD="$HOME/.local/hcloud/hcloud"
    else
        echo "错误: 未找到 hcloud CLI" >&2
        exit 3
    fi
fi

BASENAME=$(basename "$FILE")
UPLOAD_FILE="$FILE"
EXT=".md"
if [[ -n "$SKILL_NAME" && -n "$DATE_STR" ]]; then
    OBJ_NAME="${SKILL_NAME}_${DATE_STR}_总测试报告${EXT}"
elif [[ -n "$SKILL_NAME" || -n "$DATE_STR" ]]; then
    echo "错误: --skill 与 --date 必须同时提供，否则对象名无法唯一确定" >&2
    exit 1
else
    OBJ_NAME="${BASENAME%.md}${EXT}"
fi
OBJ_PATH="${OBS_PREFIX}/${OBJ_NAME}"

echo "=== 上传到 OBS ==="
echo "  源文件: $UPLOAD_FILE"
echo "  目标:   $BUCKET/$OBJ_PATH"

T0=$(date +%s%N)
UP=$(timeout 60 "$HCLOUD" obs cp "$UPLOAD_FILE" "obs://$BUCKET/$OBJ_PATH" \
    -i "$AK" -k "$SK" -e "$ENDPOINT" 2>&1)
UP_RC=$?
T1=$(date +%s%N)
printf '%s|%s|%s|%s|%s|%s\n' "$(date +%Y-%m-%dT%H:%M:%S.%3N)" "${SKILL_NAME:-upload}" "upload_report" "obs_upload" "$([ $UP_RC -eq 0 ] && echo OK || echo FAIL)" "$(( (T1 - T0) / 1000000 ))" >> "${EVAL_TIMING_LOG:-/tmp/eval_timing.log}" 2>/dev/null || true

if [[ $UP_RC -ne 0 ]]; then
    echo "错误: 上传失败 (exit $UP_RC)" >&2
    echo "$UP" >&2
    exit 3
fi

if ! echo "$UP" | grep -qiE "Upload successfully|status \[200\]"; then
    echo "错误: 上传未确认成功" >&2
    echo "$UP" >&2
    exit 3
fi

# --- 回源下载 URL ---
URL="${ORIGIN_DOMAIN}/${OBJ_PATH}"
echo ""
echo "✅ 上传成功"
echo "  OBS 对象: obs://$BUCKET/$OBJ_PATH"
echo "  回源下载: $URL"

# 验证可下载
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL" 2>&1)
if [[ "$HTTP_CODE" == "200" ]]; then
    echo "  可访问性: ✅ HTTP 200"
else
    echo "  可访问性: ⚠️ HTTP $HTTP_CODE（回源域名可能未生效）" >&2
fi

echo "DOWNLOAD_URL=$URL"
exit 0
