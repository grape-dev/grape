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
TOKEN=""
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
if [ -z "$DEVICE" ]; then
  DEVICE="worker-$(hostname | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '-')"
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
  fail "未找到 Python>=3.10，请先安装（apt/yum install python3.12 python3.12-venv）后重试"
fi
log "使用 Python: $PY ($($PY --version 2>&1 | awk '{print $2}'))"

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

# 校验包内 SHA256SUMS（存在则校验；缺失仅告警）
if [ -f "$INSTALL_DIR/SHA256SUMS" ]; then
  ( cd "$INSTALL_DIR" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) || log "警告: 运行包内容校验未通过，请确认来源可信"
fi
mkdir -p "$CFG_DIR"
if [ -f "$INSTALL_DIR/VERSION" ]; then
  cp "$INSTALL_DIR/VERSION" "$VERSION_FILE" 2>/dev/null || true
  log "Worker 版本: $(cat "$VERSION_FILE" 2>/dev/null || echo unknown)"
fi

# data/ 运行时目录
mkdir -p "$INSTALL_DIR/data"

# ===== 4. 建 venv + 最小依赖 =====
if [ ! -x "$VENV/bin/python" ]; then
  log "创建 venv ($PY)"
  "$PY" -m venv "$VENV"
fi
if [ -f "$INSTALL_DIR/requirements-worker.txt" ]; then
  log "安装依赖 requirements-worker.txt（首次需数分钟）"
  "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements-worker.txt" || log "警告: 依赖安装未完全成功，请检查网络/镜像"
fi

# ===== 5. 写入接入配置（凭据 + 主系统地址，权限 600）=====
umask 077
cat > "$ENV_FILE" <<EOF
RUNTIME_TOKEN=$TOKEN
RUNTIME_SERVER_URL=$SERVER
RUNTIME_DEVICE_NAME=$DEVICE
RUNTIME_MAX_CONCURRENT_TASKS=20
RUNTIME_POLL_INTERVAL=3
RUNTIME_HEARTBEAT_INTERVAL=15
EOF
chmod 600 "$ENV_FILE" 2>/dev/null || true
log "接入配置已写入 $ENV_FILE（token + 主控地址）"

# ===== 6. 生成启动脚本并启动 =====
if [ "$ROOT_MODE" = "1" ]; then
  # ---- root：systemd 服务 ----
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
EnvironmentFile=$ENV_FILE

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable runtime-agent >/dev/null 2>&1 || true
  systemctl restart runtime-agent || fail "服务启动失败，请查看 journalctl -u runtime-agent"
  log "已通过 systemd 启动（runtime-agent）"
else
  # ---- 普通用户：nohup/setsid 后台启动（免 root）----
  cat > "$START_SCRIPT" <<EOF
#!/usr/bin/env bash
# Grape Worker 启动脚本（由 install-worker.sh 生成，可重复执行）
cd "$INSTALL_DIR"
set -a; . "$ENV_FILE"; set +a
exec "$VENV/bin/python" -m src.runtime.agent
EOF
  chmod +x "$START_SCRIPT"
  # 停掉旧实例（按 start 脚本匹配，避免误杀）
  pkill -f "python -m src.runtime.agent" >/dev/null 2>&1 || true
  sleep 1
  # 脱离终端启动（setsid + nohup，退出 ssh 不中断）
  setsid nohup "$START_SCRIPT" >> "$INSTALL_DIR/data/worker.log" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  sleep 3
  if pgrep -f "python -m src.runtime.agent" >/dev/null 2>&1; then
    log "已后台启动（pid $(pgrep -f 'python -m src.runtime.agent' | head -1)），日志: $INSTALL_DIR/data/worker.log"
  else
    log "启动失败，请查看日志: $INSTALL_DIR/data/worker.log"
  fi
fi

# ===== 7. 执行引擎提示 =====
if ! command -v opencode >/dev/null 2>&1; then
  log "未检测到执行引擎（opencode 等）。请自行安装后重启 worker："
  log "  npm install -g opencode-ai"
  if [ "$ROOT_MODE" = "1" ]; then log "  systemctl restart runtime-agent"; else log "  $START_SCRIPT"; fi
  log "（引擎类型不限定 opencode，worker 启动会自动发现本机已装引擎）"
fi

log "安装完成！验证注册："
log "  查看日志: $INSTALL_DIR/data/worker.log  → 应看到 RuntimeAgent registered: [...]"
log "  主控 WebUI Workers 页应出现设备 $DEVICE"
