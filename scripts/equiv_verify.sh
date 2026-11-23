#!/usr/bin/env bash
# Equivalent-proof verify: run the full test suite on a baseline worktree and a
# candidate worktree, then diff the failure sets. Passes only if the candidate's
# failure set is a strict subset of the baseline's (zero new failures).
#
# Usage:
#   scripts/equiv_verify.sh --baseline <dir> --candidate <dir> [--out <dir>]
#
# Design notes (lessons learned from p0-verify v2, 2026-11-14):
#   - pytest runs files in alphabetical order; a per-directory `timeout` that
#     kills pytest mid-run silently drops every file after the cut-off point,
#     and "never ran" looks identical to "passed" in the FAILED-grep. That gap
#     let 4 stale tests (encoding the removed get_backend('') fallback
#     contract) slip through full verification undetected.
#   - Therefore: per-directory timeouts are generous and configurable, AND the
#     collected-test count per directory is asserted equal on both sides and
#     non-zero. A truncated run aborts loudly instead of faking green.
set -u

VENV_PY="${VENV_PY:-}"
BASELINE="" CANDIDATE="" OUT="$(pwd)/equiv-verify-out"
# Per-directory timeout in seconds (default 3000 = measured tests/runtime full
# run ~35min on this host + headroom). Override via env for slower machines.
DIR_TIMEOUT="${DIR_TIMEOUT:-3000}"

while [ $# -gt 0 ]; do
  case "$1" in
    --baseline) BASELINE="$2"; shift 2 ;;
    --candidate) CANDIDATE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -z "$BASELINE" ] || [ -z "$CANDIDATE" ] && { echo "usage: $0 --baseline <dir> --candidate <dir> [--out <dir>]" >&2; exit 64; }
[ -n "$VENV_PY" ] || VENV_PY="$(cd "$CANDIDATE" && .venv/bin/python 2>/dev/null || echo python)"

mkdir -p "$OUT"
TOPS="tests/test_cloud_worker.py tests/test_codearts_bwrap_smoke.py tests/test_cubesandbox.py tests/test_llm_credential_multi_provider.py tests/test_no_conflict_markers.py tests/test_provider_config_dispatch.py tests/test_same_host_multi_worker.py tests/test_sandbox_alerts.py tests/test_sandbox_redact.py"

# run_dir <worktree> <failures-out> <counts-out>
#   failures-out: sorted unique "FAILED/ERROR" lines
#   counts-out:   "  <collected>  <d>" per directory, for the equality check
run_dir () {
  local wt="$1" fout="$2" cout="$3" d coll
  : > "$fout"; : > "$cout"
  cd "$wt" || return 1
  find . -name __pycache__ -type d -not -path './.git/*' -exec rm -rf {} + 2>/dev/null
  for d in api arch contracts insight js kernel mgmt runtime e2e; do
    [ -d "tests/$d" ] || continue
    echo "  ... $d" >&2
    local raw="$OUT/raw_$(basename "$wt")_$d.log"
    timeout "$DIR_TIMEOUT" "$VENV_PY" -m pytest "tests/$d" -q -p no:randomly > "$raw" 2>&1
    local rc=$?
    # Collected count from the summary line ("N passed, M failed..." tail).
    coll=$(grep -oE '[0-9]+ passed' "$raw" | tail -1 | grep -oE '[0-9]+')
    coll="${coll:-0}"
    if [ "$rc" -eq 124 ]; then
      echo "FATAL: tests/$d hit DIR_TIMEOUT (${DIR_TIMEOUT}s) - run truncated, results are NOT trustworthy" >&2
      echo "       raise DIR_TIMEOUT or shard the directory; collected-so-far: ${coll}" >&2
      exit 3
    fi
    [ "$coll" -gt 0 ] || { echo "FATAL: tests/$d collected 0 tests (wrong dir? collection error?)" >&2; exit 3; }
    echo "$coll $d" >> "$cout"
    grep -E '^FAILED|^ERROR' "$raw" | sed 's/ - .*//' >> "$fout" || true
    pkill -9 -f "pytest tests/$d" 2>/dev/null
    echo "    collected=$coll failures+=$(grep -cE '^FAILED|^ERROR' "$raw")" >&2
  done
  echo "  ... top-level" >&2
  local raw="$OUT/raw_$(basename "$wt")_toplevel.log"
  timeout 600 "$VENV_PY" -m pytest $TOPS -q -p no:randomly > "$raw" 2>&1
  grep -E '^FAILED|^ERROR' "$raw" | sed 's/ - .*//' >> "$fout" || true
  sort -u -o "$fout" "$fout"
  echo "  TOTAL-failures=$(wc -l < "$fout")" >&2
}

echo "== baseline: $BASELINE ==" >&2
run_dir "$BASELINE" "$OUT/base_failures.txt" "$OUT/base_counts.txt"
echo "== candidate: $CANDIDATE ==" >&2
run_dir "$CANDIDATE" "$OUT/new_failures.txt" "$OUT/new_counts.txt"

echo "=== collected-count check (must be identical, non-zero) ==="
if ! diff -q "$OUT/base_counts.txt" "$OUT/new_counts.txt" >/dev/null; then
  echo "FATAL: collected test counts differ between baseline and candidate:" >&2
  diff "$OUT/base_counts.txt" "$OUT/new_counts.txt" >&2
  exit 3
fi
echo "counts identical: $(paste -d' ' "$OUT/base_counts.txt" | tr '\n' ' ')"

echo "=== failure-set diff (candidate must be a subset of baseline) ==="
if diff "$OUT/base_failures.txt" "$OUT/new_failures.txt" > "$OUT/failures.diff"; then
  echo "identical failure sets"
else
  cat "$OUT/failures.diff"
fi
# Subset check: every candidate failure must appear in the baseline set.
comm -13 "$OUT/base_failures.txt" "$OUT/new_failures.txt" > "$OUT/new_only.txt"
if [ -s "$OUT/new_only.txt" ]; then
  echo "FAIL: candidate has $(wc -l < "$OUT/new_only.txt") failure(s) absent from baseline:" >&2
  cat "$OUT/new_only.txt" >&2
  exit 1
fi
echo "PASS: candidate failure set is a subset of baseline (fixed-count + zero new red)"
