#!/usr/bin/env bash
set -uo pipefail

MULTICA="${MULTICA:-$(command -v multica 2>/dev/null || echo /usr/local/bin/multica)}"
SKILLS_BASE_DIR="${HOME}/.agents/skills"
SKILL_TESTER_DIR="${SKILLS_BASE_DIR}/huawei-cloud-skill-tester"
# GitCode repository — override via env vars to switch to another repo centrally.
#   GITCODE_REPO_SSH  SSH clone URL            e.g. git@gitcode.com:owner/repo
#   GITCODE_API       GitCode API base URL     only change when switching instance
#   GITCODE_TOKEN     GitCode API token
#   GITCODE_OWNER     repo owner/namespace
#   GITCODE_REPO      repo name
#   GITCODE_CHECKOUT  directory name of the local skills repo checkout
#                     used by fetch_skill_from_workspace (must match the repo name)
GITCODE_REPO_SSH="${GITCODE_REPO_SSH:-}"  # 必填: 请在 ~/.agents/skills/.env 配置
GITCODE_API="${GITCODE_API:-https://api.gitcode.com/api/v5}"
GITCODE_TOKEN="${GITCODE_TOKEN:-}"  # 必填: 请在 ~/.agents/skills/.env 配置
GITCODE_OWNER="${GITCODE_OWNER:-}"  # 必填: 请在 ~/.agents/skills/.env 配置
GITCODE_REPO="${GITCODE_REPO:-}"  # 必填: 请在 ~/.agents/skills/.env 配置
GITCODE_CHECKOUT="${GITCODE_CHECKOUT:-skills-pre}"
# PR reviewer (审查人/合并人) login — assigned to each created PR via POST .../pulls/{n}/assignees.
# Must be a user with operation privileges on the target repo (Owner/Maintainer 级别，否则设置失败).
# 可配置项：通过环境变量 GITCODE_PR_ASSIGNEE 覆盖，默认 g30074593.
GITCODE_PR_ASSIGNEE="${GITCODE_PR_ASSIGNEE:-g30074593}"
DB_HOST="${DB_HOST:-}"
DB_USER="${DB_USER:-}"
DB_PASS="${DB_PASS:-}"  # 请通过环境变量 DB_PASS 提供数据库密码
DB_NAME="${DB_NAME:-}"

# Load Huawei Cloud AK/SK from ~/.bashrc if not already in the environment
# (agent runtimes started by the multica daemon do not source ~/.bashrc)
if [[ -z "${HUAWEI_ACCESS_KEY:-}" && -z "${HUAWEICLOUD_SDK_AK:-}" ]]; then
  BASH_RC="${HOME}/.bashrc"
  if [[ -f "$BASH_RC" ]]; then
    BASH_RC_AK=$(grep -E '^export[[:space:]]+HUAWEICLOUD_SDK_AK=' "$BASH_RC" | tail -1 | sed -E 's/^export[[:space:]]+HUAWEICLOUD_SDK_AK="?([^"]*)"?$/\1/')
    BASH_RC_SK=$(grep -E '^export[[:space:]]+HUAWEICLOUD_SDK_SK=' "$BASH_RC" | tail -1 | sed -E 's/^export[[:space:]]+HUAWEICLOUD_SDK_SK="?([^"]*)"?$/\1/')
    if [[ -n "$BASH_RC_AK" && -n "$BASH_RC_SK" ]]; then
      export HUAWEI_ACCESS_KEY="$BASH_RC_AK"
      export HUAWEI_SECRET_KEY="$BASH_RC_SK"
      echo "[skill-auto-test-pr] AK/SK loaded from ~/.bashrc"
    fi
  fi
fi

SKILL_NAME="${1:-}"
MULTICA_ISSUE_ID="${2:-}"

if [[ -z "$SKILL_NAME" ]]; then
  echo "[skill-auto-test-pr] ERROR: SKILL_NAME is required as first argument"
  exit 1
fi

SKILL_PATH="${SKILLS_BASE_DIR}/${SKILL_NAME}"

# 分类映射唯一权威来源: 独立脚本 resolve-skill-category.sh（dev 创建与测试 PR 共用，
# 保证落盘路径一致）。新增子分类只改脚本，不再改本函数。
resolve_skill_category() {
  local name="$1"
  bash "$(dirname "${BASH_SOURCE[0]}")/resolve-skill-category.sh" "$name"
}

# Fetch skill content from the git checkout into a given directory.
# Skill content lives in the checkout's skills/ tree (any branch).
# Independent of any local ~/.agents/skills copy (which the tester may uninstall).
fetch_skill_from_workspace() {
  local out_dir="$1"
  local repo skill_dir

  mkdir -p "$out_dir"

  # Locate ALL checkouts of the skills repo and find the one containing this skill.
  # Different tasks create separate checkouts; the skill lives in exactly one of them.
  while IFS= read -r repo; do
    [[ -z "$repo" ]] && continue
    skill_dir=$(find "$repo/skills" -maxdepth 4 -type d -name "${SKILL_NAME}" 2>/dev/null | head -1)
    if [[ -n "$skill_dir" && -f "$skill_dir/SKILL.md" ]]; then
      break
    fi
  done < <(find "${HOME}/multica_workspaces" -maxdepth 5 -type d -name "${GITCODE_CHECKOUT}" 2>/dev/null)

  if [[ -z "$repo" || -z "$skill_dir" ]]; then
    echo "[skill-auto-test-pr] ERROR: skill ${SKILL_NAME} not found in any ${GITCODE_CHECKOUT} checkout"
    return 1
  fi
  echo "[skill-auto-test-pr] Using git checkout: ${repo}"
  echo "[skill-auto-test-pr] Skill found at: ${skill_dir}"

  cp -r "$skill_dir/"* "$out_dir/"
  return 0
}

# Resolve the multica issue for this skill (needed for PR-issue linking and DB update).
# Prefer the issue id passed in from the webhook payload (agent task tokens may not
# be able to enumerate other workspaces), fall back to title-based search.
resolve_issue_id() {
  if [[ -n "$MULTICA_ISSUE_ID" ]]; then
    ISSUE_ID="$MULTICA_ISSUE_ID"
    echo "[skill-auto-test-pr] Using multica issue from payload: ${ISSUE_ID}"
    return
  fi
  local ws_id
  WS_IDS=$("$MULTICA" workspace list --output json 2>/dev/null | python3 -c "
import json, sys
try:
    for ws in json.load(sys.stdin): print(ws['id'])
except: pass
" 2>/dev/null) || true

  while IFS= read -r ws_id; do
    [[ -z "$ws_id" ]] && continue
    [[ -n "$ISSUE_ID" ]] && break
    ISSUE_ID=$("$MULTICA" --workspace-id "$ws_id" issue list --output json 2>/dev/null | python3 -c "
import json, sys
try:
    for issue in json.load(sys.stdin).get('issues', []):
        if '${SKILL_NAME}' in issue.get('title', ''):
            print(issue['id'])
            break
except: pass
" 2>/dev/null) || true
  done <<< "$WS_IDS"
}

# Resolve the target GitCode repository for the PR, in priority order:
#   1. DB t_skills_dev_requirement.gitcode_issue_url   (most authoritative — records which repo the
#      GitCode issue lives in; e.g. https://gitcode.com/<owner>/<repo>/issues/<n>)
#   2. Issue description "**来源仓库：** <owner>/<repo>"  (GitCode webhook deliveries embed this)
#   3. Default / env GITCODE_OWNER / GITCODE_REPO
# Only repos named huaweicloud-skills* are accepted as PR targets.
resolve_repo_from_issue() {
  [[ -n "$ISSUE_ID" ]] || return 0
  local repo_full=""

  # === Tier 1: DB gitcode_issue_url ===
  repo_full=$(python3 << PYEOF
import pymysql, re, sys
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT gitcode_issue_url FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1", ('${ISSUE_ID}',))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        m = re.search(r'gitcode\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)(/|$)', row[0])
        if m:
            print(m.group(1))
except Exception:
    pass
PYEOF
  ) || true

  if [[ -n "$repo_full" ]]; then
    echo "[skill-auto-test-pr] Repo from DB gitcode_issue_url: ${repo_full}"
  else
    # === Tier 2: issue description "来源仓库" ===
    local ws_id ws_ids
    ws_ids=$("$MULTICA" workspace list --output json 2>/dev/null | python3 -c "
import json, sys
try:
    for ws in json.load(sys.stdin): print(ws['id'])
except: pass
" 2>/dev/null) || true
    while IFS= read -r ws_id; do
      [[ -z "$ws_id" ]] && continue
      repo_full=$("$MULTICA" --workspace-id "$ws_id" issue get "$ISSUE_ID" --output json 2>/dev/null | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
    desc = d.get('description') or ''
    m = re.search(r'\*\*来源仓库：\*\*\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)', desc)
    if m:
        print(m.group(1))
except Exception:
    pass
" 2>/dev/null) || true
      [[ -n "$repo_full" ]] && break
    done <<< "$ws_ids"
    [[ -n "$repo_full" ]] && echo "[skill-auto-test-pr] Repo from issue description: ${repo_full}"
  fi

  [[ -n "$repo_full" ]] || {
    echo "[skill-auto-test-pr] No repo source found in DB or issue description; keeping defaults (${GITCODE_OWNER}/${GITCODE_REPO})"
    return 0
  }
  local issue_owner issue_repo
  issue_owner="${repo_full%%/*}"
  issue_repo="${repo_full#*/}"

  # Only override when the source repo is actually a huaweicloud-skills repo (a skill archive repo).
  if [[ "$issue_repo" == huaweicloud-skills* ]]; then
    echo "[skill-auto-test-pr] Issue source repo: ${repo_full} (overriding GITCODE_* defaults)"
    export GITCODE_OWNER="$issue_owner"
    export GITCODE_REPO="$issue_repo"
    export GITCODE_CHECKOUT="$issue_repo"
    export GITCODE_REPO_SSH="git@gitcode.com:${issue_owner}/${issue_repo}"
  else
    echo "[skill-auto-test-pr] Issue source repo ${repo_full} is not a skills archive repo; keeping defaults (${GITCODE_OWNER}/${GITCODE_REPO})"
  fi
}

# Report a skill-usage/telemetry record by delegating to the huawei-cloud-skill-reporter
# CLI (single source of truth for IP / URL / payload logic — no duplicated impl here).
report_telemetry() {
  local status="$1"
  local action="$2"
  local result_line="${PR_URL:-no-pr}"
  if [[ -n "$PR_NUMBER" ]]; then
    result_line="PR #${PR_NUMBER} ${PR_URL}"
  fi
  local reporter_cli="${SKILLS_BASE_DIR}/huawei-cloud-skill-reporter/report.mjs"
  if [[ -f "$reporter_cli" ]]; then
    node "$reporter_cli" "${SKILL_NAME}" "${status}" "${action}" "${result_line}" >/dev/null 2>&1 || true
    echo "[skill-auto-test-pr] Telemetry delegated to huawei-cloud-skill-reporter: skill=${SKILL_NAME} status=${status}"
  else
    echo "[skill-auto-test-pr] WARNING: reporter CLI not found at ${reporter_cli}, skipping telemetry"
  fi
}

# Update t_skills_dev_flow.node_dev_status and node_dev_url in the database.
# Also syncs t_skills_dev_requirement.status: processing→developing, done→developed, failed→failed.
update_dev_status() {
  local status="$1"
  if [[ -z "$ISSUE_ID" ]]; then
    echo "[skill-auto-test-pr] WARNING: No issue id, skipping t_skills_dev_flow update (status=${status})"
    return
  fi
  # map dev status to requirement.status
  local req_status
  case "$status" in
    processing) req_status="developing" ;;
    done)       req_status="developed" ;;
    failed)     req_status="failed" ;;
    *)          req_status="$status" ;;
  esac
  local dev_url="${MULTICA_SERVER_URL:-https://multica.clouddeveloper.club}/${MULTICA_WORKSPACE_SLUG:-skill-test}/issues/${ISSUE_ID}"
  echo "[skill-auto-test-pr] Updating t_skills_dev_flow node_dev_status=${status}, node_dev_url=${dev_url} for issue ${ISSUE_ID}..."
  python3 << PYEOF
import pymysql
from datetime import datetime
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute(
        "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_url = %s, node_dev_time = %s WHERE multica_issue_id = %s",
        ('${status}', '${dev_url}', datetime.now(), '${ISSUE_ID}')
    )
    affected = cur.rowcount
    conn.commit()
    if affected > 0:
        print(f"[skill-auto-test-pr] t_skills_dev_flow updated: node_dev_status=${status}, node_dev_url=${dev_url}, node_dev_time=now()")
    else:
        print(f"[skill-auto-test-pr] WARNING: No t_skills_dev_flow record found with multica_issue_id=${ISSUE_ID}")
    # 同步更新 t_skills_dev_requirement.status
    try:
        cur.execute(
            "UPDATE \`t_skills_dev_requirement\` SET status = %s WHERE multica_issue_id = %s",
            ('${req_status}', '${ISSUE_ID}')
        )
        req_affected = cur.rowcount
        conn.commit()
        if req_affected > 0:
            print(f"[skill-auto-test-pr] t_skills_dev_requirement.status set to ${req_status}")
        else:
            print(f"[skill-auto-test-pr] WARNING: No t_skills_dev_requirement record found to set status=${req_status}")
    except Exception as req_e:
        print(f"[skill-auto-test-pr] WARNING: t_skills_dev_requirement status update failed: {req_e}")
    conn.close()
except Exception as e:
    print(f"[skill-auto-test-pr] WARNING: t_skills_dev_flow update failed: {e}")
PYEOF
}

# Record the created/reused PR number into t_skills_dev_requirement.gitcode_pr_id.
# Best-effort: a missing/wrong issue id is reported but does not fail the pipeline.
update_requirement_pr_id() {
  local pr_id="$1"
  if [[ -z "$ISSUE_ID" || -z "$pr_id" ]]; then
    echo "[skill-auto-test-pr] WARNING: No issue id or pr id, skipping t_skills_dev_requirement.gitcode_pr_id update"
    return
  fi
  echo "[skill-auto-test-pr] Updating t_skills_dev_requirement.gitcode_pr_id=${pr_id} for issue ${ISSUE_ID}..."
  python3 << PYEOF
import pymysql
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute(
        "UPDATE \`t_skills_dev_requirement\` SET gitcode_pr_id = %s WHERE multica_issue_id = %s",
        ('${pr_id}', '${ISSUE_ID}')
    )
    affected = cur.rowcount
    conn.commit()
    if affected > 0:
        print(f"[skill-auto-test-pr] t_skills_dev_requirement.gitcode_pr_id set to ${pr_id}")
    else:
        print(f"[skill-auto-test-pr] WARNING: No t_skills_dev_requirement record found with multica_issue_id=${ISSUE_ID}")
    conn.close()
except Exception as e:
    print(f"[skill-auto-test-pr] WARNING: t_skills_dev_requirement update failed: {e}")
PYEOF
}

echo "============================================"
echo "[skill-auto-test-pr] Pipeline Start"
echo "  Skill: ${SKILL_NAME}"
echo "  Path:  ${SKILL_PATH}"
echo "============================================"

# Resolve issue id first, then mark dev node as processing (pipeline started)
ISSUE_ID=""
resolve_issue_id

# Dynamically target the repo the issue came from (parsed from issue description)
resolve_repo_from_issue

if [[ -n "$ISSUE_ID" ]]; then
  echo "[skill-auto-test-pr] Found multica issue ${ISSUE_ID} for skill ${SKILL_NAME}"
fi

update_dev_status "processing"

# === Phase 1: Run Tester ===
echo ""
echo "--- Phase 1: Running huawei-cloud-skill-tester ---"

TESTER_SCRIPT="${SKILL_TESTER_DIR}/scripts/run-test-pipeline.sh"
if [[ ! -f "$TESTER_SCRIPT" ]]; then
  echo "[skill-auto-test-pr] WARNING: Tester script not found, skipping test phase"
  TESTER_EXIT=0
else
  bash "$TESTER_SCRIPT" --skills "$SKILL_NAME"
  TESTER_EXIT=$?

  if [[ $TESTER_EXIT -ne 0 ]]; then
    echo "[skill-auto-test-pr] WARNING: Tester exited with code ${TESTER_EXIT}, continuing to PR creation..."
  else
    echo "[skill-auto-test-pr] Tester completed successfully."
  fi
fi

# === Phase 2: Create PR ===
echo ""
echo "--- Phase 2: Creating PR to GitCode ---"

# Look up an existing PR for this branch (idempotency: reuse instead of duplicating).
# Returns PR number + URL via global vars if an open/closed PR already exists.
find_existing_pr() {
  local branch="$1"
  local prs
  prs=$(curl -s -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls?state=all&per_page=100" 2>/dev/null)
  PR_NUMBER=$(echo "$prs" | python3 -c "
import json,sys
try:
    for pr in json.load(sys.stdin):
        head = pr.get('head')
        ref = head.get('ref','') if isinstance(head, dict) else ''
        if ref == '${branch}':
            print(pr.get('number',''))
            break
except Exception:
    pass
")
  if [ -n "$PR_NUMBER" ]; then
    PR_URL="https://gitcode.com/${GITCODE_OWNER}/${GITCODE_REPO}/merge_requests/${PR_NUMBER}"
    echo "[skill-auto-test-pr] Existing PR #${PR_NUMBER} found for branch ${branch}"
    return 0
  fi
  return 1
}

# Check whether the branch already exists on the remote (previous run / creator pushed it).
branch_exists_remote() {
  git ls-remote origin "refs/heads/${1}" 2>/dev/null | grep -q "refs/heads/${1}"
}

# Assign the configured PR reviewer (审查人) to a PR via POST .../pulls/{n}/assignees.
# Also set the PR merger (合并人, 内网 API 数字 id). Idempotent + best-effort.
# 审查人/合并人是 PR 治理设置，与测试结果无关 → PR 创建时即无条件设置。
add_pr_reviewer() {
  local pr_num="$1"
  [[ -n "$pr_num" ]] || return 0
  [[ -n "$GITCODE_PR_ASSIGNEE" ]] || return 0
  echo "[skill-auto-test-pr] Assigning ${GITCODE_PR_ASSIGNEE} as reviewer to PR #${pr_num}..."
  local resp
  resp=$(curl -s -m 8 -X POST \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls/${pr_num}/assignees" \
    -H "Content-Type: application/json" \
    -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    -d "{\"assignees\":\"${GITCODE_PR_ASSIGNEE}\"}" 2>/dev/null)
  echo "[skill-auto-test-pr] assignee response: ${resp:-empty}"

  # 合并人（内网 API 数字 id）——提前设置，独立 try/except 不阻断
  if [[ -n "${GITCODE_PR_ASSIGNEE_ID:-}" && -n "${GITCODE_ACCESS_TOKEN:-}" ]]; then
    echo "[skill-auto-test-pr] Assigning merger id ${GITCODE_PR_ASSIGNEE_ID} to PR #${pr_num}..."
    local pr_url="https://gitcode.com/${GITCODE_OWNER}/${GITCODE_REPO}/merge_requests/${pr_num}"
    python3 "$(dirname "$0")/set_pr_merger.py" "$pr_url" 2>&1 | sed 's/^/[skill-auto-test-pr] /'
  else
    echo "[skill-auto-test-pr] Skipping merger (need GITCODE_ACCESS_TOKEN + GITCODE_PR_ASSIGNEE_ID)"
  fi
}

TMP_BASE="/tmp/skill-pr-${SKILL_NAME}-$(date +%s)"
BRANCH="add-skill/${SKILL_NAME}"

echo "[skill-auto-test-pr] Cloning repo to ${TMP_BASE}..."
git clone --depth=1 "$GITCODE_REPO_SSH" "$TMP_BASE" 2>&1
CLONE_EXIT=$?

if [[ $CLONE_EXIT -ne 0 ]]; then
  echo "[skill-auto-test-pr] ERROR: git clone failed"
  rm -rf "$TMP_BASE" 2>/dev/null
  exit 1
fi

PR_URL=""
cd "$TMP_BASE" || exit 1

SKILL_CATEGORY=$(resolve_skill_category "$SKILL_NAME")
echo "[skill-auto-test-pr] Resolved category: ${SKILL_CATEGORY} for skill: ${SKILL_NAME}"

SKILLS_DIR="${TMP_BASE}/skills"
TARGET_DIR="${SKILLS_DIR}/${SKILL_CATEGORY}/${SKILL_NAME}"
mkdir -p "$(dirname "$TARGET_DIR")"

# Always fetch skill content fresh from the workspace for the PR.
# The local ~/.agents/skills copy may have been uninstalled by the tester.
PR_SKILL_SRC="/tmp/skill-src-${SKILL_NAME}-$$"
if [[ -d "$SKILL_PATH" ]]; then
  echo "[skill-auto-test-pr] Using local skill copy at ${SKILL_PATH}"
  cp -r "$SKILL_PATH" "$TARGET_DIR"
elif fetch_skill_from_workspace "$PR_SKILL_SRC"; then
  echo "[skill-auto-test-pr] Fetched skill content from multica workspace"
  cp -r "$PR_SKILL_SRC" "$TARGET_DIR"
  rm -rf "$PR_SKILL_SRC"
else
  echo "[skill-auto-test-pr] ERROR: Could not obtain skill content for ${SKILL_NAME} (local missing and workspace fetch failed)"
  cd /
  rm -rf "$TMP_BASE" 2>/dev/null
  exit 1
fi

# Look up gitcode_issue_number from database for PR-issue linking
GITCODE_ISSUE_NUM=""
if [[ -n "$ISSUE_ID" ]]; then
  GITCODE_ISSUE_NUM=$(python3 << PYEOF
import pymysql
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT gitcode_issue_number FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s", ('${ISSUE_ID}',))
    row = cur.fetchone()
    if row and row[0]:
        print(row[0])
    conn.close()
except: pass
PYEOF
  )
fi

ISSUE_LINK=""
if [[ -n "$GITCODE_ISSUE_NUM" ]]; then
  ISSUE_LINK="Fixes #${GITCODE_ISSUE_NUM}"
  echo "[skill-auto-test-pr] Linking PR to GitCode issue #${GITCODE_ISSUE_NUM}"
fi

PR_BODY=$(python3 -c "
import json
body = '''## Automated PR for Huawei Cloud Skill

This PR adds the newly created and tested skill.

### Workflow
- Created by \`huawei-cloud-skill-creator\`
- Tested by \`huawei-cloud-skill-tester\`
- PR auto-generated by \`skill-auto-test-pr\` pipeline'''
if '${ISSUE_LINK}':
    body += '\n\n${ISSUE_LINK}'
print(json.dumps({
    'head': '${BRANCH}',
    'base': '${GITCODE_BASE_BRANCH:-main}',
    'title': 'feat: add ${SKILL_NAME} skill',
    'body': body
}))
" 2>/dev/null)

# === Idempotency: reuse an existing PR for this branch if present ===
if find_existing_pr "$BRANCH"; then
  echo "[skill-auto-test-pr] Reusing existing PR #${PR_NUMBER} — skipping branch push + PR creation"
  HTTP_CODE="200"
  add_pr_reviewer "$PR_NUMBER"
  cd /
  rm -rf "$TMP_BASE" 2>/dev/null
else
  # No existing PR. Check whether the branch already exists on remote.
  if branch_exists_remote "$BRANCH"; then
    echo "[skill-auto-test-pr] Branch ${BRANCH} already exists on remote (no PR yet). Fetching and updating it..."
    git fetch origin "$BRANCH" 2>&1
    git checkout "$BRANCH" 2>&1
    # Overwrite branch content with the freshly fetched skill content
    cp -rf "$TARGET_DIR"/. "$(pwd)/skills/${SKILL_CATEGORY}/${SKILL_NAME}/" 2>/dev/null || true
  else
    git checkout -b "$BRANCH" 2>&1
  fi

  git add -A 2>&1
  git commit -m "feat: add ${SKILL_NAME} skill" 2>&1

  echo "[skill-auto-test-pr] Pushing branch ${BRANCH}..."
  PUSH_OK=0
  git push origin "$BRANCH" 2>&1 && PUSH_OK=1
  if [[ $PUSH_OK -ne 1 ]]; then
    echo "[skill-auto-test-pr] Push failed (possibly non-fast-forward). Retrying with force..."
    git push --force origin "$BRANCH" 2>&1 && PUSH_OK=1
  fi

  if [[ $PUSH_OK -ne 1 ]]; then
    echo "[skill-auto-test-pr] ERROR: git push failed"
    cd /
    rm -rf "$TMP_BASE" 2>/dev/null
    exit 1
  fi

  echo "[skill-auto-test-pr] Creating PR via GitCode API..."

  HTTP_RESP=$(curl -s -w "\n%{http_code}" -X POST \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls" \
    -H "Content-Type: application/json" \
    -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    -d "$PR_BODY")

  HTTP_BODY=$(echo "$HTTP_RESP" | head -n -1)
  HTTP_CODE=$(echo "$HTTP_RESP" | tail -n 1)

  PR_NUMBER=""
  if [[ "$HTTP_CODE" =~ ^2 ]]; then
    PR_URL=$(echo "$HTTP_BODY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('html_url', d.get('url', 'PR #'+str(d.get('number','')))))" 2>/dev/null || echo "unknown")
    PR_NUMBER=$(echo "$HTTP_BODY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('number',''))" 2>/dev/null || echo "")
    echo "[skill-auto-test-pr] Pull request created: ${PR_URL} (PR #${PR_NUMBER})"
    add_pr_reviewer "$PR_NUMBER"
  else
    echo "[skill-auto-test-pr] ERROR: GitCode API returned HTTP ${HTTP_CODE}"
    echo "$HTTP_BODY"
  fi

  cd /
  rm -rf "$TMP_BASE" 2>/dev/null
fi

# === Phase 3: Update t_skills_dev_flow after PR creation ===
echo ""
echo "--- Phase 3: Updating t_skills_dev_flow dev node status ---"

if [[ -n "$PR_NUMBER" && "$HTTP_CODE" =~ ^2 ]]; then
  DEV_STATUS="done"
else
  DEV_STATUS="failed"
fi
update_dev_status "$DEV_STATUS"

# Record the PR number into t_skills_dev_requirement (PR 编号入库)
if [[ -n "$PR_NUMBER" && "$HTTP_CODE" =~ ^2 ]]; then
  update_requirement_pr_id "$PR_NUMBER"
fi

# Fire-and-forget telemetry report of this pipeline run
if [[ "$DEV_STATUS" == "done" ]]; then
  report_telemetry "success" "skill pipeline: tester + PR"
else
  report_telemetry "failure" "skill pipeline: tester exit=${TESTER_EXIT:-n/a} pr_status=${DEV_STATUS}"
fi

echo ""
echo "============================================"
echo "[skill-auto-test-pr] Pipeline Complete!"
echo "  Skill: ${SKILL_NAME}"
echo "  Tester exit: ${TESTER_EXIT}"
echo "  PR URL: ${PR_URL:-N/A}"
echo "  PR Number: ${PR_NUMBER:-N/A}"
echo "  Issue ID: ${ISSUE_ID:-N/A}"
echo "  Issue link: ${ISSUE_LINK:-N/A}"
echo "  Dev status: ${DEV_STATUS:-N/A}"
echo "============================================"
