#!/usr/bin/env bash
# Grape Worker 绿色一键安装脚本（普通用户免 root 接入）
#
# 用法（凭据申请页生成，token 即 rta_ 凭据）：
#   curl -fsSL https://<grape主控>/api/runtime/install | bash -s -- \
#     --token <rta_...> \
#     [--server https://<grape主控>] \
#     [--device worker-01] \
#     [--dir ~/grape] \
#     [--engine auto|opencode|hermes|none]
#
# 流程：
#   1. 从主控公开端点下载运行包 grape-worker.tar.gz（无需鉴权，包内无敏感信息）
#   2. 解压到普通用户家目录（默认 ~/grape，--dir 可改）
#   3. 建 venv + 安装最小依赖
#   4. 写入接入配置（凭据 token + 主系统地址，权限 600）
#   5. best-effort 安装执行引擎（Issue #471 C2：--engine auto 时有 npm 则装 opencode）
#      + codearts 目录信任预配置（无 TTY 不再因 folder-trust 闸门致命退出）
#   6. 后台启动 runtime-agent（免 root，nohup/setsid；root 时自动用 systemd）
#   7. 启动后自动注册到主控系统（RuntimeAgent registered；无引擎时也注册成功）
#
# 幂等：重复执行安全（复用已有目录/venv，仅更新代码与配置并重启）。
# 引擎（Issue #471 C2）：--engine auto 默认 best-effort 安装 opencode（需 npm）；
# 未装引擎时 worker 仍正常注册上线（空引擎为正常状态），后续装引擎无需重启自动注册。
set -euo pipefail

# ===== 参数解析 =====
TOKEN="${TOKEN:-}"
SERVER=""
DEVICE=""
INSTALL_DIR=""
ROOT_MODE=0
ENGINE="${ENGINE:-auto}"

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; shift 2 ;;
    --server)  SERVER="$2"; shift 2 ;;
    --device)  DEVICE="$2"; shift 2 ;;
    --dir)     INSTALL_DIR="$2"; shift 2 ;;
    --engine)  ENGINE="$2"; shift 2 ;;
    --root)    ROOT_MODE=1; shift ;;
    *) echo "Unknown argument: $1"; echo "Usage: --token <rta_...> [--server URL] [--device name] [--dir install-dir] [--engine auto|opencode|hermes|none] [--root]"; exit 1 ;;
  esac
done
case "$ENGINE" in
  auto|opencode|hermes|none) ;;
  *) echo "[install-worker] Error: --engine only supports auto|opencode|hermes|none (got: $ENGINE)" >&2; exit 1 ;;
esac

# ===== 工具函数 =====
log() { echo "[install-worker] $*"; }
fail() { echo "[install-worker] Error: $*" >&2; exit 1; }

if [ -z "$TOKEN" ]; then
  fail "Missing --token (rta_ credential, generated on the controller WebUI 'Workers' page)"
fi
if [ "$(id -u)" = "0" ]; then
  ROOT_MODE=1
fi
if [ -z "$SERVER" ]; then
  SERVER="https://grape.example.com"
  log "No --server given, using default $SERVER (if wrong, add --server https://<your-controller-domain>)"
fi
SERVER="${SERVER%/}"
# 设备名：hostname 小写，非字母数字替换为 -，清理首尾 -（hostname 输出含换行会产生尾随 -）
if [ -z "$DEVICE" ]; then
  DEVICE="worker-$(hostname | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9]/-/g; s/^-*//; s/-*$//')"
fi

# 安装目录：普通用户家目录（默认 ~/grape），root 模式默认 /opt/grape
if [ -z "$INSTALL_DIR" ]; then
  if [ "$ROOT_MODE" = "1" ]; then
    INSTALL_DIR="/opt/grape"
  else
    INSTALL_DIR="$HOME/grape"
  fi
fi
INSTALL_DIR="${INSTALL_DIR%/}"
VENV="$INSTALL_DIR/.venv"

# 配置目录与文件（普通用户：~/.grape/runtime.env；root：/etc/grape/runtime.env）
if [ "$ROOT_MODE" = "1" ]; then
  CFG_DIR="/etc/grape"
  START_SCRIPT="/opt/grape/start-worker.sh"
else
  CFG_DIR="$HOME/.grape"
  START_SCRIPT="$INSTALL_DIR/start-worker.sh"
fi
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
  fail "Downloaded worker package is corrupt (gzip check failed), please retry"
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
  log "Backed up current code to $_upgrade_backup_dir (auto-rollback on upgrade failure)"
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
  if "$VENV/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.config; import src.runtime.agent" >/dev/null 2>&1; then
    log "New code import smoke check passed"
    return 0
  else
    _smoke_err="$("$VENV/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.config; import src.runtime.agent" 2>&1 | tail -1)"
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
  log "!!! New agent failed to start, rolling back to pre-upgrade code and restarting old agent !!!"
  if [ ! -d "$_upgrade_backup_dir/src" ]; then
    log "Rollback failed: no backup ($_upgrade_backup_dir missing, probably a fresh install) -- worker will stay offline, manual intervention required"
    return 1
  fi
  rm -rf "$INSTALL_DIR/src"
  cp -a "$_upgrade_backup_dir/src" "$INSTALL_DIR/src"
  [ -f "$_upgrade_backup_dir/VERSION" ] && cp -a "$_upgrade_backup_dir/VERSION" "$INSTALL_DIR/VERSION"
  log "Restored old code ($(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo 'unknown'))"
  if [ "$_mode" = "systemd" ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart runtime-agent >/dev/null 2>&1 || true
  else
    # 2026-10-08 follow-up：先杀残留的新进程再起旧版。注册超时路径（启动验证 30s
    # 未 registered）调用本函数时，新 agent 进程可能仍存活——若不限域 pkill，
    # 旧版起成功后新旧两进程并存，残留新进程可能反向注册击穿回滚。
    # 限域模式与第 6 步停旧实例一致：按本安装目录 venv 绝对路径匹配，不误杀同机
    # 其他安装/其他用户的 worker。
    _rollback_pat="$(printf '%s' "$VENV/bin/python" | sed 's/[.[\*^$]/\\&/g')"
    pkill -f "${_rollback_pat}.*-m src\.runtime\.agent" >/dev/null 2>&1 || true
    sleep 1
    setsid nohup "$START_SCRIPT" >> "$INSTALL_DIR/data/worker.log" 2>&1 < /dev/null &
    disown 2>/dev/null || true
  fi
  sleep 5
  if pgrep -f "$(printf '%s' "$VENV/bin/python" | sed 's/[.[\*^$]/\\&/g').*-m src\.runtime\.agent" >/dev/null 2>&1; then
    log "Rollback OK: old agent restored and running"
    return 0
  fi
  log "Agent still not running after rollback -- worker offline, manual intervention needed (log: $INSTALL_DIR/data/worker.log / journalctl -u runtime-agent)"
  return 1
}

# 校验包内 SHA256SUMS：包内路径带 grape-worker/ 前缀，解压 --strip-components=1 后
# 实际文件在安装目录根，校验时去掉前缀（存在则校验；缺失仅告警）
if [ -f "$INSTALL_DIR/SHA256SUMS" ]; then
  if ( cd "$INSTALL_DIR" && sed 's|  grape-worker/|  |' SHA256SUMS | sha256sum -c >/dev/null 2>&1 ); then
    log "Worker package content checksum verified (SHA256SUMS)"
  else
    log "Warning: worker package content checksum FAILED, make sure the source is trusted"
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
if [ -f "$INSTALL_DIR/requirements-worker.txt" ]; then
  # pip 镜像：默认清华（国内网络 pypi.org 直连常超时），可用 PIP_INDEX_URL 或 --pip-index 覆盖
  PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  # 优先离线安装：包内已带依赖 wheels（grape-worker/wheels/），免网络下载
  if [ -d "$INSTALL_DIR/wheels" ] && ls "$INSTALL_DIR/wheels"/*.whl >/dev/null 2>&1; then
    log "Installing deps offline (bundled wheels: $(ls "$INSTALL_DIR/wheels"/*.whl | wc -l) files, no network needed)"
    if "$PIP_CMD" install --quiet --no-index --find-links "$INSTALL_DIR/wheels" \
        -r "$INSTALL_DIR/requirements-worker.txt"; then
      log "Deps installed offline (wheels auto-matched to arch $TARGET_ARCH by pip)"
    else
      log "Offline install failed (platform wheel not bundled or python version mismatch), falling back to online..."
      if ! "$PIP_CMD" install --quiet -i "$PIP_INDEX" --retries 3 --timeout 30 \
          -r "$INSTALL_DIR/requirements-worker.txt"; then
        log "Dependency install failed. Check network and retry, or specify a reachable mirror:"
        log "  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash -c 'curl -fsSL $SERVER/api/runtime/install | bash -s -- --token <token> --server $SERVER'"
        fail "Dependency install failed (worker cannot start without aiohttp): retry this install command, or set PIP_INDEX_URL to a reachable pip mirror"
      fi
    fi
  else
    log "Installing deps online (mirror: $PIP_INDEX, first run takes minutes)"
    if ! "$PIP_CMD" install --quiet -i "$PIP_INDEX" --retries 3 --timeout 30 \
        -r "$INSTALL_DIR/requirements-worker.txt"; then
      fail "Dependency install failed (worker cannot start without aiohttp): retry this install command, or set PIP_INDEX_URL to a reachable pip mirror"
    fi
  fi
fi

# ===== 4.5 安装 grape CLI（独立项目 grape-cli；优先随包离线 wheel，失败回退在线）=====
_install_grape_online() {
  "$PIP_CMD" install -q --no-cache-dir \
      git+https://gitcode.com/grape-dev/grape-cli.git 2>/dev/null \
    && chmod +x "$VENV/bin/grape" 2>/dev/null || true \
    && log "grape CLI installed (online): $VENV/bin/grape" \
    || log "Warning: grape-cli online install failed, no 'grape' available in the agent for now; worker keeps running"; return 0
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
# 保留已有 HERMES_CUSTOM_* 等 API key（重装不应覆盖本地配置）：
# ① 备份旧文件中的 HERMES_CUSTOM 行 ② cat 覆盖 RUNTIME_*  ③ 追加回 HERMES_CUSTOM 行
umask 077
_PRESERVED_KEYS=""
if [ -f "$ENV_FILE" ]; then
  _PRESERVED_KEYS=$(grep '^HERMES_CUSTOM' "$ENV_FILE" 2>/dev/null || true)
fi
cat > "$ENV_FILE" <<EOF
RUNTIME_TOKEN=$TOKEN
RUNTIME_SERVER_URL=$SERVER
RUNTIME_DEVICE_NAME=$DEVICE
RUNTIME_MAX_CONCURRENT_TASKS=20
RUNTIME_POLL_INTERVAL=3
RUNTIME_HEARTBEAT_INTERVAL=15
EOF
# 追加旧配置中保留的 HERMES_CUSTOM keys（防重装丢 key → opencode 401）
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
    log "Installing bwrap (bubblewrap) + lifting AppArmor restriction (setuid)..."
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -qq >/dev/null 2>&1 || true
      apt-get install -y -qq bubblewrap >/dev/null 2>&1 || log "Warning: apt install of bwrap failed"
      if command -v bwrap >/dev/null 2>&1; then
        chmod u+s /usr/bin/bwrap || log "Warning: failed to set bwrap setuid bit"
        dpkg-statoverride --add root root 4755 /usr/bin/bwrap >/dev/null 2>&1 || true
      fi
    elif command -v yum >/dev/null 2>&1; then
      yum install -y -q bubblewrap >/dev/null 2>&1 || log "Warning: yum install of bwrap failed"
      command -v bwrap >/dev/null 2>&1 && chmod u+s /usr/bin/bwrap
    else
      log "Warning: no apt/yum found, cannot auto-install bwrap"
    fi
    if _bwrap_ok; then
      log "bwrap installed (agent filesystem isolation lock ready)"
    else
      log "Warning: bwrap still unusable (AppArmor strict), filesystem lock downgraded to off (worker runs fine)"
    fi
  else
    log "Note: no usable bwrap detected (needed by the agent filesystem isolation lock). To enable it, re-run this script as root (add --root), or manually run:"
    log "  sudo apt-get install -y bubblewrap && sudo dpkg-statoverride --add root root 4755 /usr/bin/bwrap"
    log "  (without bwrap the worker still runs normally, only the filesystem isolation lock is off)"
  fi
}
install_bwrap

# ===== 5.6 安装 sshpass（SRE 远程部署密码认证；root 自动装）=====
if [ "$ROOT_MODE" = "1" ]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    apt-get install -y -qq sshpass 2>/dev/null || yum install -y -q sshpass 2>/dev/null || true
    log "sshpass installed (SRE remote deploy password auth)"
  fi
fi

# ===== 5.7 best-effort 安装执行引擎（Issue #471 C2）=====
# --engine auto（默认）：未装 opencode 且有 npm → npm install -g opencode-ai；
# --engine opencode/hermes：显式指定安装对应引擎；--engine none：跳过。
# 安装失败不阻断（worker 无引擎也正常注册上线，后续装好引擎无需重启自动注册）。
install_engine() {
  if [ "$ENGINE" = "none" ]; then
    log "Skipping engine install (--engine none; worker registers fine without engines)"
    return 0
  fi
  # auto：已有任一引擎即跳过；显式指定：仅检查对应引擎
  if [ "$ENGINE" = "auto" ]; then
    if command -v opencode >/dev/null 2>&1 || command -v hermes >/dev/null 2>&1; then
      log "Execution engine detected (opencode/hermes), skipping install"
      return 0
    fi
    _want_oc=1; _want_hermes=0
  elif [ "$ENGINE" = "opencode" ]; then
    if command -v opencode >/dev/null 2>&1; then
      log "opencode engine already present, skipping install"
      return 0
    fi
    _want_oc=1; _want_hermes=0
  else
    if command -v hermes >/dev/null 2>&1 || [ -x "$VENV/bin/hermes" ]; then
      log "hermes engine already present, skipping install"
      return 0
    fi
    _want_oc=0; _want_hermes=1
  fi
  if [ "$_want_oc" = "1" ]; then
    if command -v npm >/dev/null 2>&1; then
      log "Installing opencode engine (npm install -g opencode-ai, takes ~1-3 minutes)..."
      if timeout 300 npm install -g opencode-ai >/dev/null 2>&1 && command -v opencode >/dev/null 2>&1; then
        log "opencode engine installed"
        return 0
      fi
      log "Warning: opencode install failed (network/mirror issue). Worker still registers; install it later manually if needed: npm install -g opencode-ai"
    else
      log "Note: npm not found, cannot auto-install opencode. Worker still registers; after a later manual install no restart is needed:"
      log "  Install Node.js then run npm install -g opencode-ai (see help/usage/faq.md)"
    fi
  fi
  if [ "$_want_hermes" = "1" ]; then
    log "Installing hermes engine (pip install 'hermes[acp]')..."
    if timeout 300 "$VENV/bin/pip" install -q 'hermes[acp]' >/dev/null 2>&1 && [ -x "$VENV/bin/hermes" ]; then
      log "hermes engine installed ($VENV/bin/hermes)"
      return 0
    fi
    log "Warning: hermes install failed. Worker still registers; install it later manually if needed: pip install 'hermes[acp]'"
  fi
  return 0
}
install_engine

# ===== 5.8 codearts 目录信任预配置（Issue #471 C2）=====
# codearts CLI 的 folder-trust 闸门在无 TTY（systemd/nohup）环境下对未信任
# 目录直接 exit 1。把 worker 安装目录与 codearts tasks 目录合并进
# ~/.codeartsdoer/cli-data/settings.json 的 trustedFolders（保留已有配置）。
# ⚠️ Issue #471 QA S4：合并语义（保留 enabled 标志、trustedFolders 并入、
# 保留其它顶层键）与 src/runtime/codearts.write_trust_settings 逐条对齐——
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
from src.runtime.codearts import write_trust_settings
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
  SERVICE_FILE="/etc/systemd/system/runtime-agent.service"
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Grape RuntimeAgent (edge, $DEVICE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV/bin/python -m src.runtime.agent
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$INSTALL_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=$ENV_FILE

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable runtime-agent >/dev/null 2>&1 || true
  systemctl restart runtime-agent || { restore_upgrade_backup systemd || true; fail "New agent failed to start (rollback attempted); check journalctl -u runtime-agent"; }
  # 启动验证 + 失败回滚（2026-10-14 五台 worker 心跳全断事故补齐）：
  # `systemctl restart` 对 Type=simple 单元在进程 spawn 即返回成功，agent 随后
  # 崩溃（如 import 失败）它感知不到——nohup 分支自 2026-10-08 起有 3s+30s
  # 注册验证，systemd 分支此前缺失，导致坏包升级后 systemd 无限重启 6 小时。
  # 现验证两件事：3s 后单元仍 active（非 auto-restart 僵尸循环）且 journal
  # 无 Traceback；30s 内日志出现 registered。任一失败 → 回滚旧代码重启。
  sleep 3
  _unit_state="$(systemctl show runtime-agent --property=ActiveState --value 2>/dev/null || true)"
  _unit_sub="$(systemctl show runtime-agent --property=SubState --value 2>/dev/null || true)"
  if [ "$_unit_state" != "active" ] || [ "$_unit_sub" = "auto-restart" ] \
     || journalctl -u runtime-agent --since "-10 seconds" --no-pager 2>/dev/null | grep -q "Traceback"; then
    restore_upgrade_backup systemd || true
    fail "New agent crashed on startup (active=$_unit_state sub=$_unit_sub, rollback attempted); logs: journalctl -u runtime-agent"
  fi
  _reg_ok=""
  for _i in $(seq 1 10); do
    if journalctl -u runtime-agent --since "-$((30 + $_i * 3)) seconds" --no-pager 2>/dev/null | grep -q "RuntimeAgent registered"; then
      _reg_ok=1
      break
    fi
    sleep 3
  done
  if [ -n "$_reg_ok" ]; then
    log "Started via systemd and registered (runtime-agent)"
  elif restore_upgrade_backup systemd; then
    log "New agent not registered within 30s -- rolled back (new version may not reach master)"
  else
    log "New agent registration timed out and rollback failed, worker state abnormal; manual check: journalctl -u runtime-agent"
  fi
else
  # ---- 普通用户 或 容器环境（无 systemd）：nohup/setsid 后台启动 ----
  if [ "$ROOT_MODE" = "1" ] && ! _has_systemd; then
    log "root mode but systemd unavailable (container?), falling back to nohup/setsid"
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
set -a; . "$ENV_FILE"; set +a
exec "$VENV/bin/python" -m src.runtime.agent
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
  # ⚠️ 不能用裸 "python -m src.runtime.agent" 匹配：会误杀同机其他安装目录/其他用户
  #    的 worker（2026-10-08 实测：root 升级 /opt/grape 时误杀 liufei ~/grape-worker 的
  #    实例，恰好只因为 root 侧 cmdline 带 -u 才躲过反向误杀）。
  #    匹配本实例实际启动命令的前缀："$VENV/bin/python" ... -m src.runtime.agent
  #    （$VENV 展开为绝对路径，跨目录/跨用户天然隔离；pgrep -f 按子串匹配含路径）
  _pat="$(printf '%s' "$VENV/bin/python" | sed 's/[.[\*^$]/\\&/g')"
  pkill -f "${_pat}.*-m src\.runtime\.agent" >/dev/null 2>&1 || true
  sleep 1
  # 脱离终端启动（setsid + nohup，退出 ssh 不中断）
  setsid nohup "$START_SCRIPT" >> "$INSTALL_DIR/data/worker.log" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  # 启动验证 + 失败回滚（2026-10-08）：原逻辑只打一行"启动失败"就继续走完脚本退出，
  # 旧 agent 已被杀、新 agent 没起来 → worker 永久失联无人拉回（k8gtld1 实锤）。
  # 现在验证两件事：进程活着 + 30s 内注册成功（日志出现 registered）。
  # 注册成功才算升级完成；任一失败 → 恢复备份旧代码重启（旧版至少能连上 master）。
  sleep 3
  if ! pgrep -f "${_pat}.*-m src\.runtime\.agent" >/dev/null 2>&1; then
    restore_upgrade_backup nohup || true
    fail "New agent failed to start (rollback attempted); log: $INSTALL_DIR/data/worker.log"
  fi
  _reg_ok=""
  for _i in $(seq 1 10); do
    if tail -n 40 "$INSTALL_DIR/data/worker.log" 2>/dev/null | grep -q "RuntimeAgent registered"; then
      _reg_ok=1
      break
    fi
    sleep 3
  done
  if [ -n "$_reg_ok" ]; then
    log "Started in background and registered (pid $(pgrep -f "${_pat}.*-m src\.runtime\.agent" | head -1)), log: $INSTALL_DIR/data/worker.log"
  elif restore_upgrade_backup nohup; then
    log "New agent alive but not registered within 30s -- rolled back (new version may not reach master, e.g. upgrade during a 502 window)"
  else
    log "New agent registration timed out and rollback failed, worker state abnormal; manual check: $INSTALL_DIR/data/worker.log"
  fi
fi

log "Install complete! Verify registration:"
log "  Check log: $INSTALL_DIR/data/worker.log  -> should show RuntimeAgent registered (with no engines it is 'registered: []', which is normal)"
log "  Controller WebUI Workers page should show device $DEVICE"
