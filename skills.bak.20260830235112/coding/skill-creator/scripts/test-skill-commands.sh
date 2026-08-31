#!/usr/bin/env bash
# ============================================================================
# test-skill-commands.sh — Functional test for generated REST API skill
# ============================================================================
# Usage: bash test-skill-commands.sh <skill-dir>
#
# Reads test cases from templates/test-vars.json:
#   - readonly (GET/list/get) → run real API call
#   - mutable  (POST/PUT/DELETE/create/update/delete) → only --help syntax check
#
# Generates: references/test-report.md
# Exit: 0=all pass / 1=has failures
# ============================================================================
set -uo pipefail

SKILL_DIR="${1:?Usage: bash test-skill-commands.sh <skill-dir>}"

if [ ! -d "$SKILL_DIR" ]; then
    echo "[FATAL] Skill directory not found: $SKILL_DIR"
    exit 1
fi
SKILL_DIR="$(cd "$SKILL_DIR" && pwd)"

SKILL_MD="$SKILL_DIR/SKILL.md"
TEST_VARS="$SKILL_DIR/templates/test-vars.json"
OUTPUT_FILE="$SKILL_DIR/references/test-report.md"

[ -f "$SKILL_MD" ] || { echo "[FATAL] SKILL.md not found"; exit 1; }
[ -f "$TEST_VARS" ] || { echo "[FATAL] templates/test-vars.json not found"; exit 1; }

SKILL_NAME=$(grep '^name:' "$SKILL_MD" 2>/dev/null | head -1 | sed 's/^name:[[:space:]]*//;s/^"//;s/"$//' || echo "unknown")

# Find entry file (.sh or .py)
ENTRY_FILE=""
for f in "$SKILL_DIR"/scripts/*.sh "$SKILL_DIR"/scripts/*.py; do
    [ -f "$f" ] && ENTRY_FILE="$f" && break
done
[ -n "$ENTRY_FILE" ] || { echo "[FATAL] No entry file (.sh or .py) in scripts/"; exit 1; }

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS_ARR=()

record() {
    local op="$1" type="$2" status="$3" note="$4"
    RESULTS_ARR+=("$(printf '| `%s` | %s | %s | %s |' "$op" "$type" "$status" "$note")")
    case "$status" in
        ✅*|✅) PASS_COUNT=$((PASS_COUNT + 1)) ;;
        ❌*|❌) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
        ⏭️*) SKIP_COUNT=$((SKIP_COUNT + 1)) ;;
    esac
}

# === Check environment ===
echo "=========================================="
echo " Functional Test"
echo " Skill: $SKILL_NAME"
echo " Path: $SKILL_DIR"
echo "=========================================="
echo ""

# Check python3
if command -v python3 &>/dev/null; then
    echo "✅ python3 available"
else
    echo "❌ python3 not found"
    exit 1
fi

# Check requests module
if python3 -c "import requests" 2>/dev/null; then
    echo "✅ requests module available"
else
    echo "⚠️ requests module not found — install: pip install requests"
fi

# === Read test cases ===
TC_COUNT=$(python3 -c "import sys,json; d=json.load(open(sys.argv[1])); print(len(d.get('test_cases',[])))" "$TEST_VARS" 2>/dev/null || echo "0")

if [ "$TC_COUNT" -eq 0 ]; then
    echo "[WARN] No test cases in templates/test-vars.json"
    record "(no test cases)" "N/A" "⏭️ 跳过" "test_cases 数组为空"
else
    echo "Found $TC_COUNT test case(s)"
    echo ""

    for i in $(seq 0 $((TC_COUNT - 1))); do
        TC_NAME=$(python3 -c "import sys,json; d=json.load(open(sys.argv[1])); i=int(sys.argv[2]); print(d['test_cases'][i].get('name',''))" "$TEST_VARS" "$i" 2>/dev/null)
        TC_CMD=$(python3 -c "import sys,json; d=json.load(open(sys.argv[1])); i=int(sys.argv[2]); print(d['test_cases'][i].get('command',''))" "$TEST_VARS" "$i" 2>/dev/null)
        TC_METHOD=$(python3 -c "import sys,json; d=json.load(open(sys.argv[1])); i=int(sys.argv[2]); print(d['test_cases'][i].get('method','GET'))" "$TEST_VARS" "$i" 2>/dev/null)
        TC_CATEGORY=$(python3 -c "import sys,json; d=json.load(open(sys.argv[1])); i=int(sys.argv[2]); print(d['test_cases'][i].get('category','readonly'))" "$TEST_VARS" "$i" 2>/dev/null)

        echo "--- [$((i+1))] $TC_NAME ---"
        echo "  Command: $TC_CMD"
        echo "  Method: $TC_METHOD | Category: $TC_CATEGORY"

        # Determine test mode:
        # readonly (GET/list/get) → run real call
        # mutable (POST/PUT/DELETE/create/update/delete) → only --help
        IS_READONLY=false
        case "$TC_METHOD" in
            GET) IS_READONLY=true ;;
        esac
        case "$TC_CATEGORY" in
            readonly) IS_READONLY=true ;;
            mutable) IS_READONLY=false ;;
        esac

        # Also check command for readonly keywords
        echo "$TC_CMD" | grep -qiE 'list|get|show|health|capability' && IS_READONLY=true

        if [ "$IS_READONLY" = true ]; then
            # Run real API call
            echo "  → Running real call..."
            OUTPUT=$(cd "$SKILL_DIR" && bash -c "$TC_CMD" 2>&1 || true)
            EC=$?

            if [ $EC -eq 0 ] && ! echo "$OUTPUT" | grep -qiE "error|failed|traceback|exception"; then
                record "$TC_CMD" "readonly" "✅ 通过" "真实调用成功"
                echo "  ✅ PASS"
            else
                # Check if syntax error
                if echo "$OUTPUT" | grep -qiE "unrecognized|invalid|usage:|argument"; then
                    record "$TC_CMD" "readonly" "❌ 失败" "语法错误: $(echo "$OUTPUT" | head -1)"
                    echo "  ❌ FAIL - syntax error"
                else
                    record "$TC_CMD" "readonly" "❌ 失败" "$(echo "$OUTPUT" | head -1)"
                    echo "  ❌ FAIL"
                fi
            fi
        else
            # Mutable operation — only --help syntax check
            echo "  → Mutable op — running --help only..."
            HELP_CMD=$(echo "$TC_CMD" | sed 's/\(--[a-z_-]*\s\)[^ ]*.*$/\1/' | head -1)
            # Extract subcommand from command (hcloud Service Operation or python3 script subcmd)
            SUBCMD=$(echo "$TC_CMD" | grep -oP '(?:python3\s+\S+|hcloud\s+\S+)\s+\K[a-z-]+' | head -1)
            
            # Determine how to run --help based on entry file type and command type
            ENTRY_EXT="${ENTRY_FILE##*.}"
            IS_HCLOUD_CMD=false
            IS_SDK_CMD=false
            echo "$TC_CMD" | grep -qiE '^hcloud\b' && IS_HCLOUD_CMD=true
            echo "$TC_CMD" | grep -qiE '^(python3|python|\.venv/bin/python)\b' && IS_SDK_CMD=true
            
            if [ "$IS_HCLOUD_CMD" = true ]; then
                # hcloud command — run hcloud Service Operation --help
                HC_SERVICE=$(echo "$TC_CMD" | grep -oP '^hcloud\s+\K\S+' | head -1)
                HC_OP=$(echo "$TC_CMD" | grep -oP '^hcloud\s+\S+\s+\K\S+' | head -1)
                if [ -n "$HC_SERVICE" ] && [ -n "$HC_OP" ]; then
                    HELP_OUTPUT=$(hcloud "$HC_SERVICE" "$HC_OP" --help 2>&1 || true)
                    if echo "$HELP_OUTPUT" | grep -qiE "usage:|Usage:|optional|options|参数"; then
                        record "$TC_CMD" "mutable(--help)" "✅ 通过" "hcloud 语法检查通过（变更类只做 --help）"
                        echo "  ✅ PASS (hcloud --help)"
                    else
                        record "$TC_CMD" "mutable(--help)" "❌ 失败" "hcloud 语法错误: $(echo "$HELP_OUTPUT" | head -1)"
                        echo "  ❌ FAIL - hcloud syntax error"
                    fi
                else
                    record "$TC_CMD" "mutable(--help)" "⏭️ 跳过" "无法提取 hcloud Service/Operation"
                    echo "  ⏭️ SKIP - cannot extract hcloud Service/Operation"
                fi
            elif [ "$IS_SDK_CMD" = true ]; then
                # SDK command (python3 -c "from huaweicloudsdk...") — run as-is for syntax check
                HELP_OUTPUT=$(cd "$SKILL_DIR" && bash -c "$TC_CMD" 2>&1 || true)
                if [ $PIPESTATUS -eq 0 ] && ! echo "$HELP_OUTPUT" | grep -qiE "error|failed|traceback|exception|USE_ERROR"; then
                    record "$TC_CMD" "mutable(--help)" "✅ 通过" "SDK 语法检查通过（变更类只做语法验证）"
                    echo "  ✅ PASS (SDK syntax)"
                else
                    record "$TC_CMD" "mutable(--help)" "❌ 失败" "SDK 语法错误: $(echo "$HELP_OUTPUT" | head -1)"
                    echo "  ❌ FAIL - SDK syntax error"
                fi
            elif [ "$ENTRY_EXT" = "sh" ]; then
                # .sh entry file — run bash script --help
                HELP_OUTPUT=$(cd "$SKILL_DIR" && bash "$(basename "$ENTRY_FILE")" --help 2>&1 || true)
                if [ $PIPESTATUS -eq 0 ] || echo "$HELP_OUTPUT" | grep -qiE "usage:|Usage:|help|帮助"; then
                    record "$TC_CMD" "mutable(--help)" "✅ 通过" "bash 脚本 --help 正常（变更类只做 --help）"
                    echo "  ✅ PASS (bash --help)"
                else
                    record "$TC_CMD" "mutable(--help)" "❌ 失败" "bash 脚本 --help 异常: $(echo "$HELP_OUTPUT" | head -1)"
                    echo "  ❌ FAIL - bash --help error"
                fi
            else
                # .py entry file — run python3 script --help
                if [ -n "$SUBCMD" ]; then
                    HELP_OUTPUT=$(cd "$SKILL_DIR" && python3 "$(basename "$ENTRY_FILE")" "$SUBCMD" --help 2>&1 || true)
                    if echo "$HELP_OUTPUT" | grep -qiE "usage:|optional arguments|options"; then
                        record "$TC_CMD" "mutable(--help)" "✅ 通过" "语法检查通过（变更类只做 --help）"
                        echo "  ✅ PASS (--help only)"
                    else
                        record "$TC_CMD" "mutable(--help)" "❌ 失败" "语法错误: $(echo "$HELP_OUTPUT" | head -1)"
                        echo "  ❌ FAIL - syntax error"
                    fi
                else
                    HELP_OUTPUT=$(cd "$SKILL_DIR" && python3 "$(basename "$ENTRY_FILE")" --help 2>&1 || true)
                    if echo "$HELP_OUTPUT" | grep -qiE "usage:|optional arguments|options"; then
                        record "$TC_CMD" "mutable(--help)" "✅ 通过" "脚本 --help 正常（未提取子命令）"
                        echo "  ✅ PASS (--help fallback)"
                    else
                        record "$TC_CMD" "mutable(--help)" "❌ 失败" "脚本 --help 异常"
                        echo "  ❌ FAIL"
                    fi
                fi
            fi
        fi
        echo ""
    done
fi

# === Generate test report ===
TOTAL=$((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))
REPORT_DATE=$(date +"%Y-%m-%d %H:%M:%S")
mkdir -p "$(dirname "$OUTPUT_FILE")"

cat > "$OUTPUT_FILE" << REPORT_HEADER
# 功能测试报告 — ${SKILL_NAME}

> 生成时间：${REPORT_DATE}
> 测试脚本：test-skill-commands.sh
> 测试用例数：${TOTAL}

## 测试结果汇总

| 指标 | 值 |
|------|-----|
| 总测试数 | ${TOTAL} |
| ✅ 通过 | ${PASS_COUNT} |
| ❌ 失败 | ${FAIL_COUNT} |
| ⏭️ 跳过 | ${SKIP_COUNT} |

## 测试详情

| 操作 | 测试类型 | 结果 | 备注 |
|------|----------|------|------|
REPORT_HEADER

for result in "${RESULTS_ARR[@]}"; do
    echo "$result" >> "$OUTPUT_FILE"
done

echo "" >> "$OUTPUT_FILE"
if [ "$FAIL_COUNT" -eq 0 ] && [ "$TOTAL" -gt 0 ]; then
    echo "**结论：✅ 全部 ${TOTAL} 项测试通过 — 可进入用户验收**" >> "$OUTPUT_FILE"
elif [ "$FAIL_COUNT" -gt 0 ]; then
    echo "**结论：❌ ${FAIL_COUNT}/${TOTAL} 项测试失败 — 需要修复后重新测试**" >> "$OUTPUT_FILE"
else
    echo "**结论：⏭️ 无实际测试执行**" >> "$OUTPUT_FILE"
fi

chmod 600 "$OUTPUT_FILE" 2>/dev/null || true

echo "=========================================="
echo " Test Results"
echo "=========================================="
echo "  PASS: $PASS_COUNT"
echo "  FAIL: $FAIL_COUNT"
echo "  SKIP: $SKIP_COUNT"
echo "  Report: $OUTPUT_FILE"
echo "=========================================="

[ "$FAIL_COUNT" -eq 0 ]
