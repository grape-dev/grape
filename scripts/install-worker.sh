#!/usr/bin/env bash
# Grape Worker 绿色一键安装脚本（普通用户免 root 接入）
#
# 用法（凭据申请页生成，token 即 rta_ 凭据）：
#   curl -fsSL https://<grape主控>/api/runtime/install | bash -s -- \
#     --token <rta_...> \
#     [--server https://<grape主控>] \
#     [--device worker-01] \
#     [--dir ~/grape]
#
# 流程：
#   1. 从主控公开端点下载运行包 grape-worker.tar.gz（无需鉴权，包内无敏感信息）
#   2. 解压到普通用户家目录（默认 ~/grape，--dir 可改）
#   3. 建 venv + 安装最小依赖
#   4. 写入接入配置（凭据 token + 主系统地址，权限 600）
#   5. 后台启动 runtime-agent（免 root，nohup/setsid；root 时自动用 systemd）
#   6. 启动后自动注册到主控系统（RuntimeAgent registered）
#
# 幂等：重复执行安全（复用已有目录/venv，仅更新代码与配置并重启）。
# 不安装执行引擎（opencode 等由用户自行安装，worker 启动自动发现注册）。
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
    *) echo "未知参数: $1"; echo "用法: --token <rta_...> [--server URL] [--device 名] [--dir 安装目录] [--root]"; exit 1 ;;
  esac
done

# ===== 工具函数 =====
log() { echo "[install-worker] $*"; }
fail() { echo "[install-worker] 错误: $*" >&2; exit 1; }

if [ -z "$TOKEN" ]; then
  fail "缺少 --token（rta_ 凭据，在主控 WebUI「Workers」页生成）"
fi
if [ "$(id -u)" = "0" ]; then
  ROOT_MODE=1
fi
if [ -z "$SERVER" ]; then
  SERVER="https://grape.example.com"
  log "未指定 --server，使用默认 $SERVER（如不对请加 --server https://<你的主控域名>）"
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

log "安装目录: $INSTALL_DIR | 设备名: $DEVICE | 主控: $SERVER | 模式: $([ "$ROOT_MODE" = 1 ] && echo root/systemd || echo 用户/nohup)"

# ===== 1. 准备 Python >= 3.10 =====
# 优先级：系统 python3.12/3.11/3.10/python3 → 包内自带解释器（python/ 目录，standalone 免编译）
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
  log "未找到系统 Python>=3.10，将尝试使用包内自带解释器（解压后自动探测）"
fi

# ===== 2. 下载运行包（公开端点，无需鉴权）=====
PKG_URL="$SERVER/api/runtime/grape-worker.tar.gz"
TMP_PKG="/tmp/grape-worker-$$.tar.gz"
log "下载运行包: $PKG_URL"
if ! curl -fsSL --connect-timeout 15 "$PKG_URL" -o "$TMP_PKG"; then
  rm -f "$TMP_PKG"
  fail "下载运行包失败: $PKG_URL（请确认主控地址/网络可达）"
fi
if ! gzip -t "$TMP_PKG" 2>/dev/null; then
  rm -f "$TMP_PKG"
  fail "下载的运行包损坏（gzip 校验失败），请重试"
fi

# ===== 3. 解压到安装目录（幂等）=====
mkdir -p "$INSTALL_DIR"
log "解压运行包到 $INSTALL_DIR"
tar -xzf "$TMP_PKG" -C "$INSTALL_DIR" --strip-components=1 || fail "解压运行包失败"
rm -f "$TMP_PKG"

# 校验包内 SHA256SUMS：包内路径带 grape-worker/ 前缀，解压 --strip-components=1 后
# 实际文件在安装目录根，校验时去掉前缀（存在则校验；缺失仅告警）
if [ -f "$INSTALL_DIR/SHA256SUMS" ]; then
  if ( cd "$INSTALL_DIR" && sed 's|  grape-worker/|  |' SHA256SUMS | sha256sum -c >/dev/null 2>&1 ); then
    log "运行包内容校验通过（SHA256SUMS）"
  else
    log "警告: 运行包内容校验未通过，请确认来源可信"
  fi
fi
mkdir -p "$CFG_DIR"
if [ -f "$INSTALL_DIR/VERSION" ]; then
  cp "$INSTALL_DIR/VERSION" "$VERSION_FILE" 2>/dev/null || true
  log "Worker 版本: $(cat "$VERSION_FILE" 2>/dev/null || echo unknown)"
fi

# data/ 运行时目录
mkdir -p "$INSTALL_DIR/data"

# ===== 3.5 包内解释器补探测（解压后）=====
if [ -z "$PY" ]; then
  if [ -x "$INSTALL_DIR/python/bin/python3" ]; then
    PY="$INSTALL_DIR/python/bin/python3"
    log "使用包内自带 Python（standalone，免系统安装）"
  fi
fi
if [ -z "$PY" ]; then
  fail "未找到 Python>=3.10，请先安装（apt/yum install python3.12 python3.12-venv）后重试，或重新下载安装包（应含 python/ 目录）"
fi
log "使用 Python: $PY ($($PY --version 2>&1 | awk '{print $2}'))"

# ===== 4. 建 venv + 最小依赖 =====
if [ ! -x "$VENV/bin/python" ]; then
  log "创建 venv ($PY)"
  "$PY" -m venv "$VENV"
fi
# 确保 venv 内有 pip（某些系统 ensurepip 缺失导致 venv 无 pip）
# 某些旧 venv 只有 pip3 没有 pip 软链接，统一用 PIP_CMD 变量
PIP_CMD="$VENV/bin/pip"
if [ ! -x "$PIP_CMD" ]; then
  if [ -x "$VENV/bin/pip3" ]; then
    log "venv 无 pip（有 pip3），使用 pip3 作为替代"
    PIP_CMD="$VENV/bin/pip3"
    # 尝试修复 pip 软链接（不影响 pip3 使用）
    "$VENV/bin/python" -m pip install --upgrade pip 2>/dev/null || true
  else
    log "venv 缺 pip, 手动安装 ensurepip..."
    "$VENV/bin/python" -m ensurepip --upgrade 2>/dev/null || \
      "$VENV/bin/python" -m pip install --upgrade pip 2>/dev/null || \
      curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$VENV/bin/python" 2>/dev/null || true
    # 再次检查
    if [ ! -x "$PIP_CMD" ] && [ -x "$VENV/bin/pip3" ]; then
      PIP_CMD="$VENV/bin/pip3"
    fi
  fi
fi
if [ -f "$INSTALL_DIR/requirements-worker.txt" ]; then
  # pip 镜像：默认清华（国内网络 pypi.org 直连常超时），可用 PIP_INDEX_URL 或 --pip-index 覆盖
  PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  # 优先离线安装：包内已带依赖 wheels（grape-worker/wheels/），免网络下载
  if [ -d "$INSTALL_DIR/wheels" ] && ls "$INSTALL_DIR/wheels"/*.whl >/dev/null 2>&1; then
    log "离线安装依赖（包内 wheels: $(ls "$INSTALL_DIR/wheels"/*.whl | wc -l) 个，免网络）"
    if "$PIP_CMD" install --quiet --no-index --find-links "$INSTALL_DIR/wheels" \
        -r "$INSTALL_DIR/requirements-worker.txt"; then
      log "依赖离线安装完成"
    else
      log "离线安装失败（可能平台/Python 版本不匹配），回退在线安装..."
      if ! "$PIP_CMD" install --quiet -i "$PIP_INDEX" --retries 3 --timeout 30 \
          -r "$INSTALL_DIR/requirements-worker.txt"; then
        log "依赖安装失败。请检查网络后重试，或指定可用镜像："
        log "  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash -c 'curl -fsSL $SERVER/api/runtime/install | bash -s -- --token <token> --server $SERVER'"
        fail "依赖安装失败（aiohttp 未装好 worker 无法启动）：请重试本安装命令，或设置 PIP_INDEX_URL 环境变量指定可用 pip 镜像"
      fi
    fi
  else
    log "包内无离线 wheels，在线安装依赖（镜像: $PIP_INDEX，首次需数分钟）"
    if ! "$PIP_CMD" install --quiet -i "$PIP_INDEX" --retries 3 --timeout 30 \
        -r "$INSTALL_DIR/requirements-worker.txt"; then
      fail "依赖安装失败（aiohttp 未装好 worker 无法启动）：请重试本安装命令，或设置 PIP_INDEX_URL 环境变量指定可用 pip 镜像"
    fi
  fi
fi

# ===== 4.5 安装 grape CLI（独立项目 grape-cli；优先随包离线 wheel，失败回退在线）=====
_install_grape_online() {
  "$PIP_CMD" install -q --no-cache-dir \
      git+https://gitcode.com/grape-dev/grape-cli.git 2>/dev/null \
    && chmod +x "$VENV/bin/grape" 2>/dev/null || true \
    && log "grape CLI 已装（在线）: $VENV/bin/grape" \
    || log "警告: grape-cli 在线安装失败，agent 内暂无 \`grape\`；不影响 worker 运行"; return 0
}
if ls "$INSTALL_DIR/wheels"/grape-*.whl >/dev/null 2>&1; then
  if "$PIP_CMD" install -q --no-index --find-links "$INSTALL_DIR/wheels" grape 2>/dev/null; then
    chmod +x "$VENV/bin/grape" 2>/dev/null || true
    log "grape CLI 已装（离线 wheel，随包）: $VENV/bin/grape"
  else
    log "警告: 离线 grape wheel 安装失败，回退在线..."
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
log "接入配置已写入 $ENV_FILE（token + 主控地址）"

# ===== 5.5 安装 bwrap（agent 文件系统隔离锁；root 自动装，普通用户提示）=====
# bwrap 用于给 agent 子进程套文件系统隔离（阶段二）。已装且可用 → 跳过；
# root 模式 → 自动 apt install + setuid；普通用户 → 提示（无锁运行，不影响 worker）。
# AppArmor（Ubuntu 24.04）限制非特权 userns，用 setuid 位绕过（Debian 官方推荐）。
_bwrap_ok() {
  command -v bwrap >/dev/null 2>&1 && bwrap --ro-bind / / --proc /proc --dev /dev sh -c 'exit 0' 2>/dev/null
}
install_bwrap() {
  if _bwrap_ok; then
    log "bwrap 已可用（agent 文件系统隔离锁就绪）"
    return 0
  fi
  if [ "$ROOT_MODE" = "1" ]; then
    log "安装 bwrap（bubblewrap）+ 解除 AppArmor 限制（setuid）..."
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -qq >/dev/null 2>&1 || true
      apt-get install -y -qq bubblewrap >/dev/null 2>&1 || log "警告: apt 安装 bwrap 失败"
      if command -v bwrap >/dev/null 2>&1; then
        chmod u+s /usr/bin/bwrap || log "警告: 设置 bwrap setuid 位失败"
        dpkg-statoverride --add root root 4755 /usr/bin/bwrap >/dev/null 2>&1 || true
      fi
    elif command -v yum >/dev/null 2>&1; then
      yum install -y -q bubblewrap >/dev/null 2>&1 || log "警告: yum 安装 bwrap 失败"
      command -v bwrap >/dev/null 2>&1 && chmod u+s /usr/bin/bwrap
    else
      log "警告: 未找到 apt/yum，无法自动装 bwrap"
    fi
    if _bwrap_ok; then
      log "bwrap 安装成功（agent 文件系统隔离锁就绪）"
    else
      log "警告: bwrap 仍不可用（AppArmor 严格限制），文件系统锁降级关闭（worker 照常运行）"
    fi
  else
    log "提示: 未检测到可用的 bwrap（agent 文件系统隔离锁需要）。如需启用，请以 root 重跑本脚本（加 --root），或手动执行："
    log "  sudo apt-get install -y bubblewrap && sudo dpkg-statoverride --add root root 4755 /usr/bin/bwrap"
    log "  （未装 bwrap 时 worker 照常运行，仅文件系统隔离锁关闭）"
  fi
}
install_bwrap

# ===== 5.6 安装 sshpass（SRE 远程部署密码认证；root 自动装）=====
if [ "$ROOT_MODE" = "1" ]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    apt-get install -y -qq sshpass 2>/dev/null || yum install -y -q sshpass 2>/dev/null || true
    log "sshpass 已安装（SRE 远程部署密码认证）"
  fi
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
  systemctl restart runtime-agent || fail "服务启动失败，请查看 journalctl -u runtime-agent"
  log "已通过 systemd 启动（runtime-agent）"
else
  # ---- 普通用户 或 容器环境（无 systemd）：nohup/setsid 后台启动 ----
  if [ "$ROOT_MODE" = "1" ] && ! _has_systemd; then
    log "root 模式但 systemd 不可用（容器环境？），回退到 nohup/setsid 启动"
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
  # 停掉旧实例——限定到本安装目录自己的实例（按 venv python 路径匹配）
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
  sleep 3
  if pgrep -f "${_pat}.*-m src\.runtime\.agent" >/dev/null 2>&1; then
    log "已后台启动（pid $(pgrep -f "${_pat}.*-m src\.runtime\.agent" | head -1)），日志: $INSTALL_DIR/data/worker.log"
  else
    log "启动失败，请查看日志: $INSTALL_DIR/data/worker.log"
  fi
fi

# ===== 7. 执行引擎提示 =====
_engine_hint=""
if ! command -v opencode >/dev/null 2>&1; then
  _engine_hint="  npm install -g opencode-ai（opencode 引擎）"
fi
if ! command -v hermes >/dev/null 2>&1; then
  _engine_hint="${_engine_hint:+$_engine_hint\n}  pip install hermes（hermes 引擎）"
fi
if [ -n "$_engine_hint" ]; then
  log "未检测到执行引擎。请自行安装后重启 worker："
  printf "%b\n" "$_engine_hint"
  if [ "$ROOT_MODE" = "1" ]; then log "  systemctl restart runtime-agent"; else log "  $START_SCRIPT"; fi
  log "（引擎类型不限定，worker 启动会自动发现本机已装引擎并注册）"
fi

log "安装完成！验证注册："
log "  查看日志: $INSTALL_DIR/data/worker.log  → 应看到 RuntimeAgent registered: [...]"
log "  主控 WebUI Workers 页应出现设备 $DEVICE"
