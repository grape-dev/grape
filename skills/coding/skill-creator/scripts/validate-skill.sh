#!/usr/bin/env bash
# ============================================================================
# validate-skill.sh — Validate generated skill structure
# ============================================================================
# Usage: bash validate-skill.sh <skill-dir>
# Checks: frontmatter / required sections / file structure / no mock / no hardcoded addr
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
WARN=0

pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  ⚠️  WARN: $1"; WARN=$((WARN + 1)); }

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
grep -q '^tools:' "$SKILL_MD" && pass "tools field present" || warn "tools field missing (optional)"

# name matches directory name
FM_NAME=$(sed -n '2,/^---$/s/^name:[[:space:]]*//p' "$SKILL_MD" | head -1 | tr -d '"'"'")
if [ "$FM_NAME" = "$SKILL_NAME" ]; then
    pass "name matches directory name"
else
    fail "name '$FM_NAME' does not match directory '$SKILL_NAME'"
fi

# name format: huawei-cloud-{product}-{function}
if echo "$SKILL_NAME" | grep -qE '^huawei-cloud-[a-z0-9]+(-[a-z0-9]+)*$'; then
    pass "Naming convention: $SKILL_NAME (huawei-cloud-{product}-{function})"
else
    fail "Naming convention: $SKILL_NAME (should be huawei-cloud-{product}-{function}, e.g. huawei-cloud-ecs-query)"
fi

# === 3. Required sections ===
echo ""
echo "--- Required Sections ---"
for section in "安全硬约束" "Overview|概述" "Prerequisites|前置条件" "Workflow|工作流" "Core Commands|核心命令" "Parameter Confirmation|参数确认" "KooCLI Command Format Standard" "Reference Documents|参考文档"; do
    if grep -qiE "^##[[:space:]].*($section)" "$SKILL_MD"; then
        pass "${section%%\\*} section present"
    else
        fail "${section%%\\*} section missing"
    fi
done

# === 4. File structure ===
echo ""
echo "--- File Structure ---"

# Find entry file (.sh or .py)
ENTRY_FILE=""
for f in "$SKILL_DIR"/scripts/*.sh "$SKILL_DIR"/scripts/*.py; do
    [ -f "$f" ] && ENTRY_FILE="$f" && break
done
if [ -n "$ENTRY_FILE" ]; then
    pass "Entry file exists: scripts/$(basename "$ENTRY_FILE")"
    [ -x "$ENTRY_FILE" ] && pass "Entry file is executable" || warn "Entry file not executable (chmod +x recommended)"
else
    fail "No entry file (.sh or .py) in scripts/"
fi

# references/iam-policies.md
[ -f "$SKILL_DIR/references/iam-policies.md" ] && pass "references/iam-policies.md exists" || fail "references/iam-policies.md missing"

# templates/test-vars.json
[ -f "$SKILL_DIR/templates/test-vars.json" ] && pass "templates/test-vars.json exists" || fail "templates/test-vars.json missing"

# requirements.txt (optional - enhanced)
[ -f "$SKILL_DIR/requirements.txt" ] && pass "requirements.txt exists" || warn "requirements.txt missing (optional)"

# === 4b. references files kebab-case ===
echo ""
echo "--- References Kebab-case ---"
KEBAB_OK=0
KEBAB_TOTAL=0
while IFS= read -r -d '' ref; do
    KEBAB_TOTAL=$((KEBAB_TOTAL + 1))
    base=$(basename "$ref")
    if echo "$base" | grep -qE '^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9]+$'; then
        KEBAB_OK=$((KEBAB_OK + 1))
    fi
done < <(find "$SKILL_DIR/references" -type f -print0 2>/dev/null)
if [ "$KEBAB_TOTAL" -gt 0 ] && [ "$KEBAB_OK" -eq "$KEBAB_TOTAL" ]; then
    pass "All reference files use kebab-case ($KEBAB_OK/$KEBAB_TOTAL)"
else
    fail "Some reference files don't use kebab-case ($KEBAB_OK/$KEBAB_TOTAL)"
fi

# === 4c. Huawei Cloud dependency check ===
echo ""
echo "--- Huawei Cloud Dependency ---"
HCLOUD_FOUND=false
if [ -n "$ENTRY_FILE" ]; then
    # Check entry file
    if grep -qiE 'hcloud|huaweicloudsdk|HUAWEI_AK|HUAWEI_SK|HW_ACCESS_KEY|HW_SECRET_KEY' "$ENTRY_FILE" 2>/dev/null; then
        HCLOUD_FOUND=true
    fi
fi
# Also check SKILL.md for hcloud commands
if [ "$HCLOUD_FOUND" = false ]; then
    if grep -qiE 'hcloud |hcloud$' "$SKILL_MD" 2>/dev/null; then
        HCLOUD_FOUND=true
    fi
fi
if [ "$HCLOUD_FOUND" = true ]; then
    pass "Script depends on Huawei Cloud (hcloud/huaweicloudsdk/AK/SK found)"
else
    fail "Script does not depend on Huawei Cloud (no hcloud/huaweicloudsdk/AK/SK found in entry file)"
fi

# === 4d. Write operation confirmation ===
echo ""
echo "--- Write Operation Confirmation ---"
if grep -qiE '(Create|Delete|Update|创建|删除|更新)' "$SKILL_MD" 2>/dev/null; then
    if grep -qiE '确认|confirm|用户确认' "$SKILL_MD" 2>/dev/null; then
        pass "Write operation confirmation mentioned"
    else
        warn "Write operations found but no confirmation prompt"
    fi
else
    pass "No write operations (readonly skill)"
fi

# === 5. No hardcoded API address ===
echo ""
echo "--- Hardcoded Address Check ---"
HARDCODED=$(grep -rnE 'https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+' "$SKILL_MD" 2>/dev/null | grep -v "example.com\|localhost" | head -5)
if [ -z "$HARDCODED" ]; then
    pass "No hardcoded API address in SKILL.md"
else
    fail "Hardcoded API address found in SKILL.md:"
    echo "$HARDCODED"
fi

# === 6. No mock code ===
echo ""
echo "--- Mock Code Check ---"
MOCK_FOUND=$(grep -rniE 'mock_data|--mock|MOCK_MODE|fake_data|假数据|模拟数据' "$SKILL_DIR" 2>/dev/null | grep -v "validate-skill.sh\|test-skill-commands.sh\|严禁\|禁止\|forbidden\|prohibit\|不.*mock\|no.*mock\|not.*mock" | head -5)
if [ -z "$MOCK_FOUND" ]; then
    pass "No mock code found"
else
    fail "Mock code found:"
    echo "$MOCK_FOUND"
fi

# === 7. Size constraints ===
echo ""
echo "--- Size Constraints ---"

# SKILL.md <= 500 lines
SKILL_LINES=$(wc -l < "$SKILL_MD")
if [ "$SKILL_LINES" -le 500 ]; then
    pass "SKILL.md ≤ 500 lines ($SKILL_LINES lines)"
else
    fail "SKILL.md exceeds 500 lines ($SKILL_LINES lines)"
fi

# Total file count <= 50
TOTAL_FILES=$(find "$SKILL_DIR" -type f 2>/dev/null | wc -l)
if [ "$TOTAL_FILES" -le 50 ]; then
    pass "Total file count ≤ 50 ($TOTAL_FILES files)"
else
    fail "Total file count exceeds 50 ($TOTAL_FILES files)"
fi

# Total size <= 40MB
TOTAL_BYTES=$(find "$SKILL_DIR" -type f -exec wc -c {} + 2>/dev/null | tail -1 | awk '{print $1}')
TOTAL_MB=$(( (TOTAL_BYTES + 1048575) / 1048576 ))
if [ "$TOTAL_BYTES" -le 41943040 ]; then
    pass "Total size ≤ 40MB (${TOTAL_MB}MB)"
else
    fail "Total size exceeds 40MB (${TOTAL_MB}MB)"
fi

# === Summary ===
echo ""
echo "============================================"
echo "  Validation Summary"
echo "  PASS: $PASS  FAIL: $FAIL  WARN: $WARN"
echo "============================================"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
