#!/usr/bin/env bash
# Grape Worker 一键安装脚本（见 docs/new-version2/worker-install-optimization-plan.md）
#
# 用法：
#   curl -sSL https://grape.example.com/api/runtime/install | bash -s \
#     --token <rta_...> \
#     [--server https://grape.example.com] \
#     [--device worker-dev1] \
#     [--repo https://gitcode.com/Jason7602/grape.git]
#
# 幂等：重复执行安全（已存在目录/venv/service 则复用）。
set -euo pipefail

# ===== 参数解析 =====
TOKEN=""
SERVER="https://grape.example.com"
DEVICE=""
REPO="https://gitcode.com/Jason7602/grape.git"
BRANCH="new-version2"

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; shift 2 ;;
    --server)  SERVER="$2"; shift 2 ;;
    --device)  DEVICE="$2"; shift 2 ;;
    --repo)    REPO="$2"; shift 2 ;;
    --branch)  BRANCH="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [ -z "$TOKEN" ]; then
  echo "错误: 缺少 --token（凭据 rta_...，WebUI Workers 页生成）" >&2
  exit 1
fi
if [ -z "$DEVICE" ]; then
  DEVICE="worker-$(hostname | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '-')"
fi

INSTALL_DIR="/opt/grape"
VENV="$INSTALL_DIR/.venv"
SERVICE_FILE="/etc/systemd/system/runtime-agent.service"
ENV_FILE="/etc/grape/runtime.env"

# ===== 工具函数 =====
log() { echo "[install-worker] $*"; }
fail() { echo "[install-worker] 错误: $*" >&2; exit 1; }

# ===== 1. 准备 Python >= 3.10 =====
pick_python() {
  for py in python3.12 python3.11 python3.10 python3; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  log "未找到 Python >= 3.10，尝试自动安装..."
  if command -v apt-get >/dev/null; then
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y python3.12 python3.12-venv >/dev/null 2>&1 || true
    PY="python3.12"
  elif command -v yum >/dev/null; then
    yum install -y python3.12 >/dev/null 2>&1 || true
    PY="python3.12"
  elif command -v dnf >/dev/null; then
    dnf install -y python3.12 >/dev/null 2>&1 || true
    PY="python3.12"
  fi
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    fail "无法自动安装 Python>=3.10，请手动安装后重试（apt/yum install python3.12）"
  fi
fi
log "使用 Python: $PY ($($PY --version 2>&1 | awk '{print $2}'))"

# ===== 2. 稀疏拉取仓库 =====
if [ -d "$INSTALL_DIR/.git" ]; then
  log "仓库已存在，更新分支 $BRANCH"
  cd "$INSTALL_DIR"
  git fetch origin "$BRANCH" >/dev/null 2>&1 || true
  git checkout "$BRANCH" >/dev/null 2>&1 || true
  git pull origin "$BRANCH" >/dev/null 2>&1 || true
else
  log "稀疏拉取仓库 $REPO (分支 $BRANCH)"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --filter=blob:none --sparse -b "$BRANCH" "$REPO" "$INSTALL_DIR" || fail "git clone 失败: $REPO"
  cd "$INSTALL_DIR"
  # worker 运行 src.runtime.agent 需整个 src/ + skills + 最小依赖清单；configs/data/docs 等不拉取
  git sparse-checkout set src skills requirements-worker.txt >/dev/null 2>&1 || true
fi

# ===== 3. 建 venv + 最小依赖 =====
if [ ! -x "$VENV/bin/python" ]; then
  log "创建 venv ($PY)"
  "$PY" -m venv "$VENV"
fi
log "安装依赖 requirements-worker.txt"
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements-worker.txt" || true

# ===== 4. 检测/安装 opencode =====
if ! command -v opencode >/dev/null 2>&1; then
  log "未检测到 opencode，尝试安装..."
  if command -v npm >/dev/null 2>&1; then
    npm install -g opencode-ai >/dev/null 2>&1 || true
  fi
fi
if ! command -v opencode >/dev/null 2>&1; then
  log "警告: opencode 未安装或不在 PATH。请手动安装后重试（npm install -g opencode-ai）"
fi

# ===== 5. 生成 systemd（token 隔离到 EnvironmentFile）=====
log "生成 systemd 服务（token 存 $ENV_FILE, 权限 600）"
mkdir -p "$(dirname "$ENV_FILE")"
umask 077
cat > "$ENV_FILE" <<EOF
RUNTIME_TOKEN=$TOKEN
EOF
chmod 600 "$ENV_FILE"

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
Environment=RUNTIME_SERVER_URL=$SERVER
Environment=RUNTIME_DEVICE_NAME=$DEVICE
Environment=RUNTIME_MAX_CONCURRENT_TASKS=20
Environment=RUNTIME_POLL_INTERVAL=3
Environment=RUNTIME_HEARTBEAT_INTERVAL=15

[Install]
WantedBy=multi-user.target
EOF

# ===== 6. 启动 =====
log "启动 runtime-agent 服务"
systemctl daemon-reload
systemctl enable runtime-agent >/dev/null 2>&1 || true
systemctl restart runtime-agent || fail "服务启动失败，请查看 journalctl -u runtime-agent"

log "安装完成！验证："
log "  systemctl status runtime-agent"
log "  journalctl -u runtime-agent -f  # 应看到 RuntimeAgent registered"
