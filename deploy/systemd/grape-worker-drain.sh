#!/usr/bin/env bash
# Issue #3969：worker 停机前置摘流（systemd ExecStop 调用，先于 SIGTERM）。
#
# 顺序：标记 draining（/readyz 转 503）→ 等待在飞请求排空（≥5s）→ 退出。
# 随后 systemd 才向 uvicorn 发 SIGTERM（先关监听再 graceful 排空），网关侧
# 由多上游 + proxy_next_upstream + 客户端重试兜底，避免单台重启全量 502。
set -uo pipefail

PORT="${GRAPE_WORKER_PORT:-38082}"
DRAIN_SECONDS="${GRAPE_DRAIN_SECONDS:-5}"
URL="http://127.0.0.1:${PORT}/internal/drain"

# 摘流（重试几次覆盖 systemd 启动竞态；失败也继续排空等待，不阻断停机）
for _ in $(seq 1 5); do
    if curl -fsS -m 2 -X POST "$URL" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# 等待在飞请求排空（≥5s，可通过 GRAPE_DRAIN_SECONDS 调整）
case "$DRAIN_SECONDS" in
    ''|*[!0-9]*) DRAIN_SECONDS=5 ;;
esac
sleep "$DRAIN_SECONDS"
exit 0