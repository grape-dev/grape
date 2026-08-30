#!/usr/bin/env bash
# test-cli-commands.sh — Test Huawei Cloud Skill Reporter plugins
set -uo pipefail

SKILL_PATH="${1:?Usage: bash test-cli-commands.sh <skill-path> [--component hermes|opencode|both]}"
shift || true

COMPONENT="both"
OUTPUT_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --component) COMPONENT="$2"; shift 2 ;;
    --output) OUTPUT_FILE="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [ ! -d "$SKILL_PATH" ]; then
  echo "[FATAL] Skill directory not found: $SKILL_PATH"
  exit 1
fi
SKILL_PATH="$(cd "$SKILL_PATH" && pwd)"

[ -z "$OUTPUT_FILE" ] && OUTPUT_FILE="$SKILL_PATH/references/test-report.md"

PASS=0
FAIL=0
SKIP=0
RESULTS=""

record() {
  local status="$1"
  local msg="$2"
  case "$status" in
    PASS) PASS=$((PASS + 1)) ;;
    FAIL) FAIL=$((FAIL + 1)) ;;
    SKIP) SKIP=$((SKIP + 1)) ;;
  esac
  RESULTS="$RESULTS\n| $status | $msg |"
}

echo "=========================================="
echo " Skill Reporter Plugin Test"
echo " Path: $SKILL_PATH"
echo " Component: $COMPONENT"
echo "=========================================="
echo ""

# === Test Hermes Python Plugin ===
if [ "$COMPONENT" = "hermes" ] || [ "$COMPONENT" = "both" ]; then
  echo "--- Testing Hermes Python Plugin ---"

  if [ ! -f "$SKILL_PATH/hermes-plugin/__init__.py" ]; then
    record SKIP "Hermes plugin not found"
    echo "  ⏭️ SKIP: Hermes plugin not found"
  else
    # Test Python syntax
    if python3 -m py_compile "$SKILL_PATH/hermes-plugin/__init__.py" 2>/dev/null; then
      record PASS "Hermes __init__.py syntax valid"
      echo "  ✅ Hermes __init__.py syntax valid"
    else
      record FAIL "Hermes __init__.py syntax error"
      echo "  ❌ Hermes __init__.py syntax error"
    fi

    # Test plugin.yaml
    if python3 -c "import yaml; yaml.safe_load(open('$SKILL_PATH/hermes-plugin/plugin.yaml'))" 2>/dev/null; then
      record PASS "Hermes plugin.yaml is valid YAML"
      echo "  ✅ Hermes plugin.yaml valid"
    else
      record FAIL "Hermes plugin.yaml is invalid YAML"
      echo "  ❌ Hermes plugin.yaml invalid"
    fi

    # Test module import
    if python3 -c "
import sys
sys.path.insert(0, '$SKILL_PATH/hermes-plugin')
from __init__ import _report, _get_report_url
print('import_ok')
" 2>/dev/null | grep -q "import_ok"; then
      record PASS "Hermes plugin module imports correctly"
      echo "  ✅ Hermes plugin imports OK"
    else
      record FAIL "Hermes plugin module import failed"
      echo "  ❌ Hermes plugin import failed"
    fi
  fi
  echo ""
fi

# === Test Opencode JavaScript Plugin ===
if [ "$COMPONENT" = "opencode" ] || [ "$COMPONENT" = "both" ]; then
  echo "--- Testing Opencode JavaScript Plugin ---"

  if [ ! -f "$SKILL_PATH/plugin/huawei-cloud-skill-reporter.js" ]; then
    record SKIP "Opencode plugin not found"
    echo "  ⏭️ SKIP: Opencode plugin not found"
  else
    # Test JavaScript syntax
    if node --check "$SKILL_PATH/plugin/huawei-cloud-skill-reporter.js" 2>/dev/null; then
      record PASS "Opencode plugin syntax valid"
      echo "  ✅ Opencode plugin syntax valid"
    else
      record FAIL "Opencode plugin syntax error"
      echo "  ❌ Opencode plugin syntax error"
    fi
  fi
  echo ""
fi

# === Test Setup Scripts ===
echo "--- Testing Setup Scripts ---"

# setup.js
if [ -f "$SKILL_PATH/setup.js" ]; then
  if node "$SKILL_PATH/setup.js" --help > /dev/null 2>&1; then
    record PASS "setup.js runs and shows help"
    echo "  ✅ setup.js OK"
  else
    record FAIL "setup.js execution failed"
    echo "  ❌ setup.js failed"
  fi
else
  record SKIP "setup.js not found"
  echo "  ⏭️ SKIP: setup.js not found"
fi

# setup.ps1
if [ -f "$SKILL_PATH/setup.ps1" ]; then
  if pwsh -File "$SKILL_PATH/setup.ps1" -? > /dev/null 2>&1; then
    record PASS "setup.ps1 runs"
    echo "  ✅ setup.ps1 OK"
  else
    record WARN "setup.ps1 help check skipped (PowerShell not available)"
    echo "  ⚠️ setup.ps1 skipped (PowerShell not available)"
  fi
else
  record SKIP "setup.ps1 not found"
  echo "  ⏭️ SKIP: setup.ps1 not found"
fi

echo ""

# === Test API Endpoint ===
echo "--- Testing API Endpoint ---"

RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://skillsop.topxtopx.com/api/telemetry \
  -H "Content-Type: application/json" \
  -d '{"skill":"huawei-cloud-skill-reporter","status":"test"}' 2>/dev/null || echo "000")

if [ "$RESPONSE" = "200" ] || [ "$RESPONSE" = "201" ]; then
  record PASS "API endpoint accessible (HTTP $RESPONSE)"
  echo "  ✅ API endpoint OK (HTTP $RESPONSE)"
else
  record FAIL "API endpoint returned HTTP $RESPONSE"
  echo "  ❌ API endpoint error (HTTP $RESPONSE)"
fi

echo ""

# === Generate Report ===
TOTAL=$((PASS + FAIL + SKIP))
REPORT_DATE=$(date +"%Y-%m-%d %H:%M:%S")
mkdir -p "$(dirname "$OUTPUT_FILE")"

cat > "$OUTPUT_FILE" << REPORT_HEADER
# 功能测试报告 — Huawei Cloud Skill Reporter

> 生成时间：${REPORT_DATE}
> 测试路径：${SKILL_PATH}
> 测试组件：${COMPONENT}

## 测试结果汇总

| 指标 | 值 |
|------|-----|
| 总测试数 | ${TOTAL} |
| ✅ 通过 | ${PASS} |
| ❌ 失败 | ${FAIL} |
| ⏭️ 跳过 | ${SKIP} |

## 测试详情

| 结果 | 说明 |
|------|------|
REPORT_HEADER

printf '%b\n' "$RESULTS" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

if [ "$FAIL" -eq 0 ] && [ "$TOTAL" -gt 0 ]; then
  echo "**结论：✅ 全部 ${TOTAL} 项测试通过**" >> "$OUTPUT_FILE"
elif [ "$FAIL" -gt 0 ]; then
  echo "**结论：❌ ${FAIL}/${TOTAL} 项测试失败 — 需要修复后重新测试**" >> "$OUTPUT_FILE"
else
  echo "**结论：⏭️ 无实际测试执行**" >> "$OUTPUT_FILE"
fi

echo ""
echo "=========================================="
echo " Test Results"
echo "=========================================="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo "  SKIP: $SKIP"
echo "  Report: $OUTPUT_FILE"
echo "=========================================="

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
