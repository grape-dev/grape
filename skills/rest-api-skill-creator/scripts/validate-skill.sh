#!/usr/bin/env bash
# ============================================================================
# validate-skill.sh — rest-api-skill-creator 自测结构校验脚本
# ============================================================================
# Usage: bash validate-skill.sh <skill-dir>
# Checks: frontmatter / required sections / file structure / no mock / no hardcoded addr / no GitCode / requests not urllib / test-vars.json / SKILL.md size
# Exit: 0=all pass / 1=has failures
# ============================================================================
set -uo pipefail

SKILL_DIR="${1:?Usage: bash validate-skill.sh <skill-dir>}"

if [ ! -d "$SKILL_DIR" ]; then
    echo "[FATAL] Skill directory not found: $SKILL_DIR"
    exit 1
fi
SKILL_DIR="$(cd "$SKILL_DIR" && pwd)"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

SKILL_MD="$SKILL_DIR/SKILL.md"
SKILL_NAME="$(basename "$SKILL_DIR")"

echo "============================================"
echo "  Validate Skill: $SKILL_NAME"
echo "  Path: $SKILL_DIR"
echo "============================================"

# === 1. SKILL.md exists ===
echo ""
echo "--- SKILL.md ---"
[ -f "$SKILL_MD" ] && pass "SKILL.md exists" || { fail "SKILL.md missing"; exit 1; }

# === 2. Frontmatter ===
echo ""
echo "--- Frontmatter ---"
grep -q '^---$' "$SKILL_MD" && pass "YAML Frontmatter present" || fail "Frontmatter missing"
grep -q '^name:' "$SKILL_MD" && pass "name field present" || fail "name field missing"
grep -q '^description:' "$SKILL_MD" && pass "description field present" || fail "description field missing"
grep -q '^version:' "$SKILL_MD" && pass "version field present" || fail "version field missing"
grep -q '^triggers:' "$SKILL_MD" && pass "triggers field present" || fail "triggers field missing"
grep -q '^tags:' "$SKILL_MD" && pass "tags field present" || fail "tags field missing"
grep -q '^tools:' "$SKILL_MD" && pass "tools field present" || fail "tools field missing"

# name matches directory name
FM_NAME=$(sed -n '2,/^---$/s/^name:[[:space:]]*//p' "$SKILL_MD" | head -1 | tr -d '"'"'")
if [ "$FM_NAME" = "$SKILL_NAME" ]; then
    pass "name matches directory name"
else
    fail "name '$FM_NAME' does not match directory '$SKILL_NAME'"
fi

# name format: kebab-case
if echo "$SKILL_NAME" | grep -qE '^[a-z0-9][a-z0-9-]*$'; then
    pass "Naming convention: $SKILL_NAME (kebab-case)"
else
    fail "Naming convention: $SKILL_NAME (should be kebab-case)"
fi

# === 3. Required sections ===
echo ""
echo "--- Required Sections ---"
SECTIONS="安全硬约束 评论规范 Configuration 场景 知识 步骤 判断标准 输出规范 参考 分析结论 任务结束"
for section in $SECTIONS; do
    if grep -qE "^##.*${section}" "$SKILL_MD"; then
        pass "${section} section present"
    else
        fail "${section} section missing"
    fi
done

# === 4. File structure ===
echo ""
echo "--- File Structure ---"

# Python entry file
ENTRY_FILE=""
for f in "$SKILL_DIR"/scripts/*.py; do
    if [ -f "$f" ]; then
        ENTRY_FILE="$f"
        break
    fi
done
if [ -n "$ENTRY_FILE" ]; then
    pass "Entry file exists: scripts/$(basename "$ENTRY_FILE")"
    [ -x "$ENTRY_FILE" ] && pass "Entry file is executable" || fail "Entry file not executable (chmod +x)"
else
    fail "No entry file (.py) in scripts/"
fi

# references/api-reference.md
[ -f "$SKILL_DIR/references/api-reference.md" ] && pass "references/api-reference.md exists" || fail "references/api-reference.md missing"

# templates/test-vars.json
[ -f "$SKILL_DIR/templates/test-vars.json" ] && pass "templates/test-vars.json exists" || fail "templates/test-vars.json missing"

# requirements.txt
[ -f "$SKILL_DIR/requirements.txt" ] && pass "requirements.txt exists" || fail "requirements.txt missing"

# === 5. No mock code ===
echo ""
echo "--- Mock Check ---"
if grep -riE 'mock|MOCK_DATA|fake_data|假数据' "$SKILL_DIR/scripts/" --include="*.py" 2>/dev/null | grep -v 'spec_check\|validate' | head -3 | grep -q .; then
    fail "Mock code found:"
    grep -riE 'mock|MOCK_DATA|fake_data|假数据' "$SKILL_DIR/scripts/" --include="*.py" 2>/dev/null | grep -v 'spec_check\|validate' | head -3
else
    pass "No mock code found"
fi

# === 6. No hardcoded IP address ===
echo ""
echo "--- Hardcoded Address Check ---"
if grep -qE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$SKILL_MD" 2>/dev/null; then
    fail "Hardcoded IP address found in SKILL.md:"
    grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$SKILL_MD" | head -3
else
    pass "No hardcoded IP address in SKILL.md"
fi

if [ -f "$SKILL_DIR/references/api-reference.md" ]; then
    if grep -qE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$SKILL_DIR/references/api-reference.md" 2>/dev/null; then
        fail "Hardcoded IP address found in api-reference.md:"
        grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$SKILL_DIR/references/api-reference.md" | head -3
    else
        pass "No hardcoded IP address in api-reference.md"
    fi
fi

# === 7. No GitCode in safety constraints ===
echo ""
echo "--- GitCode Check ---"
# Extract safety constraint section
SAFETY_SECTION=$(sed -n '/^##.*安全硬约束/,/^## /p' "$SKILL_MD")
if echo "$SAFETY_SECTION" | grep -q 'GitCode'; then
    fail "GitCode found in safety constraints section"
else
    pass "No GitCode in safety constraints"
fi

# === 8. Script uses requests not urllib ===
echo ""
echo "--- HTTP Library Check ---"
if [ -n "$ENTRY_FILE" ]; then
    if grep -q 'import requests' "$ENTRY_FILE"; then
        pass "Script uses requests library"
    else
        fail "Script does not use requests library"
    fi
    if grep -q 'import urllib' "$ENTRY_FILE"; then
        fail "Script uses urllib (should use requests)"
    else
        pass "Script does not use urllib"
    fi
fi

# === 9. test-vars.json no sensitive fields ===
echo ""
echo "--- test-vars.json Sensitive Fields ---"
if [ -f "$SKILL_DIR/templates/test-vars.json" ]; then
    for field in base_url cred_prefix auth_type base_url_env; do
        if grep -q "\"$field\"" "$SKILL_DIR/templates/test-vars.json"; then
            fail "test-vars.json contains sensitive field: $field"
        else
            pass "test-vars.json does not contain: $field"
        fi
    done
fi

# === 10. test-vars.json field completeness ===
echo ""
echo "--- test-vars.json Field Completeness ---"
if [ -f "$SKILL_DIR/templates/test-vars.json" ]; then
    python3 -c "
import json, sys
with open('$SKILL_DIR/templates/test-vars.json') as f:
    tv = json.load(f)
req = ['id','name','command','method','category','description']
all_ok = True
for i, tc in enumerate(tv.get('test_cases', [])):
    missing = [f for f in req if f not in tc]
    if missing:
        print('  FAIL: TC%d missing: %s' % (i+1, ', '.join(missing)))
        all_ok = False
if all_ok:
    print('  PASS: All test cases have required fields (%d cases)' % len(tv.get('test_cases', [])))
    sys.exit(0)
else:
    sys.exit(1)
" && PASS=$((PASS + 1)) || FAIL=$((FAIL + 1))
fi

# === 11. SKILL.md line count ===
echo ""
echo "--- Size Check ---"
SKILL_LINES=$(wc -l < "$SKILL_MD")
if [ "$SKILL_LINES" -le 500 ]; then
    pass "SKILL.md <= 500 lines ($SKILL_LINES lines)"
else
    fail "SKILL.md exceeds 500 lines ($SKILL_LINES lines)"
fi

# File count
TOTAL_FILES=$(find "$SKILL_DIR" -type f | wc -l)
if [ "$TOTAL_FILES" -le 50 ]; then
    pass "Total file count <= 50 ($TOTAL_FILES files)"
else
    fail "Total file count exceeds 50 ($TOTAL_FILES files)"
fi

# === Summary ===
echo ""
echo "============================================"
echo "  Summary: $PASS PASS, $FAIL FAIL"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    exit 0
fi
