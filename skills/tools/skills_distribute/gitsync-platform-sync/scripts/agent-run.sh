#!/usr/bin/env bash
# =============================================================================
# gitsync agent-run.sh
#
# One-shot entry for the multica agent: prepares the runtime environment,
# verifies prerequisites, then runs `gitsync run`.
#
# Usage:
#   bash agent-run.sh --multica-issue <uuid>
#   bash agent-run.sh --multica-issue <uuid> --verbose
#   bash agent-run.sh --multica-issue <uuid> --force
#   bash agent-run.sh --multica-issue <uuid> --timeout 900
#
# Exit codes: 0 = ok (or nothing to do), 1 = diff/db-writeback issue,
#             2 = guard failure / environment error / timeout.
#
# Safe to re-run (idempotent).
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITSYNC_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$GITSYNC_DIR/src"
DEFAULT_CONFIG="$GITSYNC_DIR/config.yaml"

MULTICA=""
VERBOSE=0
FORCE=0
CONFIG="$DEFAULT_CONFIG"
TIMEOUT_SEC=600

# ---- parse args -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --multica-issue) MULTICA="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --timeout) TIMEOUT_SEC="$2"; shift 2 ;;
    --verbose) VERBOSE=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help)
      echo "Usage: $0 --multica-issue <uuid> [--config <path>] [--timeout <sec>] [--verbose] [--force]"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$MULTICA" ]]; then
  echo "ERROR: --multica-issue is required" >&2
  exit 2
fi

# ---- single-instance lock (2026-08-05, 需求1/4; 2026-08-16 下沉到 Python) --
# multica daemon 可能同时分发多个 issue -> 多个 agent 并发跑。git 同步本身
# 幂等可收敛, 但 map/manifest 本地状态与 ClawHub 版本 bump 并发会互相踩。
# 锁现在由 `gitsync run` 入口(Python flock)统一持有 —— 这样绕过本脚本直接
# 跑 `gitsync run` 的路径同样被保护, 且无嵌套 flock 自死锁。语义不变:
# 后到者排队等待(最多 GITSYNC_LOCK_WAIT_SEC 秒)而非直接失败, 超时才 exit 2。
log() { echo "[gitsync-agent] $*"; }
die() { echo "[gitsync-agent] ERROR: $*" >&2; exit 2; }

# 转发等待预算给 Python 层 run 锁(默认 1200s = 20 分钟排队上限, 2026-08-16)
export GITSYNC_LOCK_WAIT_SEC="${LOCK_WAIT_SEC:-1200}"

if ! [[ "$TIMEOUT_SEC" =~ ^[0-9]+$ ]] || [[ "$TIMEOUT_SEC" -lt 1 ]]; then
  echo "ERROR: --timeout must be a positive integer (got '$TIMEOUT_SEC')" >&2
  exit 2
fi

# ---- environment preparation ------------------------------------------------
log "==> 1/5 runtime environment"

# python3
command -v python3 >/dev/null 2>&1 || die "python3 not found"
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$(python3 -c 'import sys; print(sys.version_info >= (3,9))')" != "True" ]]; then
  die "python3 >= 3.9 required (found $PYVER)"
fi
log "    python3 $PYVER ok"

# git
command -v git >/dev/null 2>&1 || die "git not found"
log "    git $(git --version | awk '{print $3}') ok"

# node / npm (needed only for clawhub CLI)
NODE_OK=0
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  NODE_OK=1
  log "    node $(node --version) ok"
else
  log "    WARN: node/npm not found (clawhub CLI unavailable; publish step will fail)"
fi

# ---- install gitsync deps ---------------------------------------------------
log "==> 2/5 gitsync dependencies"
if ! python3 -c "import yaml" >/dev/null 2>&1; then
  log "    installing PyYAML..."
  pip3 install --quiet PyYAML 2>/dev/null \
    || pip3 install --quiet --break-system-packages PyYAML 2>/dev/null \
    || die "failed to install PyYAML"
fi
log "    PyYAML ok"

# ---- clawhub CLI + login ----------------------------------------------------
log "==> 3/5 clawhub CLI"
CLAWHUB_CONFIG_PATH_SET=""
if [[ -n "${CLAWHUB_CONFIG_PATH:-}" ]]; then
  CLAWHUB_CONFIG_PATH_SET="$CLAWHUB_CONFIG_PATH"
fi
# determine clawhub config path from gitsync config
CH_CFG="$(python3 -c "
import sys
sys.path.insert(0, '$SRC_DIR')
from gitsync.config import load_config
try:
    cfg = load_config('$CONFIG')
    print(cfg.clawhub.config_path)
except Exception:
    print('')
" 2>/dev/null)"
log "    clawhub config_path: ${CH_CFG:-<default>}"

if [[ "$NODE_OK" == "1" ]] && ! command -v clawhub >/dev/null 2>&1; then
  log "    installing clawhub CLI (npm i -g clawhub)..."
  npm install -g clawhub >/dev/null 2>&1 || log "    WARN: clawhub install failed"
fi

if command -v clawhub >/dev/null 2>&1; then
  export CLAWHUB_CONFIG_PATH="${CLAWHUB_CONFIG_PATH_SET:-$CH_CFG}"
  WHO="$(clawhub whoami 2>/dev/null || echo "")"
  if [[ -n "$WHO" ]]; then
    log "    clawhub logged in as: $WHO"
  else
    log "    WARN: clawhub not logged in (run 'clawhub login' once); publish will fail"
  fi
else
  log "    WARN: clawhub CLI unavailable"
fi

# ---- git remote reachability ------------------------------------------------
log "==> 4/5 git remotes"
# read remotes from gitsync config for a generic reachability probe
mapfile -t REMOTES < <(python3 -c "
import sys
sys.path.insert(0, '$SRC_DIR')
from gitsync.config import load_config
cfg = load_config('$CONFIG')
print(f\"gitcode|{cfg.remotes.get('gitcode', '')}\")
print(f\"github|{cfg.remotes.get('github', '')}\")
" 2>/dev/null)
for entry in "${REMOTES[@]}"; do
  name="${entry%%|*}"
  url="${entry#*|}"
  if [[ -n "$url" ]] && timeout 30 git ls-remote "$url" HEAD >/dev/null 2>&1; then
    log "    $name reachable (ls-remote ok)"
  else
    log "    WARN: $name not reachable via ls-remote (SSH key may be missing)"
  fi
done

# ---- run gitsync ------------------------------------------------------------
log "==> 5/5 running gitsync run"
ARGS=()
ARGS+=(--config "$CONFIG")
ARGS+=(run)
ARGS+=(--multica-issue "$MULTICA")
if [[ "$FORCE" == "1" ]]; then ARGS+=(--force); fi
if [[ "$VERBOSE" == "1" ]]; then ARGS+=(--verbose); fi

export PYTHONPATH="$SRC_DIR${PYTHONPATH:+:$PYTHONPATH}"
if [[ -n "${CLAWHUB_CONFIG_PATH:-}" ]]; then
  export CLAWHUB_CONFIG_PATH
fi

log "    command: gitsync ${ARGS[*]}"
log "    timeout: ${TIMEOUT_SEC}s"
# capture gitsync output; the last line is the JSON summary
OUT="$(timeout "$TIMEOUT_SEC" python3 -u -m gitsync.cli "${ARGS[@]}" 2>&1)"
RC=$?
if [[ "$RC" == "124" ]]; then
  echo "[gitsync-agent] ERROR: gitsync run timed out after ${TIMEOUT_SEC}s" >&2
  exit 2
fi
# print the raw output (so callers see it)
printf '%s\n' "$OUT"

# persist the JSON summary (last line) to a stable path for programmatic reads
LAST="$(printf '%s\n' "$OUT" | tail -n 1)"
if printf '%s' "$LAST" | python3 -c "import sys,json; json.loads(sys.stdin.read())" >/dev/null 2>&1; then
  RESULT_DIR="$(python3 -c "
import sys
sys.path.insert(0, '$SRC_DIR')
from gitsync.config import load_config
print(load_config('$CONFIG').db.product_dir or '')
" 2>/dev/null)"
  RESULT_DIR="${RESULT_DIR:-$GITSYNC_DIR/data/products}"
  mkdir -p "$RESULT_DIR"
  printf '%s\n' "$LAST" > "$RESULT_DIR/run-result.json"
  log "    result written to $RESULT_DIR/run-result.json"
fi

log "==> done (exit $RC)"
exit $RC
