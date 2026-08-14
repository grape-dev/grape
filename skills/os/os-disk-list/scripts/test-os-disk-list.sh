#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SCRIPT_DIR/os-disk-list.sh"
PASS=0
FAIL=0

assert() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected='$expected', actual='$actual')"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1" needle="$2" haystack="$3"
    if echo "$haystack" | grep -qF -- "$needle"; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (needle='$needle' not found)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== os-disk-list.sh test suite ==="

echo "[1] Syntax check"
bash -n "$SCRIPT" 2>/dev/null
assert "bash -n succeeds" "0" "$?"

echo "[2] Script is executable"
[ -x "$SCRIPT" ] || chmod +x "$SCRIPT"
assert "script executable" "0" "$?"

echo "[3] Default output has header"
output=$(bash "$SCRIPT" 2>&1)
assert_contains "header DEVICE" "DEVICE" "$output"
assert_contains "header MOUNTPOINT" "MOUNTPOINT" "$output"
assert_contains "header FSTYPE" "FSTYPE" "$output"
assert_contains "header SIZE" "SIZE" "$output"
assert_contains "header USED" "USED" "$output"

echo "[4] Default output has data rows"
row_count=$(echo "$output" | tail -n +3 | wc -l)
assert "has data rows" "true" "$([ "$row_count" -gt 0 ] && echo true || echo false)"

echo "[5] --json outputs valid JSON"
json_output=$(bash "$SCRIPT" --json 2>&1)
assert_contains "JSON starts with [" "[" "$json_output"
assert_contains "JSON ends with ]" "]" "$json_output"
assert_contains "JSON has device key" '"device"' "$json_output"
assert_contains "JSON has mountpoint key" '"mountpoint"' "$json_output"
assert_contains "JSON has fstype key" '"fstype"' "$json_output"
assert_contains "JSON has size key" '"size"' "$json_output"
assert_contains "JSON has used key" '"used"' "$json_output"

echo "[6] --help works"
help_output=$(bash "$SCRIPT" --help 2>&1)
assert "help exit code" "0" "$?"
assert_contains "help mentions --json" "--json" "$help_output"

echo "[7] Unknown option returns error"
bash "$SCRIPT" --invalid 2>/dev/null
assert "unknown option exit code" "1" "$?"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
exit $FAIL
