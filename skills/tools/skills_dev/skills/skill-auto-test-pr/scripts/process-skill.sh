#!/usr/bin/env bash
set -uo pipefail

SKILL_NAME="${1:-}"
MULTICA_ISSUE_ID="${2:-}"

if [[ -z "$SKILL_NAME" ]]; then
  echo "[process-skill] ERROR: skill_name is required as first argument"
  exit 1
fi

echo "[process-skill] Processing skill: ${SKILL_NAME}"
if [[ -n "$MULTICA_ISSUE_ID" ]]; then
  echo "[process-skill] Multica issue: ${MULTICA_ISSUE_ID}"
fi

bash "${HOME}/.agents/skills/skill-auto-test-pr/scripts/run-pipeline.sh" "$SKILL_NAME" "$MULTICA_ISSUE_ID"
EXIT=$?

if [[ $EXIT -eq 0 ]]; then
  echo "[process-skill] Pipeline completed successfully for ${SKILL_NAME}"
else
  echo "[process-skill] Pipeline exited with code ${EXIT} for ${SKILL_NAME}"
fi

exit $EXIT
