#!/usr/bin/env bash
# Grape Worker 绿色一键安装脚本（普通用户免 root 接入）
#
# 用法（凭据申请页生成，token 即 rta_ 凭据）：
#   curl -fsSL https://<grape主控>/api/runtime/install | bash -s -- \
#     --token <rta_...> \
#     [--server https://<grape主控>] \
#     [--device worker-01] \
#     [--dir ~/grape] \
#     [--root] \
#     [--engine auto|opencode|hermes|none]
#
# [同机多 cloud_worker spec §4.2] --type/--platform 已删除；--dir 保留（2026-11-12
# 用户裁定：恢复原有参数语义）：
#   - 凭据类型/平台由 whoami（GET /api/runtime/whoami, Bearer rta_）返回——
#     服务端是唯一事实源，用户命令不可能传错形态
#   - 安装目录按形态隔离: 普通 worker 默认 /opt/grape（非 root ~/grape）; 沙箱
#     worker 默认 /opt/grape-cw/<cred_uid8>/（每凭据全量独立——升级/回滚/venv
#     互不影响）；用户显式传 --dir 时按用户目录安装（沙箱不再追加 cred_uid8）
#   - --device 保留（两类 worker 共用，指示 worker 运行在哪台机器）
#
# 流程：
#   1. 从主控公开端点下载运行包 grape-worker.tar.gz（无需鉴权，包内无敏感信息）
#   2. 解压到普通用户家目录（默认 ~/grape，--dir 可改）
#   3. 建 venv + 安装最小依赖
#   4. 写入接入配置（凭据 token + 主系统地址，权限 600）
#   5. codearts 目录信任预配置（无 TTY 不再因 folder-trust 闸门致命退出）
#   6. 后台启动 runtime-agent（免 root，nohup/setsid；root 时自动用 systemd）
#   7. 启动后自动注册到主控系统（RuntimeAgent registered；无引擎时也注册成功）
#
# 幂等：重复执行安全（复用已有目录/venv，仅更新代码与配置并重启）。
# 执行引擎（opencode/hermes/codearts 等）不由本脚本安装（Issue #1884）：
# 用户自行安装即可，worker 启动自动发现并注册；运行中装好引擎无需重启（周期重注册）。
set -euo pipefail

# ===== 参数解析 =====
TOKEN="${TOKEN:-}"
SERVER=""
DEVICE=""
INSTALL_DIR=""
ROOT_MODE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; shift 2 ;;
    --server)  SERVER="$2"; shift 2 ;;
    --device)  DEVICE="$2"; shift 2 ;;
    --dir)     INSTALL_DIR="$2"; shift 2 ;;
    --root)    ROOT_MODE=1; shift ;;
    # [spec §4.2] --type/--platform 已删除（whoami 派生）——显式报错引导而非
    # 静默忽略，防旧文档命令装错形态。--dir 保留（原有参数，2026-11-12 裁定）。
    --type|--platform)
      echo "[install-worker] Error: $1 removed (credential form is derived from server whoami) -- use: --token <rta_...> --server <url> [--device name] [--dir install-dir] [--engine auto|opencode|hermes|none] [--root]" >&2; exit 1 ;;
    --*=*) echo "[install-worker] Error: equals-form not supported ($1) -- use space separation: ${1%%=*} <value>" >&2; exit 1 ;;
    *) echo "Unknown argument: $1"; echo "Usage: --token <rta_...> [--server URL] [--device name] [--engine auto|opencode|hermes|none] [--root] (arguments use space separation, no --x=y)"; exit 1 ;;
  esac
done
# ===== 工具函数 =====
log() { echo "[install-worker] $*"; }
fail() { echo "[install-worker] Error: $*" >&2; exit 1; }



if [ -z "$TOKEN" ]; then
  fail "Missing --token (rta_ credential, generated on the controller WebUI Workers page)"
fi
if [ "$(id -u)" = "0" ]; then
  ROOT_MODE=1
fi
if [ -z "$SERVER" ]; then
  SERVER="https://grape.example.com"
  log "No --server given, using default $SERVER (add --server https://<your-controller> if wrong)"
fi
SERVER="${SERVER%/}"
# 设备名：hostname 小写，非字母数字替换为 -，清理首尾 -（hostname 输出含换行会产生尾随 -）
if [ -z "$DEVICE" ]; then
  DEVICE="worker-$(hostname | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9]/-/g; s/^-*//; s/-*$//')"
fi

# ===== whoami：凭据自述（服务端是唯一事实源）=====
# [同机多 cloud_worker spec §4.1] 用 token 调 whoami 拿 cred_type/platform/
# credential_id/credential_uid——安装形态、实例命名全部由服务端派生，不可能传错。
_whoami_json=""
if command -v curl >/dev/null 2>&1; then
  _whoami_json="$(curl -fsSL --connect-timeout 15 --max-time 30 \
    -H "Authorization: Bearer $TOKEN" \
    "$SERVER/api/runtime/whoami" 2>/dev/null || true)"
fi
if [ -z "$_whoami_json" ] || ! printf '%s' "$_whoami_json" | grep -q '"credential_id"'; then
  fail "Credential invalid or revoked (whoami unreachable/401) -- regenerate the credential on the controller WebUI and retry"
fi
# 解析（不依赖 jq——sed 提取）
CRED_TYPE="$(printf '%s' "$_whoami_json" | sed -n 's/.*"cred_type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
PLATFORM="$(printf '%s' "$_whoami_json" | sed -n 's/.*"platform"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
CRED_ID="$(printf '%s' "$_whoami_json" | sed -n 's/.*"credential_id"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')"
if [ "$CRED_TYPE" != "worker" ] && [ "$CRED_TYPE" != "sandbox" ]; then
  fail "whoami returned invalid cred_type: '$CRED_TYPE' (only worker/sandbox supported)"
fi
if [ "$CRED_TYPE" = "sandbox" ] && [ -z "$PLATFORM" ]; then
  fail "whoami returned sandbox credential but platform is empty (server data abnormal)"
fi
# 实例键 [锚简化 2026-11-04 用户裁定]：credential_uid 前 8 位单锚——
# UUIDv4 全局唯一，跨主控天然不撞（复合锚 instance 段随 server_instance 退役而消解）
CRED_UID="$(printf '%s' "$_whoami_json" | sed -n 's/.*"credential_uid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
CRED_KEY="$(printf '%s' "$CRED_UID" | cut -c1-8)"   # 凭据目录键=credential_uid 前 8 位
# worker 入口模块：普通=runtime.agent；沙箱=runtime.cloud_worker
WORKER_MODULE="src.domains.runtime.engine.agent"
[ "$CRED_TYPE" = "sandbox" ] && WORKER_MODULE="src.domains.runtime.engine.cloud_worker"
# systemd 服务名 + 注册成功日志标记（沙箱与普通 worker 区分，同机共存不互杀）
SERVICE_NAME="runtime-agent"
REGISTER_LOG="RuntimeAgent registered"
if [ "$CRED_TYPE" = "sandbox" ]; then
  SERVICE_NAME="sandbox-worker-$CRED_KEY"
  REGISTER_LOG="cloud_worker registered"
fi
# pkill 匹配用转义模块名（src.runtime.agent 里的 . 是正则任意符）
WORKER_MODULE_ESC="$(printf '%s' "$WORKER_MODULE" | sed 's/\./\\./g')"
# 判活/pkill 用的模块正则：转义名 + 右边界。不带 ( |$) 时同前缀模块（如
# ...engine.agent_pool / cloud_worker_xxx）会被当成 worker 本身，误杀或误判活。
WORKER_MODULE_PAT="${WORKER_MODULE_ESC}( |\$)"

# 启动判活窗口（等进程出现）。原实现是固定 sleep 3：慢盘 / 首次装依赖时新 worker 还没
# 起来就被判「启动失败」并回滚，等于把一次成功的安装撤掉。取值口径：
#   * 上限要小于下方「等注册」窗口（普通 worker 10×3s=30s；沙箱 80×3s=240s）；
#   * 间隔 1s 只是采样粒度，避免 pgrep 空转；
#   * 15 / 1 为占位值，待真实冷启动耗时分布（P50/P95，含慢盘与首装依赖）实测后再定；
#     与平台心跳 RUNTIME_HEARTBEAT_INTERVAL=15 无语义关系，勿混用。
_AGENT_START_POLL_MAX=15
_AGENT_START_POLL_INTERVAL=1

# 安装目录 [spec §五 v2 用户裁定 2026-10-30; --dir 保留 2026-11-12 用户裁定]:
# - 普通 worker: 默认 /opt/grape（非 root ~/grape），--dir 可改（原有逻辑）
# - 沙箱 worker: 默认 /opt/grape-cw/<cred_uid8>/ ——每凭据全量独立(代码/venv/
#   备份/日志/配置), 升级回滚互不影响; pkill 按 venv 路径匹配天然限域,
#   跨实例误杀结构性消失。共享目录时代实测的三个全局破坏(tar 覆盖全实例
#   代码/回滚 rm src 影响他实例/单份 .upgrade-backup 被覆盖)全部消除。
# - 用户显式传 --dir（2026-11-12）: 沙箱也按用户目录安装（不再追加
#   cred_uid8 子目录）——用户自己保证目录独立性; 同机多实例场景建议用默认。
if [ "$CRED_TYPE" = "sandbox" ]; then
  if [ -n "$INSTALL_DIR" ]; then
    :  # 用户显式 --dir：尊重用户目录，沙箱不追加凭据键
  elif [ "$ROOT_MODE" = "1" ]; then
    INSTALL_DIR="/opt/grape-cw/$CRED_KEY"
  else
    INSTALL_DIR="$HOME/grape-cw/$CRED_KEY"
  fi
else
  if [ -z "$INSTALL_DIR" ]; then   # --dir 原有逻辑：未传时才落默认（基线守卫，沙箱线移植时曾丢失）
    if [ "$ROOT_MODE" = "1" ]; then
      INSTALL_DIR="/opt/grape"
    else
      INSTALL_DIR="$HOME/grape"
    fi
  fi
fi
INSTALL_DIR="${INSTALL_DIR%/}"
VENV="$INSTALL_DIR/.venv"

# 配置目录与文件——[实例全量独立 v2] 沙箱模式配置就在实例目录内(不再
# etc/workers 子目录); 普通 worker 不变(/etc/grape 或 ~/.grape)。
if [ "$CRED_TYPE" = "sandbox" ]; then
  CFG_DIR="$INSTALL_DIR/etc"
else
  if [ "$ROOT_MODE" = "1" ]; then
    CFG_DIR="/etc/grape"
  else
    CFG_DIR="$HOME/.grape"
  fi
fi
START_SCRIPT="$INSTALL_DIR/start-worker.sh"
ENV_FILE="$CFG_DIR/runtime.env"
VERSION_FILE="$CFG_DIR/worker.version"

# 目标机架构（Issue #515：worker 安装包按架构捆绑 python-x86_64/ python-aarch64/，
# 安装端按 uname -m 选对应目录；amd64/arm64 为兼容别称）
TARGET_ARCH="$(uname -m)"
case "$TARGET_ARCH" in
  x86_64|amd64) TARGET_ARCH="x86_64" ;;
  aarch64|arm64) TARGET_ARCH="aarch64" ;;
esac

log "Install dir: $INSTALL_DIR | Device: $DEVICE | Controller: $SERVER | Arch: $TARGET_ARCH | Mode: $([ "$ROOT_MODE" = 1 ] && echo root/systemd || echo user/nohup)"

# ===== 1. 准备 Python >= 3.10 =====
# 优先级：系统 python3.12/3.11/3.10/python3 → 包内自带解释器（python-<arch>/ 分目录，
# standalone 免编译；arch 由第 3.5 步按 TARGET_ARCH 选取；旧包 python/ 单目录仅为读取兜底）
# 注意：包内解释器要等第 3 步解压后才可用，故此处只探测系统 python，第 3 步后再补探测包内。
pick_python() {
  for py in python3.12 python3.11 python3.10 python3; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      echo "$py"; return 0
    fi
  done
  return 1
}
PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  log "No system Python>=3.10 found, will try the bundled interpreter (auto-detected after extraction)"
fi

# ===== 2. 下载运行包（公开端点，无需鉴权）=====
PKG_URL="$SERVER/api/runtime/grape-worker.tar.gz"
TMP_PKG="/tmp/grape-worker-$$.tar.gz"
log "Downloading worker package: $PKG_URL"
if ! curl -fsSL --connect-timeout 15 "$PKG_URL" -o "$TMP_PKG"; then
  rm -f "$TMP_PKG"
  fail "Failed to download worker package: $PKG_URL (check controller address / network reachability)"
fi
if ! gzip -t "$TMP_PKG" 2>/dev/null; then
  rm -f "$TMP_PKG"
  fail "Downloaded package corrupted (gzip check failed), please retry"
fi

# ===== 3. 解压到安装目录（幂等，升级前备份旧代码供回滚）=====
mkdir -p "$INSTALL_DIR"
# 升级回滚保护（2026-10-08）：覆盖代码前备份当前 src/ + VERSION。
# 第 6 步"杀旧起新"若新 agent 起不来，restore_upgrade_backup 可恢复旧版本重启，
# 避免 worker 永久失联（build-0005 / k8gtld1 两台实锤：杀旧成功、起新失败、
# 无人拉回）。备份只留最近一份（.upgrade-backup），旧备份先清，磁盘占用可控。
_upgrade_backup_dir="$INSTALL_DIR/.upgrade-backup"
if [ -d "$INSTALL_DIR/src" ]; then
  rm -rf "$_upgrade_backup_dir"
  mkdir -p "$_upgrade_backup_dir"
  cp -a "$INSTALL_DIR/src" "$_upgrade_backup_dir/src" 2>/dev/null || true
  [ -f "$INSTALL_DIR/VERSION" ] && cp -a "$INSTALL_DIR/VERSION" "$_upgrade_backup_dir/VERSION" 2>/dev/null || true
  log "Current code backed up to $_upgrade_backup_dir (auto-rollback on upgrade failure)"
fi
log "Extracting worker package to $INSTALL_DIR"
tar -xzf "$TMP_PKG" -C "$INSTALL_DIR" --strip-components=1 || fail "Failed to extract worker package"
rm -f "$TMP_PKG"

# ===== 3.5 升级前 import 冒烟检查（2026-10-14 五台 worker 心跳全断事故）=====
# 解压完成后、停旧进程之前，用目标 venv 实际 import 新代码。
# 拦截"包内缺模块/顶层 import 指向未打包模块"类坏包（当天事故：config.py 顶层
# import src.auth，但 src/auth/ 不在 MANIFEST → runtime-agent 启动即
# ModuleNotFoundError，systemd 5s 无限重启 6 小时，worker 永久失联）。
# 冒烟失败 → fail 并保留旧代码不动（第 6 步还没执行，旧进程仍在运行），
# worker 继续用旧版本心跳，等下一个修复版本——零中断。
# 注意：全新安装（无旧代码）时冒烟失败同样 fail，避免装出必死的实例。
_smoke_check() {
  [ -x "$VENV/bin/python" ] || return 0   # venv 未建（首次安装后续步骤才建）：跳过，由第 6 步启动验证兜底
  if "$VENV/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.kernel.config; import src.domains.runtime.engine.agent" >/dev/null 2>&1; then
    log "New code import smoke check passed"
    return 0
  else
    # worker 侧埋点依赖 otel：缺包直接中止升级（架构守护 BLOCKED-1）
    _smoke_err="$("$VENV/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.kernel.config; import src.domains.runtime.engine.agent; import src.observability.otel" 2>&1 | tail -1)"

    log "!!! New code import smoke check failed: $_smoke_err"
    log "!!! Upgrade rejected (old code untouched, worker keeps running the current version) -- please report this log to the controller maintainer"
    return 1
  fi
}
_smoke_check || fail "New worker package import smoke check failed (bad package); upgrade aborted, old code untouched"

# restore_upgrade_backup <起服务方式>：恢复备份的旧代码并按传入方式重启 agent。
# systemd: restore_upgrade_backup systemd；nohup: restore_upgrade_backup nohup
restore_upgrade_backup() {
  _mode="${1:-nohup}"
  # set -u 兜底：本函数会被单测独立提取执行（tests/api/test_install_rollback_regression.py，
  # 只注入 INSTALL_DIR/VENV/_upgrade_backup_dir/START_SCRIPT），WORKER_MODULE_ESC 为脚本级
  # 变量——缺省回退到任意 worker 模块的宽松匹配，单测/复用场景不塌。
  _wm_esc="${WORKER_MODULE_ESC:-src\.domains\\.runtime\\.engine\.[a-z_]+}"
  _wm_pat="${_wm_esc}( |\$)"
  log "!!! New agent failed to start, rolling back to pre-upgrade code and restarting the old agent !!!"
  if [ ! -d "$_upgrade_backup_dir/src" ]; then
    # [全新安装语义修复 2026-11-05] 实例 52 实测：全新安装无备份（本来就没有旧代码），
    # 此时报错"worker will stay offline"是错的——worker 根本不需要回滚，探测/注册
    # 都在正常跑。正确动作 = 不动它，直接确认状态返回成功。
    if systemctl is-active --quiet "${SERVICE_NAME:-runtime-agent}" 2>/dev/null; then
      log "No pre-upgrade backup (fresh install) -- nothing to roll back; new agent is ACTIVE, leaving it alone"
      return 0
    fi
    log "Rollback failed: no backup ($_upgrade_backup_dir missing) and service is NOT active -- manual intervention needed: journalctl -u ${SERVICE_NAME:-runtime-agent}"
    return 1
  fi
  rm -rf "$INSTALL_DIR/src"
  cp -a "$_upgrade_backup_dir/src" "$INSTALL_DIR/src"
  [ -f "$_upgrade_backup_dir/VERSION" ] && cp -a "$_upgrade_backup_dir/VERSION" "$INSTALL_DIR/VERSION"
  log "Restored old code ($(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo unknown))"
  if [ "$_mode" = "systemd" ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart ${SERVICE_NAME:-runtime-agent} >/dev/null 2>&1 || true
  else
    # 2026-10-08 follow-up：先杀残留的新进程再起旧版。注册超时路径（启动验证 30s
    # 未 registered）调用本函数时，新 agent 进程可能仍存活——若不限域 pkill，
    # 旧版起成功后新旧两进程并存，残留新进程可能反向注册击穿回滚。
    # 限域模式与第 6 步停旧实例一致：按本安装目录 venv 绝对路径匹配，不误杀同机
    # 其他安装/其他用户的 worker。
    _rollback_pat="$(printf '%s' "$VENV/bin/python" | sed 's/[.\[*^$]/\\&/g')"
    pkill -f "${_rollback_pat}.*-m ${_wm_pat}" >/dev/null 2>&1 || true
    sleep 1
    setsid nohup "$START_SCRIPT" >> "$INSTALL_DIR/data/worker.log" 2>&1 < /dev/null &
    disown 2>/dev/null || true
  fi
  sleep 5
  if pgrep -f "$(printf '%s' "$VENV/bin/python" | sed 's/[.[\*^$]/\\&/g').*-m ${_wm_pat}" >/dev/null 2>&1; then
    log "Rollback OK: old agent restored and running"
    return 0
  fi
  log "Agent still not running after rollback -- worker offline, manual intervention needed (log: $INSTALL_DIR/data/worker.log / journalctl -u $SERVICE_NAME)"
  return 1
}

# 日志水位：注册判据只认「本次启动之后」写入的行，历史运行留下的 registered 不算。
# 用 awk 的 NR（记录数）而不是 wc -l（换行数）：日志末行若没有结尾换行（上次运行被
# SIGKILL 截断写入的形态），wc -l 会少算一行，那一行历史注册行就会被 tail 重新纳入
# 扫描窗口 → 本次根本没注册也被判成功。文件不存在返回 0。
log_line_watermark() {
  local _f="${1:-}"
  if [ -n "$_f" ] && [ -f "$_f" ]; then
    awk 'END { printf "%d\n", NR }' "$_f" 2>/dev/null || printf '0\n'
  else
    printf '0\n'
  fi
}

# 判活：只认启动前 PID 快照里没有的新 PID（区分「本次新起的 worker」与「尚未退出的旧实例」）。
# 用法：wait_new_agent_alive "<pids_before>" "<限域前缀>" "<模块正则>"；命中时 stdout 打印新 PID。
wait_new_agent_alive() {
  local _before="${1:- }" _pat="${2:-}"
  local _mod="${3:-${WORKER_MODULE_PAT:-src\\.domains\\.runtime\\.engine\\.[a-z_]+( |\\$)}}"
  local _max="${_AGENT_START_POLL_MAX:-15}" _gap="${_AGENT_START_POLL_INTERVAL:-1}"
  local _alive="" _p _i
  for _i in $(seq 1 "$_max"); do
    for _p in $(pgrep -f "${_pat}.*-m ${_mod}" 2>/dev/null || true); do
      case "$_before" in
        *" $_p "*) : ;;
        *) _alive="$_p" ;;
      esac
      [ -n "$_alive" ] && break
    done
    [ -n "$_alive" ] && break
    sleep "$_gap"
  done
  [ -n "$_alive" ] || return 1
  printf '%s\n' "$_alive"
}

# 注册判据：只看日志水位之后新写入的行（历史注册行不参与判定）。
# 用法：wait_runtime_registered "<log>" "<起始行号>" "<注册日志标记>" "<轮数>"；默认 10 × 3s。
wait_runtime_registered() {
  local _f="${1:-}" _from="${2:-1}" _mark="${3:-${REGISTER_LOG:-RuntimeAgent registered}}"
  local _loops="${4:-${_reg_timeout_loops:-10}}" _i
  for _i in $(seq 1 "$_loops"); do
    if [ $((_i % 10)) -eq 0 ]; then
      log "waiting for worker registration... ($((_i * 3))s elapsed)"
    fi
    if tail -n "+${_from}" "$_f" 2>/dev/null | grep -q "$_mark"; then
      return 0
    fi
    sleep 3
  done
  return 1
}

# 校验包内 SHA256SUMS：包内路径带 grape-worker/ 前缀，解压 --strip-components=1 后
# 实际文件在安装目录根，校验时去掉前缀（存在则校验；缺失仅告警）
if [ -f "$INSTALL_DIR/SHA256SUMS" ]; then
  if ( cd "$INSTALL_DIR" && sed 's|  grape-worker/|  |' SHA256SUMS | sha256sum -c >/dev/null 2>&1 ); then
    log "Worker package content checksum verified (SHA256SUMS)"
  else
    log "Warning: package checksum mismatch, verify the source is trusted"
  fi
fi
mkdir -p "$CFG_DIR"
if [ -f "$INSTALL_DIR/VERSION" ]; then
  cp "$INSTALL_DIR/VERSION" "$VERSION_FILE" 2>/dev/null || true
  log "Worker version: $(cat "$VERSION_FILE" 2>/dev/null || echo unknown)"
fi

# data/ 运行时目录
mkdir -p "$INSTALL_DIR/data"

# ===== 3.5 包内解释器补探测（解压后）+ 架构校验（Issue #515）=====
# 双保险：① 按 TARGET_ARCH 选 python-<arch>/ 目录（与包内捆绑目录对应）；
#         ② 试运行 --version 确认可执行（免解析 ELF，交叉架构/损坏包提前暴露）。
# 不匹配 → 跳过包内解释器并引导安装系统 Python>=3.10，确保在线安装兜底路径可达
# （旧实现：x86_64 机选中 aarch64 解释器 → venv 创建时 Exec format error 直接退出）。
# 注：python/ 单目录为老主控旧包格式兜底（同样过 --version 校验，防交叉架构误选）。
_pick_bundled_python() {
  for _cand in "$INSTALL_DIR/python-$TARGET_ARCH/bin/python3" "$INSTALL_DIR/python/bin/python3"; do
    if [ -x "$_cand" ] && "$_cand" --version >/dev/null 2>&1; then
      echo "$_cand"
      return 0
    fi
  done
  return 1
}
if [ -z "$PY" ]; then
  _BUNDLED_PY="$(_pick_bundled_python || true)"
  if [ -n "$_BUNDLED_PY" ]; then
    PY="$_BUNDLED_PY"
    log "Using bundled Python (standalone, arch=$TARGET_ARCH, no system install needed)"
  else
    log "Warning: bundled Python for arch '$TARGET_ARCH' is missing or cannot run on this machine (arch mismatch or corrupted package)"
    log "Hint: install a system Python>=3.10 first (apt/yum install python3.12 python3.12-venv), then retry"
  fi
fi
if [ -z "$PY" ]; then
  fail "Python>=3.10 not found; install a system Python>=3.10 first (apt/yum install python3.12 python3.12-venv) and retry, or re-download the install package from an upgraded controller (should contain python-$TARGET_ARCH/)"
fi
log "Using Python: $PY ($($PY --version 2>&1 | awk '{print $2}'))"

# ===== 4. 建 venv + 最小依赖 =====
# venv 三级自愈（Issue #257 B1）：pip 文件在但损坏（ModuleNotFoundError）时递进修复
#   Level 1: ensurepip --upgrade
#   Level 2: get-pip.py（curl 下载执行）
#   Level 3: 删除 .venv 重建（site-packages 损坏，pip+aiohttp 都丢）
_venv_python_ok() { "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; }
_venv_pip_ok() { "$VENV/bin/python" -m pip --version >/dev/null 2>&1; }

if [ ! -x "$VENV/bin/python" ] || ! _venv_python_ok; then
  log "Creating venv ($PY)"
  rm -rf "$VENV"
  "$PY" -m venv "$VENV"
fi

# 确保 venv 内有 pip 且可用（文件存在 ≠ 可用——Issue #123 pip 损坏场景）
PIP_CMD="$VENV/bin/pip"
if [ -x "$PIP_CMD" ] && _venv_pip_ok; then
  : # pip 文件在且可正常 import，无需修复
elif [ -x "$VENV/bin/pip3" ] && "$VENV/bin/python" -m pip3 --version >/dev/null 2>&1; then
  log "venv has no pip (but has pip3), using pip3 instead"
  PIP_CMD="$VENV/bin/pip3"
else
  # pip 缺失或损坏——三级递进自愈
  log "venv pip not usable, starting 3-level self-repair..."
  # Level 1: ensurepip
  log "  [1/3] trying ensurepip --upgrade..."
  if "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 && _venv_pip_ok; then
    log "  ensurepip repair succeeded"
  else
    # Level 2: get-pip.py
    log "  [2/3] ensurepip did not help, trying get-pip.py..."
    if curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$VENV/bin/python" >/dev/null 2>&1 && _venv_pip_ok; then
      log "  get-pip.py repair succeeded"
    else
      # Level 3: 重建 venv
      log "  [3/3] pip badly broken (site-packages damaged), rebuilding venv..."
      _saved_ver=""
      [ -f "$VERSION_FILE" ] && _saved_ver=$(cat "$VERSION_FILE" 2>/dev/null || true)
      rm -rf "$VENV"
      "$PY" -m venv "$VENV"
      "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || \
        curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$VENV/bin/python" >/dev/null 2>&1 || true
      [ -n "$_saved_ver" ] && echo "$_saved_ver" > "$VERSION_FILE" 2>/dev/null || true
      if _venv_pip_ok; then
        log "  venv rebuilt, pip restored"
      else
        fail "All 3 venv self-repair levels failed, pip still unusable. Manually remove $VENV and retry, or install the python3-venv package"
      fi
    fi
  fi
  # 统一 PIP_CMD（ensurepip/get-pip 可能只装 pip3）
  if [ ! -x "$PIP_CMD" ] && [ -x "$VENV/bin/pip3" ]; then
    PIP_CMD="$VENV/bin/pip3"
  fi
fi
# Issue #699 Phase 0: requirements-worker.txt merged into pyproject.toml [worker] extra.
# [沙箱迁移生效性修复 2026-11-10] 列表必须与 pyproject.toml [project.optional-dependencies]
# worker 保持一致（声明与消费同源）——此前 pyproject 加了 cubesandbox/requests/cryptography
# 但安装脚本硬列表没跟上 → 沙箱 worker 装机缺平台 SDK（skip template scan）与凭据加密
# （crypto 软降级告警），.17 实例 852b4efd 实证；二期 7506b5c6 靠手工 pip 补装掩盖。
WORKER_DEPS="aiohttp bcrypt click httpx cubesandbox requests cryptography"
if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
  # pip 镜像：默认清华（国内网络 pypi.org 直连常超时），可用 PIP_INDEX_URL 或 --pip-index 覆盖
  PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  # 优先离线安装：包内已带依赖 wheels（grape-worker/wheels/），免网络下载
  if [ -d "$INSTALL_DIR/wheels" ] && ls "$INSTALL_DIR/wheels"/*.whl >/dev/null 2>&1; then
    log "Installing deps offline (bundled wheels: $(ls "$INSTALL_DIR/wheels"/*.whl | wc -l) files, no network needed)"
    if "$PIP_CMD" install --quiet --no-index --find-links "$INSTALL_DIR/wheels" \
        $WORKER_DEPS; then
      log "Deps installed offline (wheels auto-matched to arch $TARGET_ARCH by pip)"
    else
      log "Offline install failed (platform wheel not bundled or python version mismatch), falling back to online..."
      if ! "$PIP_CMD" install --quiet -i "$PIP_INDEX" --retries 3 --timeout 30 \
          $WORKER_DEPS; then
        log "Dependency install failed. Check network and retry, or specify a reachable mirror:"
        log "  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash -c 'curl -fsSL $SERVER/api/runtime/install | bash -s -- --token <token> --server $SERVER'"
        fail "Dependency install failed (worker cannot start without aiohttp): retry this install command, or set PIP_INDEX_URL to a reachable pip mirror"
      fi
    fi
  else
    log "Installing deps online (mirror: $PIP_INDEX, first run takes minutes)"
    if ! "$PIP_CMD" install --quiet -i "$PIP_INDEX" --retries 3 --timeout 30 \
        $WORKER_DEPS; then
      fail "Dependency install failed (worker cannot start without aiohttp): retry this install command, or set PIP_INDEX_URL to a reachable pip mirror"
    fi
    # 安装完成打印实际版本(pip list 里的 worker 依赖)
    log "Installed dependency versions:"
    "$PIP_CMD" list --format=freeze 2>/dev/null | grep -iE '^(aiohttp|bcrypt|click|httpx|cubesandbox|grape)==?' | sed 's/^/    /' || true
  fi
fi

# ===== 4.5 安装 grape CLI（独立项目 grape-cli；优先随包离线 wheel，失败回退在线）=====
_install_grape_online() {
  "$PIP_CMD" install -q --no-cache-dir \
      git+https://gitcode.com/grape-dev/grape-cli.git 2>/dev/null \
    && chmod +x "$VENV/bin/grape" 2>/dev/null || true \
    && log "grape CLI installed (online): $VENV/bin/grape" \
    || log "Warning: grape-cli online install failed, no `grape` inside agent yet; worker runs fine without it"; return 0
}
if ls "$INSTALL_DIR/wheels"/grape-*.whl >/dev/null 2>&1; then
  if "$PIP_CMD" install -q --no-index --find-links "$INSTALL_DIR/wheels" grape 2>/dev/null; then
    chmod +x "$VENV/bin/grape" 2>/dev/null || true
    log "grape CLI installed (offline wheel, bundled): $VENV/bin/grape"
  else
    log "Warning: offline grape wheel install failed, falling back to online..."
    _install_grape_online
  fi
else
  _install_grape_online
fi

# ===== 5. 写入接入配置（凭据 + 主系统地址，权限 600）=====
# 保留旧文件中「非本脚本管理」的所有行（重装/自动升级不应覆盖本地配置）：
# ① 读出旧文件中除 6 个 RUNTIME_* 受管键之外的键值行 ② cat 覆盖受管键 ③ 追加回保留行
# 事故（2026-11-10）：旧逻辑只保留 ^HERMES_CUSTOM，worker 自动升级后
# OPENGW_API_KEY（aishell-dsh 第三方模型）被抹掉 → 任务报
#   llm-pi-ai: no credential for provider route "opengw"
umask 077
_PRESERVED_KEYS=""
if [ -f "$ENV_FILE" ]; then
  _PRESERVED_KEYS=$(grep -vE '^(RUNTIME_TOKEN|RUNTIME_SERVER_URL|RUNTIME_DEVICE_NAME|RUNTIME_MAX_CONCURRENT_TASKS|RUNTIME_POLL_INTERVAL|RUNTIME_HEARTBEAT_INTERVAL|CLOUD_WORKER_PROVIDER|CLOUD_WORKER_SERVER_URL|CLOUD_WORKER_TOKEN|CLOUD_WORKER_DEVICE_NAME|CLOUD_WORKER_POLL_INTERVAL|CLOUD_WORKER_HEARTBEAT_INTERVAL|CLOUD_WORKER_CREDENTIAL_ID|CLOUD_WORKER_CREDENTIAL_UID)=' "$ENV_FILE" 2>/dev/null | grep -vE '^[[:space:]]*(#|$)' || true)
fi
if [ "$CRED_TYPE" = "sandbox" ]; then
  # [沙箱迁移] 沙箱 worker：cloud_worker 配置（rta_ token 直用 Bearer 鉴权；
  # platform 决定 Provider 适配器；引擎注册从服务端模板拉取）
  cat > "$ENV_FILE" <<EOF
CLOUD_WORKER_PROVIDER=$PLATFORM
CLOUD_WORKER_SERVER_URL=$SERVER
CLOUD_WORKER_TOKEN=$TOKEN
CLOUD_WORKER_DEVICE_NAME=$DEVICE
CLOUD_WORKER_POLL_INTERVAL=3
CLOUD_WORKER_HEARTBEAT_INTERVAL=15
CLOUD_WORKER_CREDENTIAL_ID=$CRED_ID
CLOUD_WORKER_CREDENTIAL_UID=$CRED_UID
EOF
else
  cat > "$ENV_FILE" <<EOF
RUNTIME_TOKEN=$TOKEN
RUNTIME_SERVER_URL=$SERVER
RUNTIME_DEVICE_NAME=$DEVICE
RUNTIME_MAX_CONCURRENT_TASKS=20
RUNTIME_POLL_INTERVAL=3
RUNTIME_HEARTBEAT_INTERVAL=15
EOF
fi
# 追加旧配置中保留的自定义 keys（防重装/升级丢 key → opencode 401 / aishell 无凭据）
if [ -n "$_PRESERVED_KEYS" ]; then
  printf '%s\n' "$_PRESERVED_KEYS" >> "$ENV_FILE"
fi
# opencode 模型 provider 引用 {env:HERMES_CUSTOM_*}（worker 侧 opencode.json）。
# worker 以 systemd/nohup 运行时无交互 shell 环境变量，安装时若环境存在该变量则一并写入，
# 避免重装后 opencode 模型调用 401（2026-08-17 事故链之二 / 2026-08-28 OPENGW key 缺失）。
# key 值不硬编码进脚本，安装时通过环境变量传入：
#   export HERMES_CUSTOM_API_DEEPSEEK_COM_API_KEY=xxx HERMES_CUSTOM_OPENGW_CLOUDDEVELOPER_CLUB_API_KEY=yyy && curl ... | bash
# 支持的 key 列表（新增 key 只需在此数组追加一行）：
_HERMES_API_KEYS=(
  HERMES_CUSTOM_API_DEEPSEEK_COM_API_KEY
  HERMES_CUSTOM_OPENGW_CLOUDDEVELOPER_CLUB_API_KEY
  OPENGW_API_KEY
)
for _key_name in "${_HERMES_API_KEYS[@]}"; do
  _key_value="${!_key_name:-}"
  if [ -n "$_key_value" ]; then
    # 去重：旧文件已有同名 key 则不重复写
    if ! grep -q "^${_key_name}=" "$ENV_FILE" 2>/dev/null; then
      printf '%s=%s\n' "$_key_name" "$_key_value" >> "$ENV_FILE"
    fi
  fi
done
unset _key_name _key_value
chmod 600 "$ENV_FILE" 2>/dev/null || true
log "Runtime config written to $ENV_FILE (token + controller address)"

# ===== 5.5 安装 bwrap（agent 文件系统隔离锁；root 自动装，普通用户提示）=====
# bwrap 用于给 agent 子进程套文件系统隔离（阶段二）。已装且可用 → 跳过；
# root 模式 → 自动 apt install + setuid；普通用户 → 提示（无锁运行，不影响 worker）。
# AppArmor（Ubuntu 24.04）限制非特权 userns，用 setuid 位绕过（Debian 官方推荐）。
_bwrap_ok() {
  command -v bwrap >/dev/null 2>&1 && bwrap --ro-bind / / --proc /proc --dev /dev sh -c 'exit 0' 2>/dev/null
}
install_bwrap() {
  if _bwrap_ok; then
    log "bwrap already usable (agent filesystem isolation lock ready)"
    return 0
  fi
  if [ "$ROOT_MODE" = "1" ]; then
    log "Installing bwrap (bubblewrap) + relaxing AppArmor restriction (setuid)..."
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -qq >/dev/null 2>&1 || true
      apt-get install -y -qq bubblewrap >/dev/null 2>&1 || log "Warning: apt bwrap install failed"
      if command -v bwrap >/dev/null 2>&1; then
        chmod u+s /usr/bin/bwrap || log "Warning: failed to set bwrap setuid bit"
        dpkg-statoverride --add root root 4755 /usr/bin/bwrap >/dev/null 2>&1 || true
      fi
    elif command -v yum >/dev/null 2>&1; then
      yum install -y -q bubblewrap >/dev/null 2>&1 || log "Warning: yum bwrap install failed"
      command -v bwrap >/dev/null 2>&1 && chmod u+s /usr/bin/bwrap
    else
      log "Warning: neither apt nor yum found, cannot auto-install bwrap"
    fi
    if _bwrap_ok; then
      log "bwrap installed (agent filesystem isolation lock ready)"
    else
      log "Warning: bwrap still unusable (AppArmor strict) -- filesystem lock downgraded OFF (worker runs normally)"
    fi
  else
    log "Hint: no usable bwrap detected (needed by agent filesystem isolation). To enable, re-run this script as root (add --root), or manually run:"
    log "  sudo apt-get install -y bubblewrap && sudo dpkg-statoverride --add root root 4755 /usr/bin/bwrap"
    log "  (without bwrap the worker still runs, only the filesystem isolation lock is off)"
  fi
}
if [ "$CRED_TYPE" = "sandbox" ]; then
  log "Sandbox worker mode: skipping bwrap (engines live in remote cloud sandboxes, no local isolation needed)"
else
  install_bwrap
fi

# ===== 5.6 安装 sshpass（SRE 远程部署密码认证；root 自动装）=====
if [ "$ROOT_MODE" = "1" ]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    apt-get install -y -qq sshpass 2>/dev/null || yum install -y -q sshpass 2>/dev/null || true
    log "sshpass installed (SRE remote deploy password auth)"
  fi
fi


# ===== 5.8 codearts 目录信任预配置（Issue #471 C2）=====
# codearts CLI 的 folder-trust 闸门在无 TTY（systemd/nohup）环境下对未信任
# 目录直接 exit 1。把 worker 安装目录与 codearts tasks 目录合并进
# ~/.codeartsdoer/cli-data/settings.json 的 trustedFolders（保留已有配置）。
# ⚠️ Issue #471 QA S4：合并语义（保留 enabled 标志、trustedFolders 并入、
# 保留其它顶层键）与 src/domains/runtime/agents/codearts.write_trust_settings 逐条对齐——
# 修改任意一处必须同步另一处（worker 运行态用 Python 版，安装态用本段自包含实现）。
provision_codearts_trust() {
  command -v codearts >/dev/null 2>&1 || return 0
  _oc_data="$HOME/.codeartsdoer/cli-data"
  mkdir -p "$_oc_data"
  _oc_tasks="${CODEARTS_TASKS_DIR:-/var/cache/codearts-tasks}"
  # 优先复用安装包内 Python 实现（与运行态同一份逻辑，防漂移）；不可用时回退内联版
  if [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR')
from src.domains.runtime.agents.codearts import write_trust_settings
write_trust_settings('$_oc_data', ['$INSTALL_DIR', '$_oc_tasks'])
" && log "codearts folder trust configured (write_trust_settings: trustedFolders += $INSTALL_DIR)" && return 0
  fi
  python3 - "$_oc_data/settings.json" "$INSTALL_DIR" "$_oc_tasks" <<'PYEOF' || log "Warning: codearts trust config write failed (does not affect worker registration)"
import json, os, sys
path, dirs = sys.argv[1], sys.argv[2:]
settings = {}
try:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            settings = loaded
except Exception:
    settings = {}
sec = settings.get("security") if isinstance(settings.get("security"), dict) else {}
ft = sec.get("folderTrust") if isinstance(sec.get("folderTrust"), dict) else {}
if "enabled" not in ft:
    ft["enabled"] = True
folders = sec.get("trustedFolders") if isinstance(sec.get("trustedFolders"), list) else []
for d in dirs:
    if d and d not in folders:
        folders.append(d)
sec["folderTrust"], sec["trustedFolders"] = ft, folders
settings["security"] = sec
with open(path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
PYEOF
  log "codearts folder trust configured (trustedFolders += $INSTALL_DIR)"
}
provision_codearts_trust

# ===== 5.9 codegraph 预装（Issue #666 M4：project-info 分析引擎，best-effort 不阻断注册）=====
# 分发形式：npm 平台二进制包 @colbymchenry/codegraph（自包含 node 运行时 + Rust 内核，非 pip 包）。
# 优先级：已装跳过 → npm 在线安装 → 随包 install-codegraph.sh 离线安装（需 tgz）→ 告警跳过。
# 缺失只影响知识基线全量生成（服务端/本地分析降级 + 显式告警），worker 正常注册运行。
install_codegraph() {
  if command -v codegraph >/dev/null 2>&1; then
    log "codegraph already present ($(codegraph --version 2>/dev/null || echo unknown)), skipping"
    return 0
  fi
  if command -v npm >/dev/null 2>&1; then
    log "Installing codegraph (npm install -g @colbymchenry/codegraph)..."
    if timeout 300 npm install -g @colbymchenry/codegraph >/dev/null 2>&1 && command -v codegraph >/dev/null 2>&1; then
      log "codegraph installed ($(codegraph --version 2>/dev/null || echo unknown))"
      return 0
    fi
    log "Warning: codegraph npm install failed"
  fi
  if [ -f "$INSTALL_DIR/scripts/install-codegraph.sh" ]; then
    log "Trying bundled offline installer scripts/install-codegraph.sh..."
    bash "$INSTALL_DIR/scripts/install-codegraph.sh" --src "$INSTALL_DIR/scripts" && command -v codegraph >/dev/null 2>&1 && \
      log "codegraph installed offline" && return 0
    log "Warning: offline codegraph install unavailable (no matching platform tgz bundled)"
  fi
  log "Warning: codegraph NOT installed -- project-info full generation degraded with explicit warning."
  log "  Hint: install Node.js then npm install -g @colbymchenry/codegraph, or place platform tgz in scripts/ and re-run"
  return 0
}
install_codegraph

# codegraph 可用性校验（Issue #666 验收：缺失显式告警，不 fail）
if command -v codegraph >/dev/null 2>&1; then
  log "codegraph check OK: $(codegraph --version 2>/dev/null || echo 'present')"
else
  log "Warning: codegraph check FAILED ('codegraph --version' not available); worker runs fine, knowledge baseline will be degraded"
fi

# ===== 6. 生成启动脚本并启动 =====
# systemd 可用性检测：容器环境（如 DevSpace）通常无 systemd（PID 1 非 systemd），
# 此时 root 模式也必须回退到 nohup/setsid，否则 systemctl restart 会 fail 退出。
_has_systemd() {
  [ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" = "systemd" ] && command -v systemctl >/dev/null 2>&1
}

if [ "$ROOT_MODE" = "1" ] && _has_systemd; then
  # ---- root + systemd 可用：systemd 服务 ----
  SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  # [spec §6.3] 沙箱 worker: on-failure + 突发限制(心跳 401/403 连续 N=10 自杀后
  # 不被无限拉起成僵尸); 普通 worker 保持 always(现状)
  _RESTART_POLICY="always"
  _START_LIMIT_BURST="0"
  if [ "$CRED_TYPE" = "sandbox" ]; then
    _RESTART_POLICY="on-failure"
    _START_LIMIT_BURST="5"
  fi
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Grape RuntimeAgent (edge, $DEVICE)
After=network-online.target
Wants=network-online.target
# [spec §6.3] StartLimit 属 [Unit] 段——写在 [Service] 会被 systemd 忽略回落
# 默认 10s/5 次(.17 实证: RestartSec=5 循环 10s 窗口攒不够 5 次 → 212 次不限流)
StartLimitIntervalSec=300
StartLimitBurst=$_START_LIMIT_BURST

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV/bin/python -m ${WORKER_MODULE}
# [spec §6.3] 沙箱 worker: on-failure + 突发限制——worker 心跳 401/403 连续
# N=5 次自杀退出(凭据吊销防护), 不被无限拉起成僵尸; 普通 worker 保持 always
Restart=$_RESTART_POLICY
RestartSec=5
LimitNOFILE=524288
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$INSTALL_DIR/.venv/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=$ENV_FILE

[Install]
WantedBy=multi-user.target
EOF
  # Issue #1982：已装实例升级时，unit 文件可能保留旧模板（无 LimitNOFILE），
  # 用 drop-in 落地 fd 上限并 daemon-reload；新装/重装同样幂等生效。
  _DROPIN_DIR="/etc/systemd/system/runtime-agent.service.d"
  mkdir -p "$_DROPIN_DIR"
  cat > "$_DROPIN_DIR/limit-nofile.conf" <<EOF
[Service]
LimitNOFILE=524288
EOF
  # root 模式 worker 必须显式放行 root 引擎：agent.py 在 geteuid()==0 时以 78/CONFIG
  # 退出（除非 GRAPE_ALLOW_ROOT_ENGINE=1）。本分支的 unit 模板就是 User=root，
  # 也没有 grape 用户可切 —— 不带这一行时新装/重装 worker 会立刻进入
  # auto-restart 死循环（exit 78），心跳一次都发不出，平台上整批引擎掉线
  # （2026-11-16 0002/0003/0004/0005/0006 五台实测，重启计数 5000~9000 次）。
  # 非 root 安装（User=grape）走上面的分支，不需要也不应该带这个放行。
  cat > "$_DROPIN_DIR/30-allow-root.conf" <<EOF
[Service]
Environment=GRAPE_ALLOW_ROOT_ENGINE=1
EOF
  systemctl daemon-reload
  systemctl enable $SERVICE_NAME >/dev/null 2>&1 || true
  # 升级链防 start-limit：刚被 systemd 停过的单元立即 restart 会被 StartLimitBurst 拦截
  systemctl reset-failed $SERVICE_NAME >/dev/null 2>&1 || true
  # 重启前记日志水位：后面的 worker.log 兜底判据只认本次重启之后写入的注册行，
  # 否则同 unit 历史行会让「重启没成功」被读成「已注册」。
  _log_start_sd="$(log_line_watermark "$INSTALL_DIR/data/worker.log")"
  systemctl restart $SERVICE_NAME || { restore_upgrade_backup systemd || true; fail "New agent failed to start (rollback attempted); check journalctl -u $SERVICE_NAME"; }
  # 启动验证 + 失败回滚（2026-10-14 五台 worker 心跳全断事故补齐）：
  # `systemctl restart` 对 Type=simple 单元在进程 spawn 即返回成功，agent 随后
  # 崩溃（如 import 失败）它感知不到——nohup 分支自 2026-10-08 起有 3s+30s
  # 注册验证，systemd 分支此前缺失，导致坏包升级后 systemd 无限重启 6 小时。
  # 现验证两件事：3s 后单元仍 active（非 auto-restart 僵尸循环）且 journal
  # 无 Traceback；30s 内日志出现 registered。任一失败 → 回滚旧代码重启。
  sleep 3
  _unit_state="$(systemctl show $SERVICE_NAME --property=ActiveState --value 2>/dev/null || true)"
  _unit_sub="$(systemctl show $SERVICE_NAME --property=SubState --value 2>/dev/null || true)"
  if [ "$_unit_state" != "active" ] || [ "$_unit_sub" = "auto-restart" ] \
     || journalctl -u $SERVICE_NAME --since "-10 seconds" --no-pager 2>/dev/null | grep -q "Traceback"; then
    restore_upgrade_backup systemd || true
    fail "New agent crashed on startup (active=$_unit_state sub=$_unit_sub, rollback attempted); logs: journalctl -u $SERVICE_NAME"
  fi
  _reg_ok=""
  # [沙箱 worker 注册窗口 2026-11-04] 30s 对沙箱 worker 不够：cloud_worker 启动先做
  # 全量模板探测（逐模板建箱+kill，每箱 20-40s，4 模板 ~2min）探测完才打
  # `cloud_worker registered`——-27 实测 30s 假超时→回滚旧代码（旧代码注册序在探测前）。
  # 沙箱模式窗口扩到 240s（探测最坏 ~3min）；普通 worker 维持 30s（本地发现秒级）。
  _reg_timeout_loops=10
  [ "$CRED_TYPE" = "sandbox" ] && _reg_timeout_loops=80
  # [验证修复 2026-10-30 v3] v2 锚定 ExecMainStartTimestamp 仍误判一次(实例 22)——
  # restart 后属性填充有延迟, 轮询首刻取到空值 → 走回退旧相对窗口(已知会 miss)。
  # v3: 每轮重新取 start_ts(空则该轮等属性稳定), 且回退窗口改为宽窗口(-15min,
  # 全新安装场景不存在历史注册行误命中——上一实例是不同 SERVICE_NAME)。
  for _i in $(seq 1 $_reg_timeout_loops); do
    _start_ts="$(systemctl show $SERVICE_NAME --property=ExecMainStartTimestamp --value 2>/dev/null || true)"
    if [ -n "$_start_ts" ]; then
      if journalctl -u $SERVICE_NAME --since "$_start_ts" --no-pager 2>/dev/null | grep -q "$REGISTER_LOG"; then
        _reg_ok=1; break
      fi
    fi
    # [start_ts 偏晚漏洞修复 2026-11-05] 实例 52 实测：restart 返回→属性填充延迟使
    # start_ts 晚于实际注册时刻 1-2s，--since 过滤静默切掉注册行（240s 全空→误回滚
    # 健康新版，"no backup"报错吓人）。宽窗口兜底从 else 分支改为**每轮必查**：
    # grep 计数 ≥1 且 worker 进程活着即算注册成功（旧实例的历史注册行由
    # "限域杀旧+restart"语义排除——restart 后同 unit 历史行仍在，故这里必须
    # 双条件：宽窗口有注册行 && 当前进程 cmd 含本实例 venv 路径）。
    if journalctl -u $SERVICE_NAME --since "-15 min" --no-pager 2>/dev/null | grep -q "$REGISTER_LOG"; then
      if pgrep -f "${_pat:-$INSTALL_DIR}.*-m ${WORKER_MODULE_PAT}" >/dev/null 2>&1; then
        _reg_ok=1; break
      fi
    fi
    # 双源兜底: worker.log(nohup 场景 systemd 不写它, 恒为第二保险)
    # 只认重启之后新写入的行（_log_start_sd 为重启前水位）
    if tail -n "+$((_log_start_sd + 1))" "$INSTALL_DIR/data/worker.log" 2>/dev/null | grep -q "$REGISTER_LOG"; then
      _reg_ok=1; break
    fi
    sleep 3
  done
  if [ -n "$_reg_ok" ]; then
    log "Started via systemd and registered ($SERVICE_NAME)"
  elif restore_upgrade_backup systemd; then
    log "New agent registration timed out ($((_reg_timeout_loops * 3))s) -- rolled back to old version (new version may not reach the master)"
  else
    log "New agent registration timed out and rollback failed, worker state abnormal; manual check: journalctl -u $SERVICE_NAME"
  fi
else
  # ---- 普通用户 或 容器环境（无 systemd）：nohup/setsid 后台启动 ----
  if [ "$ROOT_MODE" = "1" ] && ! _has_systemd; then
    log "Root mode but systemd unavailable (container?), falling back to nohup/setsid"
    # 容器内 root 也需要 start-worker.sh，路径用 INSTALL_DIR 而非 /opt/grape 硬编码
    START_SCRIPT="$INSTALL_DIR/start-worker.sh"
  fi
  cat > "$START_SCRIPT" <<EOF
#!/usr/bin/env bash
# Grape Worker 启动脚本（由 install-worker.sh 生成，支持崩溃自动重启）
# daemon 模式：worker 崩溃后自动重启，间隔 5s，最多 10 次/小时

SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
MAX_RESTARTS=10
RESTART_WINDOW=3600
RESTART_DELAY=5

if [ "\$1" != "--no-daemon" ]; then
  RESTART_COUNT=0
  WINDOW_START=\$(date +%s)
  while true; do
    echo "[\$(date '+%Y-%m-%d %H:%M:%S')] Starting worker (attempt \$((RESTART_COUNT + 1)))..."
    "\$0" --no-daemon &
    WORKER_PID=\$!
    wait \$WORKER_PID
    EXIT_CODE=\$?
    NOW=\$(date +%s)
    ELAPSED=\$((NOW - WINDOW_START))
    if [ \$ELAPSED -gt \$RESTART_WINDOW ]; then
      RESTART_COUNT=0
      WINDOW_START=\$NOW
    fi
    RESTART_COUNT=\$((RESTART_COUNT + 1))
    if [ \$RESTART_COUNT -ge \$MAX_RESTARTS ]; then
      echo "[\$(date '+%Y-%m-%d %H:%M:%S')] CRITICAL: Worker crashed \$RESTART_COUNT times, stopping daemon."
      exit 1
    fi
    echo "[\$(date '+%Y-%m-%d %H:%M:%S')] Worker exited (code=\$EXIT_CODE), restarting in \${RESTART_DELAY}s... (\$RESTART_COUNT/\$MAX_RESTARTS)"
    sleep \$RESTART_DELAY
  done
  exit 0
fi

cd "$INSTALL_DIR"
export PATH="$INSTALL_DIR/.venv/bin:\$HOME/.opencode/bin:\$HOME/.local/bin:\$HOME/.local/npm-global/bin:\$PATH"
# 容器/无 systemd 的 root 回退路径同样会撞 agent.py 的 root 引擎守卫（exit 78）：
# 按 euid 放行；非 root 安装不受影响（守卫只在 root 下生效）。
if [ "\$(id -u)" = "0" ]; then export GRAPE_ALLOW_ROOT_ENGINE=1; fi
set -a; . "$ENV_FILE"; set +a
exec "$VENV/bin/python" -m ${WORKER_MODULE}
EOF
  chmod +x "$START_SCRIPT"
  # 停掉旧 daemon（start-worker.sh 的 bash 进程），防止旧 daemon 重启被杀的 worker → 双 daemon 循环竞争
  # ⚠️ 匹配 start-worker.sh 进程：按脚本路径 + --no-daemon 模式（daemon 循环体特征）
  #    仅杀本安装目录的 start-worker.sh，避免误杀同机其他安装目录的 daemon
  _daemon_pat="$(printf '%s' "$START_SCRIPT" | sed 's/[.[\*^$]/\\&/g')"
  pkill -f "${_daemon_pat}.*--no-daemon" >/dev/null 2>&1 || true
  # 也杀 daemon 主进程（while true 循环体本身，--no-daemon 参数的父进程）
  pkill -f "$_daemon_pat" >/dev/null 2>&1 || true
  sleep 1
  # 停掉旧 worker——限定到本安装目录自己的实例（按 venv python 路径匹配）
  # ⚠️ 不能用裸 "python -m src.domains.runtime.engine.agent" 匹配：会误杀同机其他安装目录/其他用户
  #    的 worker（2026-10-08 实测：root 升级 /opt/grape 时误杀 liufei ~/grape-worker 的
  #    实例，恰好只因为 root 侧 cmdline 带 -u 才躲过反向误杀）。
  #    匹配本实例实际启动命令的前缀："$VENV/bin/python" ... -m src.domains.runtime.engine.agent
  #    （$VENV 展开为绝对路径，跨目录/跨用户天然隔离；pgrep -f 按子串匹配含路径）
  _pat="$(printf '%s' "$VENV/bin/python" | sed 's/[.[\*^$]/\\&/g')"
  # 停旧进程前采样两样东西（都必须在新进程启动之前）：
  #   * PID 快照：判活只认启动后新增的 PID，否则旧实例忽略 SIGTERM 慢退出时会被判成功；
  #   * 日志水位：注册判据只认水位之后的行。若在启动后（哪怕判活窗口之后）采样，新 worker
  #     在窗口内写下的注册行会被当成历史行，永远读不到 → 健康新版被误判未注册并回滚。
  _pids_before=" $(pgrep -f "${_pat}.*-m ${WORKER_MODULE_PAT}" 2>/dev/null | tr '\n' ' ')"
  _log_start="$(log_line_watermark "$INSTALL_DIR/data/worker.log")"
  pkill -f "${_pat}.*-m ${WORKER_MODULE_PAT}" >/dev/null 2>&1 || true
  sleep 1
  # 脱离终端启动（setsid + nohup，退出 ssh 不中断）
  setsid nohup "$START_SCRIPT" >> "$INSTALL_DIR/data/worker.log" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  # 启动验证 + 失败回滚（2026-10-08）：原逻辑只打一行"启动失败"就继续走完脚本退出，
  # 旧 agent 已被杀、新 agent 没起来 → worker 永久失联无人拉回（k8gtld1 实锤）。
  # 现在验证两件事：进程活着 + 30s 内注册成功（日志出现 registered）。
  # 注册成功才算升级完成；任一失败 → 恢复备份旧代码重启（旧版至少能连上 master）。
  # 判活窗口见文件顶部 _AGENT_START_POLL_*：固定 sleep 3 在慢盘/首装依赖场景会把已经
  # 正常起来的 worker 误判成失败并回滚。只认启动后新增的 PID。
  if ! _alive="$(wait_new_agent_alive "$_pids_before" "$_pat" "$WORKER_MODULE_PAT")"; then
    restore_upgrade_backup nohup || true
    fail "New agent failed to start (rollback attempted); logs: $INSTALL_DIR/data/worker.log"
  fi
  # [沙箱 worker 注册窗口 2026-11-04] 同 systemd 分支：沙箱 worker 探测完才注册，30s
  # 不够——扩到 240s；普通 worker 维持 30s。
  _reg_timeout_loops=10
  [ "$CRED_TYPE" = "sandbox" ] && _reg_timeout_loops=80
  # [进度输出 2026-11-05] 静默轮询最长 240s，用户观感=卡住（实例 52 用户实测反馈）。
  # 每 30s 打一行进度点。判据只认水位之后新写入的行（历史注册行不算）。
  if wait_runtime_registered "$INSTALL_DIR/data/worker.log" "$((_log_start + 1))" "$REGISTER_LOG" "$_reg_timeout_loops"; then
    log "Started in background and registered (pid $_alive), log: $INSTALL_DIR/data/worker.log"
  elif restore_upgrade_backup nohup; then
    log "New agent process alive but not registered within $((_reg_timeout_loops * 3))s -- rolled back (new version may not reach the master, e.g. a 502 window upgrade)"
  else
    log "New agent registration timed out and rollback failed, worker state abnormal; manual check: $INSTALL_DIR/data/worker.log"
  fi
fi

# ===== 7. 完成提示（#499: 引擎安装提示段整段删除——空引擎注册即成功，#471 C1
# 重注册周期自动拾起后续安装的引擎，无需引导用户当场装引擎）=====
if [ "$CRED_TYPE" = "sandbox" ]; then
  log "Sandbox worker install complete! Verify:"
  log "  Check log: $INSTALL_DIR/data/worker.log  -> should show cloud_worker registered + engine list"
  log "  Controller WebUI: runtimes page should show device $DEVICE; sandbox page should list this platform s sandboxes/templates"
  # [spec §7.1-③] 英文服务名提示(装的人当场知道怎么管理服务)
  log "Service installed: $SERVICE_NAME (systemctl status/restart/stop it)"
  log "  state dir: $CFG_DIR"
else
  log "Install complete! Verify registration:"
  log "  Check log: $INSTALL_DIR/data/worker.log  -> should show RuntimeAgent registered (with no engines it is 'registered: []', which is normal)"
  log "  Controller WebUI Workers page should show device $DEVICE"
fi
