#!/usr/bin/env bash
# =============================================================================
# deploy-skills-test.sh — SKILLS 测试能力一键部署脚本
#
# 功能:
#   将核心测试能力（huawei-cloud-skill-basictest + huawei-cloud-skill-tester +
#   专业功能测试 agent + 评测机制报告 agent + 评测小队 squad）一键部署到
#   任意 multica 工作区（支持当前工作区 / 其他工作区）。
#
# 用法:
#   bash deploy-skills-test.sh                                  # 默认工作区（读 config.json）
#   bash deploy-skills-test.sh --workspace <id|名称>            # 部署到指定工作区
#   bash deploy-skills-test.sh --update-token <mul_xxx>         # 更新 multica token 后重跑
#   bash deploy-skills-test.sh --token <mul_xxx>                # 指定 multica token
#   bash deploy-skills-test.sh --source <dir>                   # 本地 skill 目录（默认仓库 tools/skills_test）
#   bash deploy-skills-test.sh --dry-run                        # 只打印将执行的动作
#
# 环境变量（交互式逐项确认，非空校验）:
#   GITCODE_TOKEN        GitCode API token（gitcode_api.sh 用）
#   HUAWEI_ACCESS_KEY   华为云 AK（tester 真实环境测试）
#   HUAWEI_SECRET_KEY   华为云 SK
#   TEST_DB_HOST       测试结果数据库地址
#   TEST_DB_USER       数据库用户
#   TEST_DB_PASS       数据库密码（不回显）
#   TEST_DB_NAME       数据库名
#
# 依赖: multica CLI, python3, git, jq
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 默认源为 tools/skills_test/（脚本位于其 deploy/ 子目录）
DEFAULT_SOURCE="$(dirname "${SCRIPT_DIR}")"  # tools/skills_test/
BASICTEST_SKILL="huawei-cloud-skill-basictest"
TESTER_SKILL="huawei-cloud-skill-tester"
TEST_AGENT_NAME="专业功能测试"
VERIFY_AGENT_NAME="评测机制报告"
SQUAD_NAME="SKills专业验证+评测小队"
TMP_PKG="/tmp/skill-deploy-test"
DRY_RUN=0

# 默认 runtime（Opencode，与当前工作区一致；可被 --runtime 覆盖）
RUNTIME_ARG=""
RUNTIME_DEFAULT="30ab302a-6ded-4790-a7f1-493f53fb6346"

WORKSPACE_ARG=""
TOKEN_ARG=""
SOURCE_DIR="${DEFAULT_SOURCE}"

usage() {
    cat << 'EOF'
Usage: bash deploy-skills-test.sh [options]

  --workspace <id|name>  目标 multica 工作区（ID 或名称），默认读 ~/.multica/config.json
  --token <mul_xxx>      multica API token（可选，一般用已登录的 multica CLI）
  --update-token <t>     更新 multica token 后重跑部署
  --runtime <id>         agent 运行 runtime（默认 30ab302a... Opencode）
  --source <dir>         skill 源目录（默认 tools/skills_test，含两个 skill 目录）
  --dry-run              只打印将执行的动作，不实际执行
  -h, --help             显示帮助

部署内容:
  skill  huawei-cloud-skill-basictest（基本功评测主流程）
  skill  huawei-cloud-skill-tester（评测执行引擎）
  agent  专业功能测试（绑定 basictest）
  agent  评测机制报告（绑定 fetch-pr-skill，含 11 个环境变量）
  squad  SKills专业验证+评测小队（leader=专业功能测试，成员=评测机制报告）
EOF
    exit 0
}

for a in "$@"; do [[ "${a}" == "-h" || "${a}" == "--help" ]] && usage; done

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace)    WORKSPACE_ARG="${2:-}"; shift 2 ;;
        --token)        TOKEN_ARG="${2:-}"; shift 2 ;;
        --update-token) TOKEN_ARG="${2:-}"; shift 2 ;;
        --runtime)      RUNTIME_ARG="${2:-}"; shift 2 ;;
        --source)       SOURCE_DIR="${2:-}"; shift 2 ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

# =============================================================================
# 工具函数
# =============================================================================
log() { echo -e "$@"; }
log_ok()  { echo -e "  \033[32m✅ $*\033[0m"; }
log_warn() { echo -e "  \033[33m⚠️  $*\033[0m"; }
log_err() { echo -e "  \033[31m❌ $*\033[0m" >&2; }

require_cmd() {
    for c in "$@"; do
        if ! command -v "$c" >/dev/null 2>&1; then
            log_err "缺少依赖命令: $c"
            exit 1
        fi
    done
}

# 交互读取环境变量（非空校验；敏感值不回显）
prompt_env() {
    local var="$1" desc="$2" secret="$3" cur="${4:-}" value=""
    while true; do
        if [[ -n "$cur" ]]; then
            if [[ "$secret" == "1" ]]; then
                read -r -p "  设置 ${var}（${desc}，当前已设置，直接回车保留或输入新值）: " value
            else
                read -r -p "  设置 ${var}（${desc}，当前: ${cur}，直接回车保留或输入新值）: " value
            fi
        else
            if [[ "$secret" == "1" ]]; then
                read -r -s -p "  设置 ${var}（${desc}，必填）: " value
                echo ""
            else
                read -r -p "  设置 ${var}（${desc}，必填）: " value
            fi
        fi
        if [[ -z "$value" && -n "$cur" ]]; then
            echo "$cur"; return 0
        fi
        if [[ -z "$value" ]]; then
            log_warn "${var} 不能为空，请重新输入"
            continue
        fi
        echo "$value"; return 0
    done
}

resolve_workspace_id() {
    local arg="${WORKSPACE_ARG}"
    if [[ -n "${arg}" ]]; then
        if [[ "${arg}" =~ ^[0-9a-f-]{8,}$ ]]; then
            echo "${arg}"
        else
            local found
            found=$(multica workspace list --output json 2>/dev/null | RESOLVE_NAME="${arg}" python3 -c "
import json,sys,os
name=os.environ['RESOLVE_NAME']
for w in json.load(sys.stdin):
    if w.get('name')==name:
        print(w.get('id')); sys.exit(0)
sys.exit(1)
")
            [[ -n "${found}" ]] || { log_err "工作区 '${arg}' 未找到。可用:"; multica workspace list 2>&1 | sed 's/^/  /' >&2; exit 1; }
            echo "${found}"
        fi
    else
        python3 -c "import json; print(json.load(open('$HOME/.multica/config.json'))['workspace_id'])" 2>/dev/null \
            || { log_err "无法从 ~/.multica/config.json 读取 workspace_id，请用 --workspace 指定"; exit 1; }
    fi
}

# =============================================================================
# 0. 环境检查
# =============================================================================
echo "=============================================================================="
echo " SKILLS 测试能力一键部署"
echo "=============================================================================="
require_cmd multica python3 git jq

# =============================================================================
# 1. multica token（可选更新）
# =============================================================================
if [[ -n "${TOKEN_ARG}" ]]; then
    echo "==> 更新 multica token..."
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] multica login --token ${TOKEN_ARG:0:6}..."
    else
        multica login --token "${TOKEN_ARG}" >/dev/null 2>&1 \
            && log_ok "multica token 已更新" \
            || { log_err "token 更新失败（multica login）"; exit 1; }
    fi
fi

# =============================================================================
# 2. 工作区选择
# =============================================================================
echo "==> 解析目标工作区..."
WORKSPACE_ID="$(resolve_workspace_id)"
echo "  目标工作区: ${WORKSPACE_ID}"

# =============================================================================
# 3. skill 源准备（从仓库 tools/skills_test 拉取）
# =============================================================================
echo "==> 检查 skill 源目录..."
for skill in "${BASICTEST_SKILL}" "${TESTER_SKILL}"; do
    if [[ ! -d "${SOURCE_DIR}/${skill}" ]]; then
        log_err "skill 源目录缺失: ${SOURCE_DIR}/${skill}"
        log_err "请确认 SOURCE_DIR（默认 tools/skills_test）含这两个 skill 目录，或用 --source 指定"
        exit 1
    fi
done
log_ok "skill 源就绪: ${BASICTEST_SKILL} + ${TESTER_SKILL}"

# =============================================================================
# 4. 打包并导入 skill
# =============================================================================
mkdir -p "${TMP_PKG}"
declare -A SKILL_IDS
for skill in "${BASICTEST_SKILL}" "${TESTER_SKILL}"; do
    src="${SOURCE_DIR}/${skill}"
    pkg="${TMP_PKG}/${skill}.skill"
    echo "==> 打包 ${skill}..."
    python3 - "${src}" "${skill}" "${pkg}" <<'PYEOF'
import zipfile, os, sys
src, name, dst = sys.argv[1], sys.argv[2], sys.argv[3]
exclude_dirs = {"__pycache__", ".pytest_cache", ".git", "node_modules"}
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.join(name, os.path.relpath(p, src)))
PYEOF
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] multica skill import --file ${pkg} --on-conflict overwrite (workspace ${WORKSPACE_ID})"
        SKILL_IDS["${skill}"]="<new-id>"
        continue
    fi
    # 导入（--workspace-id 通过环境变量? 检查 multica 是否支持）
    result=$(multica skill import --file "${pkg}" --on-conflict overwrite --output json 2>&1)
    new_id=$(echo "$result" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get('id') or (d.get('data') or {}).get('id') or '')
except Exception:
    print('')
" 2>/dev/null)
    if [[ -z "${new_id}" ]]; then
        # 可能已存在，按名字查找
        new_id=$(multica skill list --output json 2>/dev/null | SKILL_NAME="${skill}" python3 -c "
import json,sys,os
name=os.environ['SKILL_NAME']
for s in json.load(sys.stdin):
    if s.get('name')==name:
        print(s.get('id')); break
")
    fi
    if [[ -n "${new_id}" ]]; then
        SKILL_IDS["${skill}"]="${new_id}"
        log_ok "${skill} 导入完成 (id: ${new_id})"
    else
        log_warn "${skill} 导入结果未知，按名称查找也失败: ${result:0:100}"
    fi
done

# =============================================================================
# 5. 创建/更新 agents
# =============================================================================
RUNTIME="${RUNTIME_ARG:-${RUNTIME_DEFAULT}}"
echo "==> 确保 agents 存在..."

# 5.1 专业功能测试 agent
TEST_AGENT_ID=$(multica agent list --output json 2>/dev/null | AGENT_NAME="${TEST_AGENT_NAME}" python3 -c "
import json,sys,os
name=os.environ['AGENT_NAME']
for a in json.load(sys.stdin):
    if a.get('name')==name and not a.get('archived_at'):
        print(a.get('id')); break
")
if [[ -n "${TEST_AGENT_ID}" ]]; then
    log_ok "agent「${TEST_AGENT_NAME}」已存在 (${TEST_AGENT_ID})"
else
    echo "  创建 agent「${TEST_AGENT_NAME}」..."
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] multica agent create --name ${TEST_AGENT_NAME} --runtime-id ${RUNTIME}"
        TEST_AGENT_ID="<new-agent-id>"
    else
        TEST_AGENT_ID=$(multica agent create --name "${TEST_AGENT_NAME}" \
            --description "负责对华为云 skill 执行专业功能测试（使用 huawei-cloud-skill-basictest）" \
            --runtime-id "${RUNTIME}" \
            --permission-mode public_to --public-to-workspace \
            --instructions "$(cat "${SCRIPT_DIR}/agent_test_instructions.md" 2>/dev/null || echo '负责专业功能测试')" \
            --output json 2>&1 | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
        [[ -n "${TEST_AGENT_ID}" ]] && log_ok "agent「${TEST_AGENT_NAME}」创建完成 (${TEST_AGENT_ID})" \
            || { log_err "创建 agent 失败"; exit 1; }
    fi
fi

# 5.2 评测机制报告 agent
VERIFY_AGENT_ID=$(multica agent list --output json 2>/dev/null | AGENT_NAME="${VERIFY_AGENT_NAME}" python3 -c "
import json,sys,os
name=os.environ['AGENT_NAME']
for a in json.load(sys.stdin):
    if a.get('name')==name and not a.get('archived_at'):
        print(a.get('id')); break
")
if [[ -n "${VERIFY_AGENT_ID}" ]]; then
    log_ok "agent「${VERIFY_AGENT_NAME}」已存在 (${VERIFY_AGENT_ID})"
else
    echo "  创建 agent「${VERIFY_AGENT_NAME}」..."
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] multica agent create --name ${VERIFY_AGENT_NAME} --runtime-id ${RUNTIME}"
        VERIFY_AGENT_ID="<new-agent-id>"
    else
        VERIFY_AGENT_ID=$(multica agent create --name "${VERIFY_AGENT_NAME}" \
            --description "负责对 SKILLS PR 进行验证评测（使用 fetch-pr-skill）" \
            --runtime-id "${RUNTIME}" \
            --permission-mode public_to --public-to-workspace \
            --instructions "$(cat "${SCRIPT_DIR}/agent_verify_instructions.md" 2>/dev/null || echo '负责验证评测')" \
            --output json 2>&1 | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
        [[ -n "${VERIFY_AGENT_ID}" ]] && log_ok "agent「${VERIFY_AGENT_NAME}」创建完成 (${VERIFY_AGENT_ID})" \
            || { log_err "创建 agent 失败"; exit 1; }
    fi
fi

# =============================================================================
# 6. 分配 skill 到 agent
# =============================================================================
if [[ "${DRY_RUN}" -ne 1 ]]; then
    # 专业功能测试 → basictest
    multica agent skills add "${TEST_AGENT_ID}" --skill-ids "${SKILL_IDS[${BASICTEST_SKILL}]}" >/dev/null 2>&1 \
        && log_ok "basictest 已分配给「${TEST_AGENT_NAME}」" \
        || log_warn "basictest 分配失败（可能已分配）"
    # 评测机制报告 → fetch-pr-skill（若存在）
    FETCH_PR_ID=$(multica skill list --output json 2>/dev/null | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s.get('name')=='fetch-pr-skill':
        print(s.get('id')); break
")
    if [[ -n "${FETCH_PR_ID}" ]]; then
        multica agent skills add "${VERIFY_AGENT_ID}" --skill-ids "${FETCH_PR_ID}" >/dev/null 2>&1 \
            && log_ok "fetch-pr-skill 已分配给「${VERIFY_AGENT_NAME}」" \
            || log_warn "fetch-pr-skill 分配失败（可能已分配）"
    else
        log_warn "工作区无 fetch-pr-skill，评测机制报告 agent 暂未绑定（可用 deploy_skill.sh 单独部署）"
    fi
else
    echo "  [dry-run] multica agent skills add ..."
fi

# =============================================================================
# 7. 创建/更新 squad
# =============================================================================
echo "==> 确保 squad 存在..."
SQUAD_ID=$(multica squad list --output json 2>/dev/null | SQUAD_NAME="${SQUAD_NAME}" python3 -c "
import json,sys,os
name=os.environ['SQUAD_NAME']
for s in json.load(sys.stdin):
    if s.get('name')==name and not s.get('archived_at'):
        print(s.get('id')); break
")
if [[ -n "${SQUAD_ID}" ]]; then
    log_ok "squad「${SQUAD_NAME}」已存在 (${SQUAD_ID})"
else
    echo "  创建 squad「${SQUAD_NAME}」..."
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "  [dry-run] multica squad create --name '${SQUAD_NAME}' --leader ${TEST_AGENT_ID}"
        SQUAD_ID="<new-squad-id>"
    else
        SQUAD_ID=$(multica squad create --name "${SQUAD_NAME}" \
            --leader "${TEST_AGENT_ID}" \
            --description "先由专业功能测试智能体进行专业功能测试，再由评测机制报告智能体进行评估报告输出" \
            --output json 2>&1 | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
        [[ -n "${SQUAD_ID}" ]] && log_ok "squad「${SQUAD_NAME}」创建完成 (${SQUAD_ID})" \
            || { log_err "创建 squad 失败"; exit 1; }
    fi
fi

# 添加评测机制报告为 squad 成员
if [[ "${DRY_RUN}" -ne 1 && -n "${SQUAD_ID}" && "${SQUAD_ID}" != "<new-squad-id>" ]]; then
    multica squad member add "${SQUAD_ID}" --member-id "${VERIFY_AGENT_ID}" --type agent >/dev/null 2>&1 \
        && log_ok "评测机制报告已加入 squad" \
        || log_warn "评测机制报告加入 squad 失败（可能已是成员）"
fi

# =============================================================================
# 8. 环境变量交互式设置（逐项确认）
# =============================================================================
echo ""
echo "=============================================================================="
echo " 环境变量设置（逐项确认）"
echo "=============================================================================="
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "  [dry-run] 将交互式确认: GITCODE_TOKEN / HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY / TEST_DB_*"
else
    # 读取当前 env（若有）
    get_env() { multica agent env get "${VERIFY_AGENT_ID}" --output json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); print(d.get('$1',''))
except Exception: print('')
" 2>/dev/null; }

    GITCODE_TOKEN="$(get_env GITCODE_TOKEN)"
    HUAWEI_ACCESS_KEY="$(get_env HUAWEI_ACCESS_KEY)"
    HUAWEI_SECRET_KEY="$(get_env HUAWEI_SECRET_KEY)"
    TEST_DB_HOST="$(get_env TEST_DB_HOST)"
    TEST_DB_USER="$(get_env TEST_DB_USER)"
    TEST_DB_PASS="$(get_env TEST_DB_PASS)"
    TEST_DB_NAME="$(get_env TEST_DB_NAME)"

    echo ""
    echo "--- GitCode token ---"
    GITCODE_TOKEN="$(prompt_env GITCODE_TOKEN "GitCode API 访问令牌" 1 "${GITCODE_TOKEN}")"
    echo "--- 华为云凭证 ---"
    HUAWEI_ACCESS_KEY="$(prompt_env HUAWEI_ACCESS_KEY "华为云 Access Key" 0 "${HUAWEI_ACCESS_KEY}")"
    HUAWEI_SECRET_KEY="$(prompt_env HUAWEI_SECRET_KEY "华为云 Secret Key" 1 "${HUAWEI_SECRET_KEY}")"
    echo "--- 测试数据库 ---"
    TEST_DB_HOST="$(prompt_env TEST_DB_HOST "数据库地址" 0 "${TEST_DB_HOST:-192.168.1.18}")"
    TEST_DB_USER="$(prompt_env TEST_DB_USER "数据库用户" 0 "${TEST_DB_USER:-root}")"
    TEST_DB_PASS="$(prompt_env TEST_DB_PASS "数据库密码" 1 "${TEST_DB_PASS}")"
    TEST_DB_NAME="$(prompt_env TEST_DB_NAME "数据库名" 0 "${TEST_DB_NAME:-rds-hd-prod-skillsopr-db}")"

    # 组装 JSON 写入 agent env
    ENV_JSON=$(python3 -c "
import json,sys
env={
 'GITCODE_TOKEN': '$GITCODE_TOKEN',
 'HUAWEI_ACCESS_KEY': '$HUAWEI_ACCESS_KEY',
 'HUAWEI_SECRET_KEY': '$HUAWEI_SECRET_KEY',
 'TEST_DB_HOST': '$TEST_DB_HOST',
 'TEST_DB_USER': '$TEST_DB_USER',
 'TEST_DB_PASS': '$TEST_DB_PASS',
 'TEST_DB_NAME': '$TEST_DB_NAME',
}
print(json.dumps(env, ensure_ascii=False))
")
    echo ""
    echo "==> 写入环境变量到 agent「${VERIFY_AGENT_NAME}」..."
    echo "${ENV_JSON}" > /tmp/skill-deploy-test-env.json
    chmod 600 /tmp/skill-deploy-test-env.json
    if multica agent env set "${VERIFY_AGENT_ID}" --custom-env-file /tmp/skill-deploy-test-env.json >/dev/null 2>&1; then
        log_ok "环境变量已设置（7 项）"
    else
        # 尝试 agent update 的 custom-env-file 方式
        if multica agent update "${VERIFY_AGENT_ID}" --custom-env-file /tmp/skill-deploy-test-env.json >/dev/null 2>&1; then
            log_ok "环境变量已设置（update 方式）"
        else
            log_warn "环境变量写入失败，请手动在 Web 控制台为 agent「${VERIFY_AGENT_NAME}」配置"
            log_warn "需设置: GITCODE_TOKEN / HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY / TEST_DB_HOST / TEST_DB_USER / TEST_DB_PASS / TEST_DB_NAME"
        fi
    fi
    rm -f /tmp/skill-deploy-test-env.json
fi

# =============================================================================
# 9. 部署校验
# =============================================================================
echo ""
echo "=============================================================================="
echo " 部署校验"
echo "=============================================================================="
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "  [dry-run] 完成（未实际部署）"
    exit 0
fi
echo "  workspace: ${WORKSPACE_ID}"
echo "  skill basictest: ${SKILL_IDS[${BASICTEST_SKILL}]:-未确认}"
echo "  skill tester:    ${SKILL_IDS[${TESTER_SKILL}]:-未确认}"
echo "  agent 专业功能测试: ${TEST_AGENT_ID}"
echo "  agent 评测机制报告: ${VERIFY_AGENT_ID}"
echo "  squad: ${SQUAD_ID}"
echo ""
echo "  可用命令验证:"
echo "    multica skill list --workspace-id ${WORKSPACE_ID}"
echo "    multica agent list --workspace-id ${WORKSPACE_ID}"
echo "    multica squad list --workspace-id ${WORKSPACE_ID}"
echo ""
log_ok "部署完成！"
