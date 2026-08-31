#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_SCRIPT="${SCRIPT_DIR}/session.sh"
ADAPTERS_DIR="${SCRIPT_DIR}/adapters"

show_usage() {
  echo "usage: run_skill.sh <skill_path> [options]"
  echo ""
  echo "options:"
  echo "  --agent <name>       Agent adapter to use (default: opencode)"
  echo "  --timeout <seconds>  Max run time before killing (default: 300)"
  echo "  --input <prompt>     User prompt to pass to the Agent"
  echo "  --session-dir <dir>  Override session storage root (default: ~/.fetch-pr-skill/sessions)"
  echo "  --help               Show usage"
  exit 0
}

AGENT="opencode"
TIMEOUT=300
ADAPTER_INPUT=""
CUSTOM_SESSION_ROOT=""
PR_URL=""
SKILL_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      AGENT="$2"
      shift 2
      ;;
    --timeout)
      TIMEOUT="$2"
      shift 2
      ;;
    --input)
      ADAPTER_INPUT="$2"
      shift 2
      ;;
    --session-dir)
      CUSTOM_SESSION_ROOT="$2"
      shift 2
      ;;
    --pr-url)
      PR_URL="$2"
      shift 2
      ;;
    --help)
      show_usage
      ;;
    --*)
      echo "error: unknown option: $1" >&2
      exit 1
      ;;
    *)
      if [[ -n "$SKILL_PATH" ]]; then
        echo "error: unexpected argument: $1" >&2
        exit 1
      fi
      SKILL_PATH="$1"
      shift
      ;;
  esac
done

if [[ -z "$SKILL_PATH" ]]; then
  echo "error: skill_path is required" >&2
  exit 1
fi

echo "==> Resolving skill path..."
if [[ ! -d "$SKILL_PATH" ]]; then
  echo "error: skill path does not exist: $SKILL_PATH" >&2
  exit 1
fi
SKILL_PATH="$(cd "$SKILL_PATH" && pwd)"

if [[ ! -f "${SKILL_PATH}/SKILL.md" ]]; then
  echo "error: SKILL.md not found in: $SKILL_PATH" >&2
  exit 1
fi

SKILL_NAME="$(basename "$SKILL_PATH")"
echo "    Skill: ${SKILL_NAME} (${SKILL_PATH})"
[[ -n "${PR_URL}" ]] && echo "    PR URL : ${PR_URL}"

echo "==> Resolving adapter: ${AGENT}"
ADAPTER_FILE="${ADAPTERS_DIR}/${AGENT}.sh"
if [[ ! -f "$ADAPTER_FILE" ]]; then
  echo "error: adapter not found: ${AGENT}" >&2
  echo "    available adapters:" >&2
  for f in "${ADAPTERS_DIR}"/*.sh; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    [[ "$name" == "_interface.sh" ]] && continue
    echo "      ${name%.sh}" >&2
  done
  exit 1
fi
echo "    Adapter: ${ADAPTER_FILE}"

echo "==> Checking dependencies..."
if ! command -v jq &>/dev/null; then
  echo "error: jq is required but not found" >&2
  exit 1
fi
echo "    jq: $(command -v jq)"

if [[ -n "$CUSTOM_SESSION_ROOT" ]]; then
  export FETCH_PR_SKILL_SESSION_ROOT="$CUSTOM_SESSION_ROOT"
  echo "==> Session root overridden: ${CUSTOM_SESSION_ROOT}"
fi

echo "==> Creating session..."
SESSION_ID="$("${SESSION_SCRIPT}" create "$SKILL_NAME" "$SKILL_PATH" "$AGENT" "$ADAPTER_INPUT")"
SESSION_DIR="${FETCH_PR_SKILL_SESSION_ROOT:-$HOME/.fetch-pr-skill/sessions}/${SESSION_ID}"
echo "    Session ID: ${SESSION_ID}"
echo "    Session dir: ${SESSION_DIR}"

echo "==> Copying skill snapshot into session..."
cp -r "${SKILL_PATH}/." "${SESSION_DIR}/skill/"
echo "    Snapshot: ${SESSION_DIR}/skill/"

echo "==> Setting flow verify status to processing..."
if command -v python3 &>/dev/null; then
    FLOW_SCRIPT="${SCRIPT_DIR}/eval/flow_status.py"
    if [[ -f "${FLOW_SCRIPT}" ]]; then
        python3 -c "
import sys, os
sys.path.insert(0, '${SCRIPT_DIR}')
from eval.flow_status import get_multica_issue_id, update_verify_status, build_verify_url
issue = get_multica_issue_id('${SKILL_PATH}')
if issue:
    url = build_verify_url(issue, '${PR_URL}')
    if update_verify_status(issue, 'processing', url):
        print(f'    Verify status -> processing (issue {issue})')
    else:
        print(f'    Warning: no flow record updated for issue {issue}')
else:
    print('    Warning: multica_issue_id not found, skipping flow status update')
" 2>&1 || echo "    Warning: flow status processing update failed (non-blocking)"
    else
        echo "    Warning: flow_status.py not found, skipping processing update"
    fi
else
    echo "    Warning: python3 not found, skipping processing update"
fi

echo "==> Running skill via adapter..."
START_EPOCH=$(date +%s)

export ADAPTER_INPUT
export ADAPTER_TIMEOUT="$TIMEOUT"

EXIT_CODE=0
timeout "${TIMEOUT}" bash -c "source '${ADAPTER_FILE}'; adapter_run '${SKILL_PATH}' '${SESSION_DIR}'" || EXIT_CODE=$?

END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))

if [[ $EXIT_CODE -eq 124 ]]; then
  STATUS="timeout"
elif [[ $EXIT_CODE -eq 0 ]]; then
  STATUS="success"
else
  STATUS="failed"
fi

if [[ "${STATUS}" != "success" ]]; then
    echo "==> Skill run failed (${STATUS}), setting flow verify status to failed..."
    if command -v python3 &>/dev/null; then
        python3 -c "
import sys, os
sys.path.insert(0, '${SCRIPT_DIR}')
from eval.flow_status import get_multica_issue_id, update_verify_status, build_verify_url
issue = get_multica_issue_id('${SKILL_PATH}')
if issue:
    url = build_verify_url(issue, '${PR_URL}')
    if update_verify_status(issue, 'failed', url):
        print(f'    Verify status -> failed (issue {issue})')
    else:
        print(f'    Warning: no flow record updated for issue {issue}')
" 2>&1 || echo "    Warning: flow status failed update failed (non-blocking)"
    fi
fi

echo "==> Completing session..."
"${SESSION_SCRIPT}" complete "$SESSION_ID" "$STATUS" "$EXIT_CODE"

echo ""
echo "==> Loading environment..."
for _src in "${HOME}/.bashrc" "${HOME}/.profile" /etc/profile; do
    [[ -f "$_src" ]] && source "$_src" 2>/dev/null || true
done

echo "==> Running evaluation..."
if command -v python3 &>/dev/null; then
    EVAL_SCRIPT="${SCRIPT_DIR}/eval_skill.py"
    if [[ -f "${EVAL_SCRIPT}" ]]; then
        EVAL_EXIT=0
        python3 "${EVAL_SCRIPT}" "${SESSION_ID}" \
            --agent "${AGENT}" \
            --skill-name "${SKILL_NAME}" \
            --skill-path "${SKILL_PATH}" \
            --pr-url "${PR_URL}" 2>&1 || EVAL_EXIT=$?
        if [[ ${EVAL_EXIT} -ne 0 ]]; then
            echo "    Warning: Evaluation failed (exit ${EVAL_EXIT})"
        fi
    else
        echo "    Warning: eval_skill.py not found, skipping evaluation"
    fi
else
    echo "    Warning: python3 not found, skipping evaluation"
fi

echo ""
echo "==> Session Summary"
echo "    Session ID : ${SESSION_ID}"
echo "    Status     : ${STATUS}"
echo "    Exit code  : ${EXIT_CODE}"
echo "    Duration   : ${DURATION}s"
echo "    Session dir: ${SESSION_DIR}"
echo "    Output log : ${SESSION_DIR}/output.log"
echo "    Show cmd   : bash ${SESSION_SCRIPT} show ${SESSION_ID}"
