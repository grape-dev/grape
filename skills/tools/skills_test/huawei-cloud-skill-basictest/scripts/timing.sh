#!/bin/bash
# timing.sh — 轻量耗时埋点（被各评测脚本 source 使用）
#
# 记录格式（管道分隔，便于 cut/awk/python 分析）:
#   ts|skill|stage|event|detail|dur_ms
#
# 用法:
#   source "$(dirname "$0")/timing.sh"
#   log_timing <skill> <stage> <event> <detail> [dur_ms]
#
# 环境变量:
#   EVAL_TIMING_LOG  日志文件路径（默认 /tmp/eval_timing.log）

TIMING_LOG="${EVAL_TIMING_LOG:-/tmp/eval_timing.log}"

log_timing() {
    local skill="$1" stage="$2" event="$3" detail="${4:-}" dur="${5:-}"
    mkdir -p "$(dirname "$TIMING_LOG")"
    printf '%s|%s|%s|%s|%s|%s\n' \
        "$(date +%Y-%m-%dT%H:%M:%S.%3N)" "$skill" "$stage" "$event" "$detail" "$dur" \
        >> "$TIMING_LOG"
}
