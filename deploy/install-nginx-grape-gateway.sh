#!/usr/bin/env bash
# Issue #3847：原生部署的网关公共片段「幂等下发 + nginx -t 守卫 + 失败回滚」。
#
# 背景（PR #1998 R3 复审 Optional）：Issue #3847 的加固依赖 3 个公共片段，原先只能手工
# `cp` 到 /etc/nginx/conf.d 并手工改主 nginx.conf（main 上下文）。漏拷/漏 include 会让
# 网关启动即 [emerg]，或失去 worker_shutdown_timeout 的优雅退出。本脚本按
# deploy/install-nginx-gzip.sh 的模式做成一次下发：拷贝 → nginx -t → 失败回滚 → reload。
#
# 下发内容：
#   1. nginx-grape-upstream.inc            → $NGINX_CONF_DIR/（重试 + 客户端 keepalive）
#   2. nginx-grape-upstream-keepalive.inc  → $NGINX_CONF_DIR/（upstream keepalive）
#   3. nginx-worker-shutdown.inc           → 主配置同目录，并在主 nginx.conf 的 main
#      上下文（events 之前）补一行 include（worker_shutdown_timeout 不能放 conf.d）
#
# 幂等：目标文件内容一致时跳过拷贝；主配置已 include 时不重复插入；重复执行不改内容。
# dry-run 只打印计划，不写盘、不 reload（可非 root 预演）。
#
# 用法：
#   sudo bash deploy/install-nginx-grape-gateway.sh [--dry-run]
#
# 可配置环境变量（默认值即生产路径）：
#   NGINX_CONF_DIR   片段安装目录，默认 /etc/nginx/conf.d
#   NGINX_MAIN_CONF  主配置，默认 /etc/nginx/nginx.conf
#   NGINX_CMD        nginx 可执行文件，默认 nginx
#   NGINX_TEST_ARGS  追加给 `nginx -t` 的参数（测试/非标准 prefix 用）
#   NGINX_RELOAD_ARGS 追加给 `nginx -s reload` 的参数
#   NGINX_SKIP_RELOAD 设为 1 时只校验不 reload（测试/演练）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${NGINX_CONF_DIR:-/etc/nginx/conf.d}"
MAIN_CONF="${NGINX_MAIN_CONF:-/etc/nginx/nginx.conf}"
NGINX_CMD="${NGINX_CMD:-nginx}"
TEST_ARGS="${NGINX_TEST_ARGS:-}"
RELOAD_ARGS="${NGINX_RELOAD_ARGS:-}"
SKIP_RELOAD="${NGINX_SKIP_RELOAD:-0}"
SHUTDOWN_LINE="include nginx-worker-shutdown.inc;"
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "[gateway] 未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/grape-nginx-install.XXXXXX")"
CREATED=()
BACKED_UP=()

cleanup() {
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

fail() {
  echo "[gateway] $*" >&2
  exit 1
}

# 记录备份（同一文件只备份一次）
backup_file() {
  local path="$1" dest="$BACKUP_DIR/$(basename "$1").bak"
  [ -f "$path" ] || return 0
  [ -f "$dest" ] && return 0
  cp -p "$path" "$dest" || fail "备份失败：$path"
  BACKED_UP+=("$path|$dest")
}

# 失败回滚：恢复备份 + 删除本次新建文件
rollback_all() {
  local pair path dest
  for pair in "${BACKED_UP[@]:-}"; do
    [ -z "$pair" ] && continue
    path="${pair%%|*}"
    dest="${pair##*|}"
    cp -p "$dest" "$path" && echo "[gateway] 已回滚：$path" >&2
  done
  for path in "${CREATED[@]:-}"; do
    [ -z "$path" ] && continue
    rm -f "$path" && echo "[gateway] 已删除本次下发：$path" >&2
  done
}

install_file() {
  local src="$1" dst="$2"
  [ -f "$src" ] || fail "缺少源文件：$src"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    echo "[gateway] 已是目标状态：$dst"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] cp $src $dst"
    return 0
  fi
  if [ -f "$dst" ]; then backup_file "$dst"; else CREATED+=("$dst"); fi
  cp "$src" "$dst" || fail "拷贝失败：$src → $dst"
  echo "[gateway] 已写入 $dst"
}

ensure_main_include() {
  local tmp inserted=0
  if grep -qE "^[[:space:]]*include[[:space:]]+nginx-worker-shutdown\.inc;" "$MAIN_CONF"; then
    echo "[gateway] 主配置已 include worker_shutdown_timeout：$MAIN_CONF"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] 在 $MAIN_CONF 的 events 之前插入：$SHUTDOWN_LINE"
    return 0
  fi
  tmp="$(mktemp "$BACKUP_DIR/mainconf.XXXXXX")"
  awk -v inc="$SHUTDOWN_LINE" '
    !done && $0 ~ /^[[:space:]]*events[[:space:]]*\{/ { print "    " inc; done=1 }
    { print }
    END { if (!done) exit 3 }
  ' "$MAIN_CONF" > "$tmp" || fail "主配置未见 events 块，无法确定 main 上下文：$MAIN_CONF"
  backup_file "$MAIN_CONF"
  cat "$tmp" > "$MAIN_CONF" || fail "写入主配置失败：$MAIN_CONF"
  rm -f "$tmp"
  echo "[gateway] 已在 main 上下文插入：$SHUTDOWN_LINE"
}

# ---- 前置检查 ----
[ -f "$HERE/nginx-grape-upstream.inc" ] || fail "缺少 $HERE/nginx-grape-upstream.inc"
[ -f "$HERE/nginx-grape-upstream-keepalive.inc" ] || fail "缺少 $HERE/nginx-grape-upstream-keepalive.inc"
[ -f "$HERE/nginx-worker-shutdown.inc" ] || fail "缺少 $HERE/nginx-worker-shutdown.inc"
[ -f "$MAIN_CONF" ] || fail "主配置不存在：$MAIN_CONF"
if [ "$DRY_RUN" = "0" ]; then
  mkdir -p "$CONF_DIR" 2>/dev/null || fail "无法创建 $CONF_DIR"
  [ -w "$CONF_DIR" ] || fail "$CONF_DIR 不可写（需 root/sudo）"
  [ -w "$MAIN_CONF" ] || fail "$MAIN_CONF 不可写（需 root/sudo）"
fi

echo "[gateway] 下发网关公共片段（dry_run=${DRY_RUN} conf_dir=${CONF_DIR} main_conf=${MAIN_CONF}）"

# ---- 拷贝片段 ----
install_file "$HERE/nginx-grape-upstream.inc" "$CONF_DIR/nginx-grape-upstream.inc"
install_file "$HERE/nginx-grape-upstream-keepalive.inc" "$CONF_DIR/nginx-grape-upstream-keepalive.inc"
install_file "$HERE/nginx-worker-shutdown.inc" "$(dirname "$MAIN_CONF")/nginx-worker-shutdown.inc"
ensure_main_include

[ "$DRY_RUN" = "1" ] && { echo "[dry-run] 完成预演，未写盘、未 reload"; exit 0; }

# ---- nginx -t 守卫：失败回滚 ----
read -r -a TEST_ARGV <<< "$TEST_ARGS"
if ! "$NGINX_CMD" -t "${TEST_ARGV[@]}" 2>/tmp/grape-nginx-gateway-test.err; then
  echo "[gateway] nginx -t 失败，回滚本次下发：" >&2
  cat /tmp/grape-nginx-gateway-test.err >&2
  rollback_all
  exit 1
fi
echo "[gateway] nginx -t 通过"

if [ "$SKIP_RELOAD" = "1" ]; then
  echo "[gateway] NGINX_SKIP_RELOAD=1，跳过 reload"
  exit 0
fi
read -r -a RELOAD_ARGV <<< "$RELOAD_ARGS"
if ! "$NGINX_CMD" -s reload "${RELOAD_ARGV[@]}"; then
  echo "[gateway] reload 失败，回滚本次下发：" >&2
  rollback_all
  exit 1
fi
echo "[gateway] nginx 已 reload；建议核对：nginx -T | grep -E 'keepalive|worker_shutdown_timeout'"