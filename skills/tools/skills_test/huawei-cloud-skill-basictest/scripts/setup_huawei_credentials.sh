#!/usr/bin/env bash
# =============================================================================
# setup_huawei_credentials.sh — 华为云 AK/SK 统一管理分发脚本
#
# 功能:
#   将 AK/SK 从中心配置文件 (~/.huawei-credentials) 统一分发到全部使用位置:
#     1. hcloud CLI profile（--cli-auth-encrypt false 明文存储）
#     2. ~/.obsutilconfig（OBS 上传用）
#     3. ~/.bashrc（环境变量 HUAWEI_ACCESS_KEY 等，供评测 agent 运行时读取）
#   并做连通性验证（RDS + OBS）。
#
# 用法:
#   bash setup_huawei_credentials.sh              # 分发 + 验证
#   bash setup_huawei_credentials.sh --dry-run    # 只打印将执行的动作
#   bash setup_huawei_credentials.sh --init       # 创建/更新中心配置文件（交互式）
#
# 中心配置文件 ~/.huawei-credentials 格式:
#   AK=<access_key>
#   SK=<secret_key>
#   REGION=<region>
#   ENDPOINT=<obs_endpoint>
#
# 安全: 中心配置 0600 权限，密钥不回显，不入库
# =============================================================================
set -uo pipefail

CRED_FILE="${HUAWEI_CRED_FILE:-$HOME/.huawei-credentials}"
BASH_RC="$HOME/.bashrc"
DRY_RUN=0
INIT=0

for a in "$@"; do [[ "${a}" == "-h" || "${a}" == "--help" ]] && { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }; done
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --init)    INIT=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

log()  { echo -e "$@"; }
log_ok(){ echo -e "  \033[32m✅ $*\033[0m"; }
log_err(){ echo -e "  \033[31m❌ $*\033[0m" >&2; }

# --- 读取中心配置 ---
if [[ ! -f "$CRED_FILE" || "$INIT" -eq 1 ]]; then
  echo "==> 创建/更新中心凭证配置 $CRED_FILE ..."
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "  [dry-run] 交互式创建 $CRED_FILE"
  else
    read -r -p "  AK: " AK_IN
    read -r -s -p "  SK: " SK_IN; echo ""
    read -r -p "  Region [cn-north-4]: " REGION_IN
    REGION_IN="${REGION_IN:-cn-north-4}"
    umask 177
    cat > "$CRED_FILE" << EOF
AK=${AK_IN}
SK=${SK_IN}
REGION=${REGION_IN}
ENDPOINT=https://obs.${REGION_IN}.myhuaweicloud.com
EOF
    chmod 600 "$CRED_FILE"
    log_ok "中心配置已写入 $CRED_FILE"
  fi
fi

# --- 读取中心配置值 ---
if [[ -f "$CRED_FILE" ]]; then
  AK=$(grep -E "^AK=" "$CRED_FILE" | head -1 | cut -d= -f2)
  SK=$(grep -E "^SK=" "$CRED_FILE" | head -1 | cut -d= -f2)
  REGION=$(grep -E "^REGION=" "$CRED_FILE" | head -1 | cut -d= -f2)
  ENDPOINT=$(grep -E "^ENDPOINT=" "$CRED_FILE" | head -1 | cut -d= -f2)
  REGION="${REGION:-cn-north-4}"
  ENDPOINT="${ENDPOINT:-https://obs.${REGION}.myhuaweicloud.com}"
else
  log_err "未找到中心凭证配置 $CRED_FILE，请先运行 --init 创建"
  exit 1
fi

if [[ -z "$AK" || -z "$SK" ]]; then
  log_err "中心配置 $CRED_FILE 缺少 AK/SK，请运行 --init 更新"
  exit 1
fi
log "中心凭证源: $CRED_FILE (AK=${AK:0:4}***, region=$REGION)"
echo ""

# --- 1. 分发到 hcloud profile（明文存储）---
echo "==> 1. 配置 hcloud CLI profile..."
HCLOUD_BIN="${HCLOUD:-hcloud}"
if command -v "$HCLOUD_BIN" >/dev/null 2>&1 || [[ -x "$HOME/.local/hcloud/hcloud" ]]; then
  if ! command -v "$HCLOUD_BIN" >/dev/null 2>&1 && [[ -x "$HOME/.local/hcloud/hcloud" ]]; then
    HCLOUD_BIN="$HOME/.local/hcloud/hcloud"
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "  [dry-run] $HCLOUD_BIN configure set --cli-access-key=$AK --cli-secret-key=*** --cli-auth-encrypt false"
  else
    "$HCLOUD_BIN" configure set --cli-access-key="$AK" --cli-secret-key="$SK" --cli-auth-encrypt=false >/dev/null 2>&1 \
      && log_ok "hcloud profile 已配置（明文存储）" \
      || log_err "hcloud 配置失败"
  fi
else
  log_err "未找到 hcloud CLI，跳过（可稍后手动配置）"
fi

# --- 2. 分发到 obsutilconfig ---
echo "==> 2. 配置 ~/.obsutilconfig..."
if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "  [dry-run] 写入 ~/.obsutilconfig (ak/sk/endpoint)"
else
  OBS_CFG="$HOME/.obsutilconfig"
  cp "$OBS_CFG" "$OBS_CFG.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null
  # 保留原有配置项，仅更新 ak/sk/endpoint
  if grep -q "^ak=" "$OBS_CFG" 2>/dev/null; then
    sed -i "s|^ak=.*|ak=$AK|; s|^sk=.*|sk=$SK|; s|^endpoint=.*|endpoint=$ENDPOINT|" "$OBS_CFG"
  else
    # 追加（配置不存在时）
    printf "endpoint=%s\nak=%s\nsk=%s\n" "$ENDPOINT" "$AK" "$SK" >> "$OBS_CFG"
  fi
  chmod 600 "$OBS_CFG"
  log_ok "~/.obsutilconfig 已更新（ak/sk/endpoint）"
fi

# --- 3. 分发到 shell 环境（~/.bashrc）---
echo "==> 3. 配置 ~/.bashrc 环境变量..."
if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "  [dry-run] 追加 export HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY / HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK 到 ~/.bashrc"
else
  # 先移除旧的 HUAWEI 相关 export（避免重复）
  sed -i '/^export HUAWEI_ACCESS_KEY=/d; /^export HUAWEI_SECRET_KEY=/d; /^export HUAWEICLOUD_SDK_AK=/d; /^export HUAWEICLOUD_SDK_SK=/d' "$BASH_RC" 2>/dev/null
  cat >> "$BASH_RC" << EOF
# Huawei Cloud credentials (managed by setup_huawei_credentials.sh)
export HUAWEI_ACCESS_KEY=$AK
export HUAWEI_SECRET_KEY=$SK
export HUAWEICLOUD_SDK_AK=$AK
export HUAWEICLOUD_SDK_SK=$SK
EOF
  log_ok "~/.bashrc 已追加 4 个环境变量（已清理旧值）"
fi

echo ""

# --- 4. 连通性验证 ---
echo "==> 4. 连通性验证..."
if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "  [dry-run] 验证: hcloud RDS ListInstances + hcloud obs cp"
  echo "==> dry-run 完成（未实际分发）"
  exit 0
fi

# 4.1 RDS 验证
if command -v "$HCLOUD_BIN" >/dev/null 2>&1 || [[ -x "$HOME/.local/hcloud/hcloud" ]]; then
  [[ ! -x "$HOME/.local/hcloud/hcloud" ]] || HCLOUD_BIN="$HOME/.local/hcloud/hcloud"
  RDS_TEST=$("$HCLOUD_BIN" RDS ListInstances --cli-region="$REGION" --limit=1 2>&1 | head -3)
  if echo "$RDS_TEST" | grep -qiE "error|unauthorized|failed"; then
    log_err "RDS 验证失败: $(echo "$RDS_TEST" | head -1)"
  else
    log_ok "RDS 连通性 OK"
  fi
fi

# 4.2 OBS 验证（上传测试文件）
OBS_TEST_FILE="/tmp/cred_verify_$$.txt"
echo "cred verify" > "$OBS_TEST_FILE"
if command -v "$HCLOUD_BIN" >/dev/null 2>&1 || [[ -x "$HOME/.local/hcloud/hcloud" ]]; then
  OBS_TEST=$("$HCLOUD_BIN" obs cp "$OBS_TEST_FILE" "obs://obs-hd-dev-static/testReport/.cred_verify.txt" 2>&1 | tail -2)
  if echo "$OBS_TEST" | grep -qiE "successfully|status \[200\]"; then
    log_ok "OBS 连通性 OK"
  else
    log_err "OBS 验证失败: $(echo "$OBS_TEST" | head -1)"
  fi
fi
rm -f "$OBS_TEST_FILE"

echo ""
log_ok "AK/SK 统一管理完成。中心配置: $CRED_FILE"
log "  提示: 当前 shell 需执行 'source ~/.bashrc' 使环境变量立即生效"
