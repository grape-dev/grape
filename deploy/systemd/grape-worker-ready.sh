#!/usr/bin/env bash
# Issue #3969：worker 启动 readiness 门禁（systemd ExecStartPost 调用）。
#
# 轮询 /readyz 直到 200（DB 连通 + 表结构 + 未处于排空），未就绪不接流量。
# 单元里以 `ExecStartPost=-` 挂载：超时只记录告警、不让 Restart=always 反复拉起，
# 兼顾「门禁可观测」与「不把启动失败放大成重启风暴」。
set -uo pipefail

PORT="${GRAPE_WORKER_PORT:-38082}"
TIMEOUT="${GRAPE_READY_TIMEOUT:-60}"
URL="http://127.0.0.1:${PORT}/readyz"

elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
    code="$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
        exit 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
done

echo "worker readiness not reached in ${TIMEOUT}s ($URL -> ${code:-no-response})" >&2
exit 1