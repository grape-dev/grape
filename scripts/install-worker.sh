#!/usr/bin/env bash
# Grape Worker 一键安装脚本（见 docs/new-version2/worker-server-delivery-design.md）
#
# 用法：
#   curl -sSL https://grape.example.com/api/runtime/install | bash -s \
#     --token <rta_...> \
#     [--server https://grape.example.com] \
#     [--device worker-dev1] \
#     [--repo https://gitcode.com/Jason7602/grape.git] \
#     [--branch new-version2]
#
# 默认从 grape server 下发运行包（rta_ 鉴权），不再 git clone 代码仓库；
# 显式传 --repo 时回退到 git 稀疏拉取方式。
# 不安装任何执行引擎（opencode 等由用户自行安装，见方案 §2.3）。
# 幂等：重复执行安全（已存在目录/venv/service 则复用）。
set -euo pipefail

# ===== 参数解析 =====
TOKEN=""
SERVER="https://grape.example.com"
DEVICE=""
REPO=""
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
WORKER_VERSION_FILE="/etc/grape/worker.version"

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

# ===== 2. 获取运行代码：服务端下发运行包（默认）或 git 回退 =====
if [ -n "$REPO" ]; then
  log "检测到 --repo，回退 git 稀疏拉取方式（$REPO，分支 $BRANCH）"
  if [ -d "$INSTALL_DIR/.git" ]; then
    log "仓库已存在，更新分支 $BRANCH"
    cd "$INSTALL_DIR"
    git fetch origin "$BRANCH" --progress || true
    git checkout "$BRANCH" || true
    git pull origin "$BRANCH" --progress || true
  else
    log "稀疏拉取仓库 $REPO (分支 $BRANCH，首次拉取需数分钟，请耐心等待)"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --filter=blob:none --sparse --progress -b "$BRANCH" "$REPO" "$INSTALL_DIR" || fail "git clone 失败: $REPO"
    cd "$INSTALL_DIR"
    git sparse-checkout set src skills requirements-worker.txt || true
  fi
else
  log "从 grape server 下载运行包: $SERVER/api/runtime/install/package"
  TMP_PKG="/tmp/grape-worker-$$.tar.gz"
  if ! curl -fsSL -H "Authorization: Bearer $TOKEN" -o "$TMP_PKG" "$SERVER/api/runtime/install/package"; then
    rm -f "$TMP_PKG"
    fail "下载运行包失败: $SERVER/api/runtime/install/package（请确认 token 有效 / server 地址正确）"
  fi

  # 校验 tar.gz 完整性
  if ! gzip -t "$TMP_PKG" 2>/dev/null; then
    rm -f "$TMP_PKG"
    fail "下载的运行包损坏（gzip 校验失败）"
  fi

  # 解压到安装目录（沿用 /opt/grape，非 git 仓库）
  mkdir -p "$INSTALL_DIR"
  log "解压运行包到 $INSTALL_DIR"
  tar -xzf "$TMP_PKG" -C "$INSTALL_DIR" --strip-components=1 || fail "解压运行包失败"
  rm -f "$TMP_PKG"

  # 校验包内 SHA256SUMS（存在则校验；缺失仅告警，不阻断）
  if [ -f "$INSTALL_DIR/SHA256SUMS" ]; then
    log "校验运行包内容（SHA256SUMS）..."
    ( cd "$INSTALL_DIR" && sha256sum -c SHA256SUMS >/dev/null 2>&1 ) || log "警告: 运行包内容校验未通过，请确认来源可信"
  fi

  # 记录版本号
  mkdir -p "$(dirname "$WORKER_VERSION_FILE")"
  if [ -f "$INSTALL_DIR/VERSION" ]; then
    cp "$INSTALL_DIR/VERSION" "$WORKER_VERSION_FILE"
    log "Worker 版本: $(cat "$WORKER_VERSION_FILE")"
  fi
fi

# data/ 是运行时目录（sessions.db/workspaces/logs），gitignore，需先建好
if [ ! -d "$INSTALL_DIR/data" ]; then
  log "创建运行时目录 $INSTALL_DIR/data"
  mkdir -p "$INSTALL_DIR/data"
fi

# ===== 3. 建 venv + 最小依赖 =====
if [ ! -x "$VENV/bin/python" ]; then
  log "创建 venv ($PY)"
  "$PY" -m venv "$VENV"
fi
if [ -f "$INSTALL_DIR/requirements-worker.txt" ]; then
  log "安装依赖 requirements-worker.txt（首次安装需数分钟）"
  "$VENV/bin/pip" install -r "$INSTALL_DIR/requirements-worker.txt" || true
fi

# ===== 4. 执行引擎提示（不自动安装，见方案 §2.3）=====
if ! command -v opencode >/dev/null 2>&1; then
  log "未检测到执行引擎（opencode 等）。请自行安装后重启 worker，例如："
  log "  npm install -g opencode-ai"
  log "  systemctl restart runtime-agent"
  log "（引擎类型不限定 opencode，按需选用；worker 启动时会自动发现本机已装引擎）"
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
