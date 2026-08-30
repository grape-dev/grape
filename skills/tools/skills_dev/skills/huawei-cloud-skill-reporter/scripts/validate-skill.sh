#!/usr/bin/env bash
# validate-skill.sh — Validate Huawei Cloud Skill Reporter structure
set -euo pipefail

SKILL_DIR="${1:-.}"
PASS=0
FAIL=0
WARN=0

pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); return 0; }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); return 1; }
warn() { echo "  ⚠️  WARN: $1"; WARN=$((WARN + 1)); return 0; }

echo "============================================"
echo "  Validate Skill Reporter: $(basename "$SKILL_DIR")"
echo "============================================"

# === CRITICAL Checks ===
echo ""
echo "--- Critical Checks ---"

[ -f "$SKILL_DIR/SKILL.md" ] && pass "SKILL.md exists" || fail "SKILL.md missing"

if [ -f "$SKILL_DIR/SKILL.md" ]; then
    # YAML Frontmatter
    grep -q '^---$' "$SKILL_DIR/SKILL.md" && pass "YAML Frontmatter present" || fail "Frontmatter missing"
    grep -q '^name:' "$SKILL_DIR/SKILL.md" && pass "name field present" || fail "name field missing"
    grep -q '^description:' "$SKILL_DIR/SKILL.md" && pass "description field present" || fail "description field missing"

    FRONTMATTER_NAME=$(sed -n '2,/^---$/s/^name:[[:space:]]*//p' "$SKILL_DIR/SKILL.md" | head -n 1 | tr -d '"' | tr -d "'")
    if [ "$FRONTMATTER_NAME" = "$(basename "$SKILL_DIR")" ]; then
        pass "name field matches directory name"
    else
        fail "name field '$FRONTMATTER_NAME' does not match directory '$(basename "$SKILL_DIR")'" || true
    fi

    if grep -q '^version:' "$SKILL_DIR/SKILL.md" 2>/dev/null; then
        warn "version field must be removed under Huawei Cloud Skill Specification"
    else
        pass "version field absent"
    fi

    grep -qiE '(Triggers include:|触发词|触发条件|Use when)' "$SKILL_DIR/SKILL.md" && pass "description includes trigger words" || warn "description trigger words missing"

    # SEC-001 — No hardcoded AK/SK
    _ak="access[_-]?key"
    _sk="secret[_-]?key"
    _pair="(^|[^[:alnum:]_])(ak|sk)"
    _value="[[:space:]]*[:=][[:space:]]*['\"]?[[:alnum:]_+/=-]{8,}"
    _sec001_pattern="(${_ak}|${_sk}|${_pair})${_value}"
    _sec001_whitelist='\b(forbidden|never|prohibit|禁止|不得|检测|detection|scan|pattern|your[-_]|example|placeholder)\b|<YOUR'
    if grep -RniE "${_sec001_pattern}" "$SKILL_DIR" 2>/dev/null | grep -viE "${_sec001_whitelist}" > /dev/null; then
        fail "[SEC-001] Possible hardcoded AK/SK literal values found" || true
    else
        pass "[SEC-001] No hardcoded AK/SK literal values"
    fi
fi

# Required sections
for section in "Overview" "Prerequisites" "Workflow" "Core Commands" "Parameter Confirmation"; do
    if grep -qiE "^##[[:space:]].*($section)" "$SKILL_DIR/SKILL.md"; then
        pass "$section section present"
    else
        fail "$section section missing" || true
    fi
done

# References section
grep -qiE '^##[[:space:]].*References' "$SKILL_DIR/SKILL.md" && pass "References section present" || fail "References section missing"

# === Plugin Files Checks ===
echo ""
echo "--- Plugin Files Checks ---"

# Hermes Python plugin
[ -f "$SKILL_DIR/hermes-plugin/__init__.py" ] && pass "Hermes __init__.py exists" || fail "Hermes __init__.py missing"
[ -f "$SKILL_DIR/hermes-plugin/plugin.yaml" ] && pass "Hermes plugin.yaml exists" || fail "Hermes plugin.yaml missing"

# Opencode JavaScript plugin
[ -f "$SKILL_DIR/plugin/huawei-cloud-skill-reporter.js" ] && pass "Opencode JS plugin exists" || fail "Opencode JS plugin missing"

# Setup scripts
[ -f "$SKILL_DIR/setup.js" ] && pass "setup.js exists" || fail "setup.js missing"
[ -f "$SKILL_DIR/setup.ps1" ] && pass "setup.ps1 exists" || fail "setup.ps1 missing"
[ -f "$SKILL_DIR/setup.sh" ] && pass "setup.sh exists" || fail "setup.sh missing"

# === References Checks ===
echo ""
echo "--- References Checks ---"

[ -f "$SKILL_DIR/references/iam-policies.md" ] && pass "references/iam-policies.md exists" || fail "references/iam-policies.md missing"
[ -f "$SKILL_DIR/references/dataflow-diagram.md" ] && pass "references/dataflow-diagram.md exists" || fail "references/dataflow-diagram.md missing"
[ -f "$SKILL_DIR/references/verification-method.md" ] && pass "references/verification-method.md exists" || warn "references/verification-method.md missing"
[ -f "$SKILL_DIR/references/acceptance-criteria.md" ] && pass "references/acceptance-criteria.md exists" || warn "references/acceptance-criteria.md missing"

# === Script Checks ===
echo ""
echo "--- Script Checks ---"

[ -f "$SKILL_DIR/scripts/validate-skill.sh" ] && pass "scripts/validate-skill.sh exists" || warn "scripts/validate-skill.sh missing"

# === Template Checks ===
echo ""
echo "--- Template Checks ---"

[ -f "$SKILL_DIR/templates/test-vars.json" ] && pass "templates/test-vars.json exists" || warn "templates/test-vars.json missing"

if [ -f "$SKILL_DIR/templates/test-vars.json" ]; then
    if python3 -c "import json; json.load(open('$SKILL_DIR/templates/test-vars.json'))" 2>/dev/null; then
        pass "test-vars.json is valid JSON"
    else
        fail "test-vars.json is invalid JSON"
    fi
fi

# === Low Priority Checks ===
echo ""
echo "--- Low Priority Checks ---"

# Naming convention
SKILL_NAME=$(basename "$SKILL_DIR")
if echo "$SKILL_NAME" | grep -qE '^huawei-cloud-[a-z0-9]+(-[a-z0-9]+)*$'; then
    pass "Naming convention: $SKILL_NAME"
else
    warn "Naming convention check: $SKILL_NAME"
fi

# Size constraints
TOTAL_FILES=$(find "$SKILL_DIR" -type f 2>/dev/null | wc -l)
if [ "$TOTAL_FILES" -le 30 ]; then
    pass "Total file count ≤ 30 ($TOTAL_FILES files)"
else
    fail "Total file count exceeds 30 ($TOTAL_FILES files)"
fi

if [ -f "$SKILL_DIR/SKILL.md" ]; then
    SKILL_LINES=$(wc -l < "$SKILL_DIR/SKILL.md")
    if [ "$SKILL_LINES" -le 500 ]; then
        pass "SKILL.md lines ≤ 500 ($SKILL_LINES lines)"
    else
        fail "SKILL.md exceeds 500 lines ($SKILL_LINES lines)"
    fi
fi

# Extension checks
ALLOWED_EXTENSIONS="md mdx txt json json5 yaml yml toml js cjs mjs ts tsx jsx py sh ps1 psm1 psd1 r rb go rs swift kt java cs cpp c h hpp sql csv tsv ini cfg conf env properties dat xml html css scss sass svg"
INVALID_FILES=""
while IFS= read -r -d '' file; do
    base=$(basename "$file")
    if [[ "$base" == *.* ]]; then
        ext=${base##*.}
        ext=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')
    else
        ext=""
    fi
    case " $ALLOWED_EXTENSIONS " in
        *" $ext "*) ;;
        *) INVALID_FILES="$INVALID_FILES $base" ;;
    esac
done < <(find "$SKILL_DIR" -type f -print0 2>/dev/null)
if [ -z "$INVALID_FILES" ]; then
    pass "All files use allowed extensions"
else
    fail "Files with invalid extensions:$INVALID_FILES"
fi

# === Summary ===
echo ""
echo "============================================"
echo "  Validation Summary"
echo "  PASS: $PASS  FAIL: $FAIL  WARN: $WARN"
echo "============================================"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
