#!/usr/bin/env bash
# Issue #3847：发布窗口「摘流 → 排空 → 退出」+ 5xx 率门禁（>0.5% 自动回滚）。
#
# 背景：发布/热加载时后端进程被直接重启/摘流，网关复用已关闭连接并切在途请求，
# 10:12–10:14 三分钟回 1,173 次 502（5xx 率 1.63%，基线 0.077%）。本脚本把发布
# 流程固化为：先摘流（不再分配新请求）→ 轮询在途归零（排空）→ 再重启（退出）；
# 重启期间同步采样健康入口失败率，超过阈值立即执行回滚命令并以非 0 退出。
#
# 失败语义（fail-closed，Issue #3847 审查修复）：
#   - 未配置 GATEWAY_DRAIN_CMD → 拒绝重启（exit 3）；确有无需摘流的场景（单实例直连）
#     才显式设置 GATEWAY_ALLOW_UNDRAINED=1 放行；
#   - 摘流/重启/回滚命令执行失败 → 立即非 0 退出，不再继续后续步骤；
#   - 采样时连接失败（curl 退出非 0 或 000）、网关整体不可用 → 计入失败率，绝不当作
#     「0 次 502」放行；
#   - 失败率口径 = **5xx（502/503/504…）+ 连接失败**（审查 R1）：事故验收目标是
#     「窗口内 5xx ≤0.5%」，只认 502 会把 503/504 风暴（上游过载/超时）当成健康。
#     GATEWAY_502_THRESHOLD 是历史变量名，口径按 5xx 计。
#   - 全样本失败（网关判死）优先于最小样本判定直接回滚（审查 O1）：缩窗/少路径采到
#     3 个样本全失败时，不能因「样本不足 30」放行。
#   - 单个偶发失败不回滚：比率达标还需失败样本数 >= GATEWAY_MIN_FAILURES（默认 3），
#     避免 180 样本窗口下「1 次 502 = 0.56%」把 0.077% 基线误判成发布故障。
#
# 计时与短路（审查 R2）：
#   - 采样窗口按**墙钟**截止（date +%s），不按「循环次数 × 间隔」——curl 每次最多
#     -m 5s，按迭代计时会让配置的 60s 窗口在超时场景跑成约 16 分钟，回滚严重延迟；
#   - 失败率与失败样本数已达标时**提前短路**，不必跑满窗口才判定。
#
# 采样覆盖（审查 O2）：
#   - 采样器在 `stop_unit` **之前**后台启动、与重启并行，覆盖切流/重启瞬间的失败；
#     采集结果由门禁统一判定（旧实现重启完成后才开始采样，切流 502 采不到）。
#
# 用法：
#   GATEWAY_DRAIN_CMD='true' GATEWAY_STOP_CMD='systemctl restart grape-server' \
#     bash scripts/deploy-drain.sh [--dry-run] [--gateway URL] [--unit NAME]
#
# --dry-run 只打印计划与阈值，不执行任何摘流/重启/回滚命令（用于发布前预演与 CI）。
#
# 可配置环境变量（均有安全默认值）：
#   GATEWAY_BASE_URL            采样入口，默认 http://127.0.0.1:38080
#   GATEWAY_HEALTH_PATHS        采样路径（空格分隔），默认 /api/health /api/runtime/heartbeat /static/openapi.json
#   GATEWAY_502_THRESHOLD       失败率阈值（5xx + 连接失败），默认 0.005（=0.5%，与 deploy/alerts.yml 一致）
#   GATEWAY_MIN_FAILURES        触发回滚所需的最小失败样本数，默认 3（防单个偶发 502 误回滚）
#   GATEWAY_MIN_SAMPLES         最小样本量，低于此值不判定（全样本失败除外），默认 30
#   GATEWAY_GATE_WINDOW_SECONDS 采样窗口墙钟秒数，默认 60
#   GATEWAY_SAMPLE_INTERVAL     采样间隔秒数，默认 1
#   GATEWAY_DRAIN_TIMEOUT       等待在途归零的最长秒数，默认 20
#   GATEWAY_INFLIGHT_CMD        返回当前在途请求数的命令（如查 LB/nginx stub_status）；未设置则只按固定时间等待
#   GATEWAY_DRAIN_CMD           摘流命令（从 LB/upstream 摘除本实例）；未设置时默认拒绝重启（fail-closed）
#   GATEWAY_ALLOW_UNDRAINED     设为 1 才允许「未配置摘流命令」继续重启（显式豁免）
#   GATEWAY_STOP_CMD            退出/重启命令；未设置且有 --unit 时默认 systemctl restart <unit>
#   GATEWAY_ROLLBACK_CMD        失败率超阈值时的回滚命令；未设置则仅告警退出
#   GATEWAY_NGINX_TEST=1        执行前先跑 nginx -t（若系统有 nginx）
set -uo pipefail

DRY_RUN=0
UNIT=""
GATEWAY_BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:38080}"
GATEWAY_HEALTH_PATHS="${GATEWAY_HEALTH_PATHS:-/api/health /api/runtime/heartbeat /static/openapi.json}"
GATEWAY_502_THRESHOLD="${GATEWAY_502_THRESHOLD:-0.005}"
GATEWAY_MIN_FAILURES="${GATEWAY_MIN_FAILURES:-3}"
GATEWAY_MIN_SAMPLES="${GATEWAY_MIN_SAMPLES:-30}"
GATEWAY_GATE_WINDOW_SECONDS="${GATEWAY_GATE_WINDOW_SECONDS:-60}"
GATEWAY_SAMPLE_INTERVAL="${GATEWAY_SAMPLE_INTERVAL:-1}"
GATEWAY_DRAIN_TIMEOUT="${GATEWAY_DRAIN_TIMEOUT:-20}"

# 并行采样器状态（O2）：启动于 stop_unit 之前，门禁取它的结果
SAMPLER_PID=""
SAMPLER_OUT=""

cleanup_sampler() {
  if [ -n "${SAMPLER_PID:-}" ] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill "$SAMPLER_PID" 2>/dev/null || true
  fi
  [ -n "${SAMPLER_OUT:-}" ] && rm -f "$SAMPLER_OUT"
}
trap cleanup_sampler EXIT

# 解析带值参数（保持 POSIX 友好，不用 getopts 长选项）
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --unit) UNIT="${2:-}"; shift ;;
    --gateway) GATEWAY_BASE_URL="${2:-}"; shift ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    *) echo "[FAIL] 未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

# 采样参数兜底：间隔为 0 会让采样循环空转，窗口至少 1s
if ! [ "${GATEWAY_SAMPLE_INTERVAL:-0}" -ge 1 ] 2>/dev/null; then GATEWAY_SAMPLE_INTERVAL=1; fi
if ! [ "${GATEWAY_GATE_WINDOW_SECONDS:-0}" -ge 1 ] 2>/dev/null; then GATEWAY_GATE_WINDOW_SECONDS=1; fi
if ! [ "${GATEWAY_MIN_SAMPLES:-0}" -ge 0 ] 2>/dev/null; then GATEWAY_MIN_SAMPLES=30; fi
if ! [ "${GATEWAY_MIN_FAILURES:-0}" -ge 1 ] 2>/dev/null; then GATEWAY_MIN_FAILURES=3; fi

# 执行命令；失败时打印原因并返回非 0（调用方必须检查，禁止忽略退出码）
run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] $*"
    return 0
  fi
  echo "[exec] $*"
  if ! bash -c "$*"; then
    echo "[FAIL] 命令执行失败：$*" >&2
    return 1
  fi
  return 0
}

drain() {
  echo "=== [1/3] 摘流：从 upstream/LB 摘除本实例，不再分配新请求 ==="
  if [ -z "${GATEWAY_DRAIN_CMD:-}" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      echo "[dry-run] 未配置 GATEWAY_DRAIN_CMD；真实运行将拒绝重启（fail-closed）"
      return 0
    fi
    if [ "${GATEWAY_ALLOW_UNDRAINED:-0}" = "1" ]; then
      echo "[WARN] 未配置 GATEWAY_DRAIN_CMD 且显式 GATEWAY_ALLOW_UNDRAINED=1——跳过摘流" >&2
      return 0
    fi
    echo "[FAIL] 未配置 GATEWAY_DRAIN_CMD——拒绝在未摘流的情况下重启（会让在途请求直接 502）。" >&2
    echo "       请接入 LB/upstream 摘流钩子；确无需摘流时显式设置 GATEWAY_ALLOW_UNDRAINED=1。" >&2
    exit 3
  fi
  if ! run "$GATEWAY_DRAIN_CMD"; then
    echo "[FAIL] 摘流失败——中止发布，不重启进程（避免把在途请求切成 502）。" >&2
    exit 3
  fi
}

wait_inflight() {
  echo "=== [2/3] 排空：等待在途请求归零（最长 ${GATEWAY_DRAIN_TIMEOUT}s）==="
  if [ -z "${GATEWAY_INFLIGHT_CMD:-}" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      echo "[dry-run] sleep ${GATEWAY_DRAIN_TIMEOUT}（未配置 GATEWAY_INFLIGHT_CMD，按固定窗口等待）"
    else
      sleep "$GATEWAY_DRAIN_TIMEOUT"
    fi
    return 0
  fi
  local waited=0 inflight
  while [ "$waited" -lt "$GATEWAY_DRAIN_TIMEOUT" ]; do
    inflight="$(bash -c "$GATEWAY_INFLIGHT_CMD" 2>/dev/null | tr -dc '0-9')"
    inflight="${inflight:-0}"
    echo "  在途请求：${inflight}（已等待 ${waited}s）"
    [ "$inflight" -le 0 ] && return 0
    if [ "$DRY_RUN" = "1" ]; then return 0; fi
    sleep "$GATEWAY_SAMPLE_INTERVAL"
    waited=$((waited + GATEWAY_SAMPLE_INTERVAL))
  done
  echo "[WARN] 排空超时（${GATEWAY_DRAIN_TIMEOUT}s）仍可能有在途请求，继续退出步骤" >&2
  return 0
}

# 采样健康入口状态码，输出 "总样本 502 数 5xx 数 连接失败数"
# 连接失败（curl 非 0 退出 / 000 / 无状态码）单独计数：网关整体不可用时不能算 0 次 502。
# 窗口按墙钟截止；失败率与失败样本数达标时提前短路（不跑满窗口）。
sample_codes() {
  local total=0 n502=0 n5xx=0 nconn=0 code rc raw path
  local deadline now remaining failures ratio stop=0
  deadline=$(( $(date +%s) + GATEWAY_GATE_WINDOW_SECONDS ))
  while [ "$stop" -eq 0 ]; do
    for path in $GATEWAY_HEALTH_PATHS; do
      if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] 采样窗口共 ${GATEWAY_GATE_WINDOW_SECONDS}s（每 ${GATEWAY_SAMPLE_INTERVAL}s 采 ${GATEWAY_HEALTH_PATHS}）" >&2
        echo "0 0 0 0"
        return 0
      fi
      raw="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "${GATEWAY_BASE_URL}${path}" 2>/dev/null)"
      rc=$?
      code="$(printf '%s' "$raw" | tr -dc '0-9' | tail -c 3)"
      total=$((total + 1))
      if [ "$rc" -ne 0 ] || [ -z "$code" ] || [ "$code" = "000" ]; then
        nconn=$((nconn + 1))
      else
        [ "$code" = "502" ] && n502=$((n502 + 1))
        case "$code" in 5??) n5xx=$((n5xx + 1)) ;; esac
      fi

      # 提前短路（R2）：失败率 + 失败样本数达标，或网关整体判死（失败样本==总样本）
      failures=$((n5xx + nconn))
      if [ "$failures" -ge "$GATEWAY_MIN_FAILURES" ] \
         && { [ "$failures" -eq "$total" ] || [ "$total" -ge "$GATEWAY_MIN_SAMPLES" ]; }; then
        if [ "$failures" -eq "$total" ]; then
          echo "[gate] 采样到 ${total} 个样本全部失败（5xx=${n5xx} 连接失败=${nconn}）——提前结束采样" >&2
          stop=1
          break
        fi
        ratio="$(awk -v n="$failures" -v t="$total" 'BEGIN{ printf "%.6f", n/t }')"
        if awk -v r="$ratio" -v th="$GATEWAY_502_THRESHOLD" 'BEGIN{ exit !(r > th) }'; then
          echo "[gate] 失败率 ${ratio} > 阈值 ${GATEWAY_502_THRESHOLD}（样本 ${total}，失败 ${failures}）——提前结束采样，不必跑满窗口" >&2
          stop=1
          break
        fi
      fi
      # 墙钟截止（curl 慢响应时也不能超窗太多）
      if [ "$(date +%s)" -ge "$deadline" ]; then stop=1; break; fi
    done
    [ "$stop" -eq 1 ] && break
    now=$(date +%s)
    remaining=$((deadline - now))
    [ "$remaining" -le 0 ] && break
    if [ "$remaining" -lt "$GATEWAY_SAMPLE_INTERVAL" ]; then
      sleep "$remaining"
    else
      sleep "$GATEWAY_SAMPLE_INTERVAL"
    fi
  done
  echo "$total $n502 $n5xx $nconn"
}

# 在重启之前启动后台采样器（O2）：覆盖 stop_unit 的切流瞬间。
gate_start() {
  [ "$DRY_RUN" = "1" ] && return 0
  SAMPLER_OUT="$(mktemp "${TMPDIR:-/tmp}/grape-gate.XXXXXX")"
  echo "=== 门禁采样启动（与退出/重启并行，覆盖切流窗口；窗口 ${GATEWAY_GATE_WINDOW_SECONDS}s）==="
  sample_codes > "$SAMPLER_OUT" &
  SAMPLER_PID=$!
}

# 取并行采样结果（在主 shell 内 wait，子 shell 无法 wait 父 shell 的后台进程）；
# 采样器异常无输出时前台补采一次，避免门禁被跳过。
gate() {
  echo "=== 门禁：采样健康入口失败率（5xx 含 502 + 连接失败；阈值 ${GATEWAY_502_THRESHOLD}，窗口 ${GATEWAY_GATE_WINDOW_SECONDS}s）==="
  local total n502 n5xx nconn ratio failures line=""
  if [ -n "${SAMPLER_PID:-}" ]; then
    wait "$SAMPLER_PID" 2>/dev/null || true
    SAMPLER_PID=""
    if [ -n "${SAMPLER_OUT:-}" ]; then
      line="$(tail -n 1 "$SAMPLER_OUT" 2>/dev/null)"
      rm -f "$SAMPLER_OUT"
      SAMPLER_OUT=""
    fi
    if [ -z "$line" ]; then
      echo "[WARN] 并行采样无结果，改为前台补采一次" >&2
    fi
  fi
  if [ -z "$line" ]; then
    line="$(sample_codes)"
  fi
  read -r total n502 n5xx nconn <<< "$line"
  echo "  样本=${total} 502=${n502} 5xx=${n5xx} 连接失败=${nconn}"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] 以上为模拟结果；真实运行将按下列规则判定："
    echo "  失败样本 = 5xx（含 502）+ 连接失败"
    echo "  失败样本 == 总样本（网关整体不可用/全 5xx）→ 直接回滚，先于最小样本判定"
    echo "  total < ${GATEWAY_MIN_SAMPLES} → 样本不足，不判定"
    echo "  失败率 > ${GATEWAY_502_THRESHOLD} 且失败数 >= ${GATEWAY_MIN_FAILURES} → 执行回滚并以非 0 退出"
    return 0
  fi
  failures=$((n5xx + nconn))
  # O1：全样本失败优先于最小样本判定（缩窗/少路径时不能因样本不足放行）
  if [ "$total" -gt 0 ] && [ "$failures" -eq "$total" ]; then
    echo "[FAIL] ${total} 个样本全部失败（5xx=${n5xx} 连接失败=${nconn}）——网关不可用，无视最小样本/最小失败数，触发自动回滚" >&2
    rollback
    exit 1
  fi
  if [ "$total" -lt "$GATEWAY_MIN_SAMPLES" ]; then
    echo "[WARN] 样本不足（${total} < ${GATEWAY_MIN_SAMPLES}），跳过回滚判定" >&2
    return 0
  fi
  ratio="$(awk -v n="$failures" -v t="$total" 'BEGIN{ if(t>0) printf "%.6f", n/t; else print "0" }')"
  if awk -v r="$ratio" -v th="$GATEWAY_502_THRESHOLD" 'BEGIN{ exit !(r > th) }'; then
    if [ "$failures" -lt "$GATEWAY_MIN_FAILURES" ]; then
      echo "[WARN] 失败率 ${ratio} > 阈值 ${GATEWAY_502_THRESHOLD}，但失败样本仅 ${failures} < ${GATEWAY_MIN_FAILURES}——视为偶发抖动，不回滚（口径见脚本头注释）" >&2
      return 0
    fi
    echo "[FAIL] 失败率 ${ratio} > 阈值 ${GATEWAY_502_THRESHOLD}（失败样本 ${failures}，其中 5xx=${n5xx}、连接失败 ${nconn}），触发自动回滚" >&2
    rollback
    exit 1
  fi
  if [ "$nconn" -gt 0 ]; then
    echo "[WARN] 采样期出现 ${nconn} 次连接失败（未达回滚条件），请人工确认入口可用性" >&2
  fi
  echo "[OK] 失败率 ${ratio} ≤ 阈值 ${GATEWAY_502_THRESHOLD}，放行"
}

stop_unit() {
  echo "=== [3/3] 退出：排空完成后再重启/退出进程 ==="
  local cmd="${GATEWAY_STOP_CMD:-}"
  if [ -z "$cmd" ] && [ -n "$UNIT" ]; then
    cmd="systemctl restart $UNIT"
  fi
  if [ -z "$cmd" ]; then
    echo "[WARN] 未配置 GATEWAY_STOP_CMD 且未指定 --unit——跳过退出步骤" >&2
    return 0
  fi
  if ! run "$cmd"; then
    echo "[FAIL] 重启/退出命令失败——发布未完成，需人工介入。" >&2
    exit 4
  fi
}

rollback() {
  if [ -n "${GATEWAY_ROLLBACK_CMD:-}" ]; then
    if ! bash -c "$GATEWAY_ROLLBACK_CMD"; then
      echo "[FAIL] 回滚命令执行失败，需人工介入。" >&2
    fi
  else
    echo "[FAIL] 未配置 GATEWAY_ROLLBACK_CMD，需人工回滚" >&2
  fi
}

main() {
  echo "Grape 发布摘流/排空/门禁（dry_run=${DRY_RUN} unit=${UNIT:-<none>} gateway=${GATEWAY_BASE_URL}）"
  echo "失败率阈值=${GATEWAY_502_THRESHOLD} 最小失败数=${GATEWAY_MIN_FAILURES} 最小样本=${GATEWAY_MIN_SAMPLES} 窗口=${GATEWAY_GATE_WINDOW_SECONDS}s"
  if [ "${GATEWAY_NGINX_TEST:-0}" = "1" ]; then
    if command -v nginx >/dev/null 2>&1; then
      run "nginx -t" || { echo "[FAIL] nginx -t 未通过，中止发布" >&2; exit 3; }
    else
      echo "[WARN] 未安装 nginx，跳过 nginx -t" >&2
    fi
  fi
  drain
  wait_inflight
  gate_start
  stop_unit
  gate
  echo "[DONE] 发布窗口完成：摘流→排空→退出，失败率门禁未触发。"
}

main