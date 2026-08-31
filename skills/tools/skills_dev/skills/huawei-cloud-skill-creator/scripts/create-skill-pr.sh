#!/usr/bin/env bash
# ============================================================================
# create-skill-pr.sh — 开发 agent 创建 GitCode PR 的统一入口
#
# 背景(2026-08-18 PR #20 事故): 开发 agent 裸调 GitCode API 创建 PR 时
# 把 base 分支自拟为 'main', 而目标仓库 hd-skills-pre/skills-pre 默认分支
# 是 master, 导致 PR diff 卷入 master 已有提交、审查误报、被迫重建 PR #21。
# 本脚本将"解析仓库 + 确定 base + 创建 PR + 校验 base + 设审查人"收敛为
# 唯一代码点, base 一律取 ${GITCODE_BASE_BRANCH:-master}, 严禁默认 'main'。
#
# 用法:
#   create-skill-pr.sh <multica_issue_id> <skill_name>
#     分支固定为 add-skill/<skill_name>, 需先推送到目标仓库
#
# 环境变量(均可用 ~/.agents/skills/.env 提供):
#   GITCODE_API / GITCODE_TOKEN / GITCODE_OWNER / GITCODE_REPO
#   GITCODE_REPO_SSH / GITCODE_PR_ASSIGNEE / GITCODE_BASE_BRANCH(默认 master)
#   DB_HOST / DB_USER / DB_PASS / DB_NAME (关联 issue 号查询, 可缺省)
#
# 退出码: 0=成功(创建或复用); 1=参数/环境错误; 2=创建失败或 base 校验不过
# ============================================================================
set -uo pipefail

MULTICA="${MULTICA:-$(command -v multica 2>/dev/null || echo /usr/local/bin/multica)}"
# .env 路径可用 CREATE_SKILL_PR_ENV 覆盖(测试注入用); 生产默认 ~/.agents/skills/.env
ENV_FILE="${CREATE_SKILL_PR_ENV:-${HOME}/.agents/skills/.env}"

# 环境变量兜底: .env 已存在则加载(不覆盖已导出的值)
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

GITCODE_API="${GITCODE_API:-https://api.gitcode.com/api/v5}"
GITCODE_OWNER="${GITCODE_OWNER:-hd-skills-pre}"
GITCODE_REPO="${GITCODE_REPO:-skills-pre}"
GITCODE_REPO_SSH="${GITCODE_REPO_SSH:-git@gitcode.com:hd-skills-pre/skills-pre}"
GITCODE_PR_ASSIGNEE="${GITCODE_PR_ASSIGNEE:-HugLovesWine}"
# ★ 核心: base 分支唯一来源。单仓库场景默认 master(目标仓库默认分支), 禁止 'main'
GITCODE_BASE_BRANCH="${GITCODE_BASE_BRANCH:-master}"

ISSUE_ID="${1:-}"
SKILL_NAME="${2:-}"

[[ -n "$ISSUE_ID" ]]  || { echo "[create-skill-pr] ERROR: multica_issue_id 必填"; exit 1; }
[[ -n "$SKILL_NAME" ]] || { echo "[create-skill-pr] ERROR: skill_name 必填"; exit 1; }
[[ -n "${GITCODE_TOKEN:-}" ]] || { echo "[create-skill-pr] ERROR: GITCODE_TOKEN 未设置(检查 .env/daemon env)"; exit 1; }

BRANCH="add-skill/${SKILL_NAME}"

log() { echo "[create-skill-pr] $*"; }

# ---------------------------------------------------------------------------
# 1) 解析目标仓库: DB gitcode_issue_url → issue description **来源仓库：** → 默认
#    仅 huaweicloud-skills* 来源仓库覆盖 GITCODE_* (与 run-pipeline.sh 一致)
# ---------------------------------------------------------------------------
resolve_repo() {
  local repo_full="" row
  row=$(python3 << PYEOF 2>/dev/null || true
import pymysql, re
try:
    conn = pymysql.connect(host='${DB_HOST:-}', user='${DB_USER:-}', password='${DB_PASS:-}', database='${DB_NAME:-}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT gitcode_issue_url FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1", ('${ISSUE_ID}',))
    r = cur.fetchone()
    conn.close()
    if r and r[0]:
        m = re.search(r'gitcode\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)(/|$)', r[0])
        if m: print(m.group(1))
except Exception:
    pass
PYEOF
  )
  if [[ -z "$row" ]]; then
    # Tier2: issue description 的 **来源仓库：**
    row=$("$MULTICA" issue get "$ISSUE_ID" --output json 2>/dev/null | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
    m = re.search(r'\*\*来源仓库：\*\*\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)', d.get('description') or '')
    if m: print(m.group(1))
except Exception:
    pass
" 2>/dev/null || true)
    [[ -n "$row" ]] && log "来源仓库(issue description): ${row}"
  else
    log "来源仓库(DB gitcode_issue_url): ${row}"
  fi
  [[ -n "$row" ]] || { log "无来源仓库字段, 使用默认仓库 ${GITCODE_OWNER}/${GITCODE_REPO}"; return 0; }
  local issue_owner="${row%%/*}" issue_repo="${row#*/}"
  if [[ "$issue_repo" == huaweicloud-skills* ]]; then
    log "来源仓库 ${row} 为技能归档仓, 覆盖 GITCODE_*"
    export GITCODE_OWNER="$issue_owner" GITCODE_REPO="$issue_repo"
    export GITCODE_REPO_SSH="git@gitcode.com:${issue_owner}/${issue_repo}"
  else
    log "来源仓库 ${row} 非技能归档仓, 使用默认仓库 ${GITCODE_OWNER}/${GITCODE_REPO}"
  fi
}

# ---------------------------------------------------------------------------
# 2) 查 GitCode issue 号(关联 Fixes #N); DB 不可用/无记录则跳过
# ---------------------------------------------------------------------------
gitcode_issue_num() {
  local num=""
  GITCODE_ISSUE_NUM=""
  [[ -n "${DB_HOST:-}" && -n "${DB_PASS:-}" ]] || { log "DB 未配置, 跳过 issue 关联"; return; }
  num=$(python3 << PYEOF 2>/dev/null || true
import pymysql
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER:-}', password='${DB_PASS}', database='${DB_NAME:-}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT gitcode_issue_number FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s", ('${ISSUE_ID}',))
    r = cur.fetchone()
    conn.close()
    if r and r[0]: print(r[0])
except Exception:
    pass
PYEOF
  )
  GITCODE_ISSUE_NUM="${num:-}"
  [[ -n "$GITCODE_ISSUE_NUM" ]] && log "关联 GitCode issue #${GITCODE_ISSUE_NUM}"
}

# ---------------------------------------------------------------------------
# 3) 幂等: 该分支已有 open PR 则复用(归属校验: body 含 Fixes #本任务号)
# ---------------------------------------------------------------------------
find_existing_pr() {
  local prs
  prs=$(curl -s -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls?state=open&per_page=100" 2>/dev/null)
  PR_NUMBER=$(echo "$prs" | python3 -c "
import json,sys
try:
    for pr in json.load(sys.stdin):
        head = pr.get('head') or {}
        if head.get('ref') == '${BRANCH}':
            print(pr.get('number','')); break
except Exception:
    pass
")
  [[ -n "$PR_NUMBER" ]] || return 1
  PR_URL="https://gitcode.com/${GITCODE_OWNER}/${GITCODE_REPO}/merge_requests/${PR_NUMBER}"
  # 归属校验: 有 Fixes #且不是本任务号 → 不复用(避免冒认他人 PR)
  local detail
  detail=$(curl -s -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls/${PR_NUMBER}" 2>/dev/null)
  if [[ -n "$GITCODE_ISSUE_NUM" ]]; then
    local fixes
    fixes=$(echo "$detail" | python3 -c "
import json,sys,re
try:
    d=json.load(sys.stdin); m=re.search(r'[Ff]ixes\s*#(\d+)', d.get('body') or '')
    print(m.group(1) if m else '')
except Exception: pass
" 2>/dev/null || true)
    if [[ -n "$fixes" && "$fixes" != "$GITCODE_ISSUE_NUM" ]]; then
      log "ERROR: 分支 ${BRANCH} 已有 PR #${PR_NUMBER} 但归属其他 issue(#${fixes}), 不复用不重复创建"
      PR_NUMBER="" PR_URL=""
      return 2
    fi
  fi
  log "复用已有 open PR #${PR_NUMBER} (${PR_URL})"
  return 0
}

# ---------------------------------------------------------------------------
# 4) 创建 PR
# ---------------------------------------------------------------------------
create_pr() {
  local issue_link="" body
  [[ -n "$GITCODE_ISSUE_NUM" ]] && issue_link="Fixes #${GITCODE_ISSUE_NUM}"
  body=$(python3 -c "
import json
b = '''## Add Skill: ${SKILL_NAME}

- Created by \`huawei-cloud-skill-creator\`
- Branch: ${BRANCH}
- Base: ${GITCODE_BASE_BRANCH}'''
if '${issue_link}':
    b += '\n\n${issue_link}'
print(json.dumps({'head': '${BRANCH}', 'base': '${GITCODE_BASE_BRANCH}', 'title': 'add skill: ${SKILL_NAME}', 'body': b}))
" 2>/dev/null)
  log "POST /repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls  base=${GITCODE_BASE_BRANCH} head=${BRANCH}"
  local resp code
  resp=$(curl -s -w "\n%{http_code}" -X POST \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls" \
    -H "Content-Type: application/json" \
    -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    -d "$body")
  code=$(echo "$resp" | tail -n 1)
  local body_only; body_only=$(echo "$resp" | head -n -1)
  if [[ ! "$code" =~ ^2 ]]; then
    log "ERROR: 创建 PR 失败 HTTP ${code}"
    echo "$body_only" | head -c 500
    echo ""
    return 1
  fi
  PR_NUMBER=$(echo "$body_only" | python3 -c "import json,sys; print(json.load(sys.stdin).get('number',''))" 2>/dev/null || true)
  PR_URL="https://gitcode.com/${GITCODE_OWNER}/${GITCODE_REPO}/merge_requests/${PR_NUMBER}"
  [[ -n "$PR_NUMBER" ]] || { log "ERROR: 创建响应无 PR number"; return 1; }
  log "PR 已创建 #${PR_NUMBER} (${PR_URL})"
  return 0
}

# ---------------------------------------------------------------------------
# 5) 校验: base.ref 必须等于 GITCODE_BASE_BRANCH 且 state=open
# ---------------------------------------------------------------------------
verify_base() {
  local detail
  detail=$(curl -s -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls/${PR_NUMBER}" 2>/dev/null)
  local actual state
  actual=$(echo "$detail" | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('base') or {}).get('ref',''))" 2>/dev/null || true)
  state=$(echo "$detail" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('state',''))" 2>/dev/null || true)
  if [[ "$actual" != "$GITCODE_BASE_BRANCH" ]]; then
    log "ERROR: PR #${PR_NUMBER} base 校验不过: 期望 ${GITCODE_BASE_BRANCH}, 实际 ${actual:-未知} — 请关闭后重建"
    return 2
  fi
  if [[ "$state" != "open" ]]; then
    log "WARNING: PR #${PR_NUMBER} state=${state}(非 open), 请检查"
  fi
  log "校验通过: PR #${PR_NUMBER} base=${actual} state=${state}"
  return 0
}

# ---------------------------------------------------------------------------
# 6) 设置审查人
# ---------------------------------------------------------------------------
add_reviewer() {
  [[ -n "$GITCODE_PR_ASSIGNEE" ]] || return 0
  local r
  r=$(curl -s -m 8 -X POST \
    "${GITCODE_API}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/pulls/${PR_NUMBER}/assignees" \
    -H "Content-Type: application/json" \
    -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" \
    -d "{\"assignees\":\"${GITCODE_PR_ASSIGNEE}\"}" 2>/dev/null)
  log "审查人 ${GITCODE_PR_ASSIGNEE} 设置响应: ${r:-empty}(失败不阻塞)"
}

# ---------------------------------------------------------------------------
# VOD 报告: 解析 SKILL.md 判断执行模式, 若为 SDK/API 则自动调用 report-vod.sh
# 幂等(skip if open issue exists), 永不阻塞 PR 创建
# ---------------------------------------------------------------------------
check_vod_report() {
  local service_name skill_dir resolve_out
  # 从 skill_name 提取 service 前缀
  service_name=$(echo "$SKILL_NAME" | sed 's/^huawei-cloud-//' | sed 's/-.*//' | tr '[:lower:]' '[:upper:]')
  [[ -n "$service_name" ]] || { log "[vod] 无法从 skill_name 提取 service"; return 0; }

  # 解析分类目录, 定位生成的 SKILL.md (在 git 工作目录 skills/{cat}/{sub}/{skill-name}/SKILL.md)
  local rc=0
  resolve_out=$(bash "$(dirname "$0")/../../skill-auto-test-pr/scripts/resolve-skill-category.sh "$SKILL_NAME" 2>/dev/null) || rc=$?
  [[ $rc -eq 0 && -n "$resolve_out" ]] || { log "[vod] resolve-category 失败($rc), 跳过 SKILL.md 检查"; return 0; }
  skill_dir="./skills/${resolve_out}/${SKILL_NAME}"

  # SDK/API 模式判断: SKILL.md 不含 hcloud 命令(CLI) 且不含 CLI-installation-guide
  if [ -f "$skill_dir/SKILL.md" ]; then
    if grep -qi '^##.*Core Commands' "$skill_dir/SKILL.md" 2>/dev/null; then
      if grep -qE '```\w*\n?hcloud[ \t]' "$skill_dir/SKILL.md" 2>/dev/null || [ -f "$skill_dir/references/cli-installation-guide.md" ]; then
        log "[vod] ${SKILL_NAME} 基于 hcloud CLI, 无需 VOD 上报"
        return 0
      fi
    fi
    # SDK 模式确认或不确定 → 继续上报
  else
    log "[vod] 未找到 $skill_dir/SKILL.md, 按服务名直接上报"
  fi

  log "[vod] ${SKILL_NAME} 无 CLI 支持, 自动上报 VOD..."
  bash "$(dirname "$0")/report-vod.sh" "$ISSUE_ID" "$service_name" "SDK/API" "$SKILL_NAME"
  log "[vod] report-vod.sh 完成(幂等, 见上一条日志)"
}

# ============================== main =======================================
check_vod_report
resolve_repo
gitcode_issue_num
log "目标: ${GITCODE_OWNER}/${GITCODE_REPO}  branch=${BRANCH}  base=${GITCODE_BASE_BRANCH}"

if find_existing_pr; then
  :  # 已复用 PR_NUMBER/PR_URL
else
  rc=$?
  if [[ $rc -eq 2 ]]; then
    exit 2  # 归属他人, 不创建
  fi
  create_pr || exit 2
fi

verify_base
rc=$?
if [[ $rc -ne 0 ]]; then
  log "请人工处理: ${PR_URL}"
  exit $rc
fi

add_reviewer
echo ""
log "完成: ${PR_URL}  (base=${GITCODE_BASE_BRANCH}, 分支=${BRANCH})"
exit 0
