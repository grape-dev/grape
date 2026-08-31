#!/usr/bin/env bash
# set_processing.sh — agent 启动时立即将 node_verify_status 置为 processing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Setting flow status to processing (agent startup)..."
if command -v python3 &>/dev/null; then
    python3 -c "
import sys, os
sys.path.insert(0, '${SCRIPT_DIR}')
from eval.flow_status import get_multica_issue_id, update_verify_status, build_verify_url

issue = get_multica_issue_id(os.getcwd())
if not issue:
    print('    Warning: multica_issue_id not found, skipping')
    sys.exit(0)

url = build_verify_url(issue, '')
if update_verify_status(issue, 'processing', url):
    print(f'    node_verify_status -> processing (issue {issue})')
else:
    print(f'    Warning: no flow record updated for issue {issue}')
" 2>&1 || echo "    Warning: set_processing failed (non-blocking)"
else
    echo "    Warning: python3 not found, skipping"
fi
