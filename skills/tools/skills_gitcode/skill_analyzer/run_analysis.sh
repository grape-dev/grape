#!/usr/bin/env bash
# run_analysis.sh — Skill实现原理分析包装脚本
#
# 用法:
#   run_analysis.sh --repo-url <git_url> --sha <pr_head_sha> \
#                    --skill-names <name1> [name2 ...] \
#                    [--output-dir <dir>] [--analyzer-dir <dir>] \
#                    [--no-llm] [--llm-provider <p> --llm-model <m> --llm-api-key <k> --llm-base-url <u>]
#
# 功能:
#   1. 浅克隆目标仓库到临时目录并 checkout 到指定 SHA
#   2. 在克隆仓库上运行 huaweicloud-skills-analyzer
#   3. 将生成的 Skill实现原理分析报告复制到 --output-dir
#   4. 输出报告文件路径列表 (每行一个绝对路径)
#
# 退出码: 0=成功 1=参数错误 2=克隆失败 3=分析失败

set -euo pipefail

PYTHON_BIN="${DEVOPS_PYTHON:-python3}"

REPO_URL=""
SHA=""
SKILL_NAMES=()
OUTPUT_DIR=""
ANALYZER_DIR=""
NO_LLM=false
LLM_PROVIDER=""
LLM_MODEL=""
LLM_API_KEY=""
LLM_BASE_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)      REPO_URL="$2"; shift 2 ;;
    --sha)           SHA="$2"; shift 2 ;;
    --skill-names)   shift; while [[ $# -gt 0 && "$1" != --* ]]; do SKILL_NAMES+=("$1"); shift; done ;;
    --output-dir)    OUTPUT_DIR="$2"; shift 2 ;;
    --analyzer-dir)  ANALYZER_DIR="$2"; shift 2 ;;
    --no-llm)        NO_LLM=true; shift ;;
    --llm-provider)  LLM_PROVIDER="$2"; shift 2 ;;
    --llm-model)     LLM_MODEL="$2"; shift 2 ;;
    --llm-api-key)   LLM_API_KEY="$2"; shift 2 ;;
    --llm-base-url)  LLM_BASE_URL="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REPO_URL" || -z "$SHA" || ${#SKILL_NAMES[@]} -eq 0 ]]; then
  echo "Usage: run_analysis.sh --repo-url <url> --sha <sha> --skill-names <n1> [n2 ...] [--output-dir <dir>] [--analyzer-dir <dir>]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -z "$ANALYZER_DIR" ]] && ANALYZER_DIR="$SCRIPT_DIR"
[[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="$(mktemp -d)/reports"

mkdir -p "$OUTPUT_DIR"

TMP_REPO="$(mktemp -d)"
trap 'rm -rf "$TMP_REPO"' EXIT

echo "[run_analysis] Cloning $REPO_URL @ $SHA ..." >&2
if ! git clone --filter=blob:none --no-checkout "$REPO_URL" "$TMP_REPO" 2>&1 >&2; then
  echo "[run_analysis] git clone failed" >&2
  exit 2
fi
cd "$TMP_REPO"
git checkout "$SHA" 2>&1 >&2 || { echo "[run_analysis] git checkout $SHA failed" >&2; exit 2; }

SKILL_ARGS=""
for n in "${SKILL_NAMES[@]}"; do
  SKILL_ARGS="$SKILL_ARGS $n"
done

LLM_FLAGS=""
if [[ "$NO_LLM" == true ]]; then
  LLM_FLAGS="--no-llm"
else
  [[ -n "$LLM_PROVIDER" ]]  && LLM_FLAGS="$LLM_FLAGS --llm-provider $LLM_PROVIDER"
  [[ -n "$LLM_MODEL" ]]     && LLM_FLAGS="$LLM_FLAGS --llm-model $LLM_MODEL"
  [[ -n "$LLM_API_KEY" ]]   && LLM_FLAGS="$LLM_FLAGS --llm-api-key $LLM_API_KEY"
  [[ -n "$LLM_BASE_URL" ]]  && LLM_FLAGS="$LLM_FLAGS --llm-base-url $LLM_BASE_URL"
fi

echo "[run_analysis] Running analyzer on: ${SKILL_NAMES[*]}" >&2
cd "$ANALYZER_DIR"

ANALYZER_OUTPUT="$(mktemp -d)/reports"
mkdir -p "$ANALYZER_OUTPUT"

if ! "$PYTHON_BIN" main.py \
    --skills-repo "$TMP_REPO" \
    --skill-names ${SKILL_ARGS} \
    --output-dir "$ANALYZER_OUTPUT" \
    --skip-cli-install \
    --log-level WARNING \
    $LLM_FLAGS \
    2>&1 >&2; then
  echo "[run_analysis] analyzer execution failed (continuing to collect any partial reports)" >&2
fi

if [[ -d "$ANALYZER_OUTPUT" && "$(ls -A "$ANALYZER_OUTPUT" 2>/dev/null)" ]]; then
  cp "$ANALYZER_OUTPUT"/*.md "$OUTPUT_DIR/" 2>/dev/null || true
fi

echo "[run_analysis] Reports collected in: $OUTPUT_DIR" >&2
for f in "$OUTPUT_DIR"/*.md; do
  [[ -f "$f" ]] && echo "$(realpath "$f")"
done

if ! ls "$OUTPUT_DIR"/*.md >/dev/null 2>&1; then
  echo "[run_analysis] No report files generated" >&2
  exit 3
fi
