#!/usr/bin/env bash
# 把兄弟机上的 agent 执行进程从旧代码目录切到当前部署目录（三库模式）。
#
# 背景（2026-11-15 实测）：116 上跑着两个 agent 执行进程，它们的解释器分别指向
# <legacy-root>/.venv 和 <tenant-root>/.venv —— 也就是两套**旧代码目录**。
# 我们把新代码部署到 /home/grape/grape-new-version2 并重启了 38081-38084，但真正认领/
# 执行任务的是这个 agent 进程，它仍在跑旧代码，于是新部署的修复（引擎会话、认领闸门、
# 事件判死口径）对实际执行链路完全没生效，表现就是任务被认领后没有会话、零输出挂死。
#
# 本脚本只做一件事：找出「解释器不在当前部署目录」的 agent 进程，用**它们自己的 env**
# （RUNTIME_TOKEN / SERVER_URL / DEVICE_NAME / HOME / PATH）在部署目录里重新拉起一个，
# 然后校验进程数与日志。不动凭据、不改库、不打印 token。
#
# 用法：
#   bash scripts/upgrade_worker_agent.sh [--dry-run] [--owner-user <user>]
#     --dry-run     只打印将要执行的动作
#     --owner-user  只处理该用户（/proc/PID 的属主）下的 agent；默认全部
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="${GRAPE_AGENT_LOG_DIR:-/home/grape/grape-logs}"
PY="$REPO/venv/bin/python"
DRY=0
ONLY_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --owner-user) shift; ONLY_USER="${1:-}" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -x "$PY" ] || { echo "FATAL: $PY 不存在（部署目录 venv 未建好）" >&2; exit 1; }
mkdir -p "$LOGS"

# 1) 找 agent 进程：命令行含 -m src.domains.runtime.engine.agent
mapfile -t PIDS < <(pgrep -f "python.*-m src.domains.runtime.engine.agent" 2>/dev/null || true)
if [ "${#PIDS[@]}" -eq 0 ]; then
  echo "[agent-upgrade] 未发现运行中的 agent 进程"
else
  echo "[agent-upgrade] 发现 ${#PIDS[@]} 个 agent 进程"
fi

STOPPED=0
for pid in "${PIDS[@]}"; do
  [ -r "/proc/$pid/cmdline" ] || continue
  _user="$(stat -c %U "/proc/$pid" 2>/dev/null || echo '')"
  if [ -n "$ONLY_USER" ] && [ "$_user" != "$ONLY_USER" ]; then
    echo "  pid=$pid user=$_user 跳过（--owner-user=$ONLY_USER）"
    continue
  fi
  _argv0="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1)"
  # 解释器所在 venv 的上一级即代码目录
  _repo="$(dirname "$(dirname "$_argv0")")"
  if [ "$_repo" = "$REPO" ]; then
    echo "  pid=$pid user=$_user 已在部署目录（$_repo），跳过"
    continue
  fi
  _home="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^HOME=//p' | head -1)"
  _env_file="/tmp/agent-env-$pid.env"
  # 只取运行必需的 RUNTIME_*，token 落 600 权限临时文件，不打印
  if [ "$DRY" -eq 0 ]; then
    umask 077
    tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
      | grep -E '^RUNTIME_[A-Z_]+=' > "$_env_file" || true
    chmod 600 "$_env_file"
  fi
  echo "  pid=$pid user=$_user 旧代码目录=$_repo → 切到 $REPO"
  if [ "$DRY" -eq 0 ]; then
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    _home_dir="${_home:-/home/grape}"
    # PATH 顺序：部署目录 venv 在前；随后是该用户的 CLI 目录（opencode 在 .opencode/bin，
    # hermes/opencode npm 装在 .local/npm-global/bin），最后系统路径。
    _PATH="$REPO/venv/bin:$_home_dir/.local/bin:$_home_dir/.local/npm-global/bin:$_home_dir/.opencode/bin:/usr/local/bin:/usr/bin:/bin"
    # 必须保持原属主身份启动：以 root 跑会改写工作区与 ~/.hermes（属主错乱、后续引擎
    # spawn 到 /root 下）。env 文件临时给该用户可读，启动后立即删除。
    chown "$_user": "$_env_file" 2>/dev/null || true
    chmod 600 "$_env_file" 2>/dev/null || true
    nohup runuser -u "$_user" -- bash -c \
      "set -a; . '$_env_file'; set +a; export HOME='$_home_dir'; export PATH='$_PATH'; cd '$REPO'; exec '$PY' -m src.domains.runtime.engine.agent" \
      >> "$LOGS/agent.log" 2>&1 &
    echo $! > "/tmp/agent-new-$pid.pid"
    sleep 1
    _new="$(cat "/tmp/agent-new-$pid.pid" 2>/dev/null || echo '')"
    if [ -n "$_new" ] && kill -0 "$_new" 2>/dev/null; then
      echo "    新进程 pid=$_new 已启动（日志 $LOGS/agent.log）"
      STOPPED=$((STOPPED+1))
    else
      echo "    WARN: 新进程未存活，检查 $LOGS/agent.log" >&2
    fi
    rm -f "$_env_file"
  else
    STOPPED=$((STOPPED+1))
  fi
done

echo "[agent-upgrade] 切换 $STOPPED 个进程"

# 1.5) 补齐执行侧技能树 —— 这是最容易被漏掉、后果又最隐蔽的一项。
# 引擎物化技能时走 resolve_official_skill_path()，它读的是 <checkout>/.grape/skills/
# （带 issue/feedback-loop 这类中间层），**不是**仓库根的 skills/。
# 只同步 skills/ 的话引擎会报：
#   [skill_mismatch] bound skill 'issue-cli' not materialized anywhere
# 然后拒绝执行，任务在 agent 起来之前就失败，看起来像流水线坏了。
for _home in $(awk -F: '$3>=1000 && $7 !~ /nologin|false/ {print $6}' /etc/passwd); do
  _co="$_home/grape-new-version2"
  [ -d "$_co" ] || continue
  if [ "$DRY" -eq 0 ]; then
    mkdir -p "$_co/.grape"
    rsync -a --delete "$REPO/.grape/skills/" "$_co/.grape/skills/" 2>/dev/null \
      || cp -a "$REPO/.grape/skills/." "$_co/.grape/skills/"
    chown -R "$(stat -c %U "$_home"):$(stat -c %G "$_home")" "$_co/.grape" 2>/dev/null
    echo "  skills -> $_co/.grape/skills ($(find "$_co/.grape/skills" -type f 2>/dev/null | wc -l) files)"
  else
    echo "  [dry-run] 将补齐 skills -> $_co/.grape/skills"
  fi
done
# 2) 校验：每个用户至多一个 agent，且解释器指向部署目录
for pid in $(pgrep -f "python.*-m src.domains.runtime.engine.agent" 2>/dev/null || true); do
  _argv0="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1)"
  _user="$(stat -c %U "/proc/$pid" 2>/dev/null || echo '')"
  echo "  running pid=$pid user=$_user interpreter=$_argv0"
done