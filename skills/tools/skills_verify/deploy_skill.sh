#!/usr/bin/env bash
# =============================================================================
# deploy_skill.sh — 评测 skill 一键部署（fetch-pr-skill + huawei-cloud-skill-audit）
#
# 用法:
#   bash deploy_skill.sh                                  # 默认工作区 + 默认评测 agent
#   bash deploy_skill.sh --workspace <id|名称>            # 指定工作区
#   bash deploy_skill.sh --agent <id|名称> --token mul_xxx  # 指定 agent + 更新 token
#   bash deploy_skill.sh --dry-run                        # 只打印将执行的动作
#
# 说明:
#   - 部署评测全套: fetch-pr-skill(导入 multica+分配 agent) + huawei-cloud-skill-audit(复制到 ~/.agents/skills/)
#   - workspace/agent 支持 ID 或名称（名称自动解析）
#   - --token 用 multica login 更新认证
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE="${SCRIPT_DIR}"          # tools/skills_verify/ 含 fetch-pr-skill/ 和 huawei-cloud-skill-audit/
DEFAULT_AGENT="评测机制报告"
DEFAULT_AGENT_ID="47f1d99e-be13-403c-af1e-888c743702ce"
AUDIT_SKILL="huawei-cloud-skill-audit"
PR_SKILL="fetch-pr-skill"
TMP_PKG="/tmp/skill-deploy"

WORKSPACE_ID=""
WORKSPACE_ARG=""
AGENT_ARG=""
TOKEN_ARG=""
SOURCE_DIR="${DEFAULT_SOURCE}"
AUDIT_DIR="${HOME}/.agents/skills/${AUDIT_SKILL}"
DRY_RUN=0
NO_CONFIRM=0
ENV_FILE=""
SKIP_ENV=0
OBS_CRED_FILE="${HOME}/.fetch-pr-skill/obs-cred.conf"
DB_CONF_FILE="${HOME}/.fetch-pr-skill/eval-db.conf"

usage() {
    echo "Usage: $0 [--workspace <id|name>] [--agent <id|name>] [--token <mul_xxx>] [--source <dir>] [--audit-dir <dir>] [--no-confirm] [--env-file <path>] [--skip-env] [--dry-run]"
    echo ""
    echo "  部署评测 skill 全套：fetch-pr-skill（导入 multica + 分配 agent）"
    echo "                      + huawei-cloud-skill-audit（复制到 agent 运行机）"
    echo ""
    echo "  --workspace <id|name>  目标 multica 工作区（ID 或名称）"
    echo "  --agent <id|name>      目标 agent（ID 或名称），默认 ${DEFAULT_AGENT}"
    echo "  --token <mul_xxx>      更新 multica token（执行 multica login --token）"
    echo "  --source <dir>         评测 skill 根目录（含 fetch-pr-skill/ 和 huawei-cloud-skill-audit/）"
    echo "  --audit-dir <dir>      audit skill 复制目标（默认 ~/.agents/skills/huawei-cloud-skill-audit）"
    echo "  --no-confirm           跳过交互确认，用当前环境变量直接部署"
    echo "  --env-file <path>      从 JSON 文件读取全部环境变量（custom_env + 配置）"
    echo "  --skip-env             跳过环境变量确认与写入，只部署 skill"
    echo "  --dry-run              只打印将执行的动作，不实际执行"
    exit 0
}

for a in "$@"; do
    [[ "${a}" == "-h" || "${a}" == "--help" ]] && usage
done

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace) WORKSPACE_ARG="$2"; shift 2 ;;
        --agent)     AGENT_ARG="$2";     shift 2 ;;
        --token)     TOKEN_ARG="$2";     shift 2 ;;
        --source)    SOURCE_DIR="$2";    shift 2 ;;
        --audit-dir) AUDIT_DIR="$2";     shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        --no-confirm) NO_CONFIRM=1; shift ;;
        --env-file)   ENV_FILE="$2"; shift 2 ;;
        --skip-env)   SKIP_ENV=1; shift ;;
        -h|--help)   usage ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- 工具函数 ---
log() { echo "$@"; }

# 环境变量定义: key|来源(custom_env/conf_obs/conf_db)|敏感(0/1)|默认值
# 来源: custom_env → agent custom_env；conf_obs → obs-cred.conf；conf_db → eval-db.conf
ENV_DEFS=(
    "GITCODE_TOKEN|custom_env|1|"
    "HUAWEI_CLOUD_AK|custom_env|1|"
    "HUAWEI_CLOUD_SK|custom_env|1|"
    "HUAWEI_CLOUD_PROJECT_ID|custom_env|0|"
    "HUAWEI_CLOUD_REGION|custom_env|0|cn-north-4"
    "HUAWEI_CLOUD_SMN_TOPIC_URN|custom_env|0|"
    "EVAL_NOTIFICATION_EMAILS|custom_env|0|"
    "EVAL_DB_HOST|conf_db|0|"
    "EVAL_DB_USER|conf_db|0|"
    "EVAL_DB_PASSWORD|conf_db|1|"
    "EVAL_DB_NAME|conf_db|0|"
    "OBS_AK|conf_obs|1|"
    "OBS_SK|conf_obs|1|"
    "OBS_BUCKET|conf_obs|0|"
    "OBS_CDN_DOMAIN|conf_obs|0|"
    "OBS_REGION|conf_obs|0|cn-north-4"
    "DEFAULT_REPO|conf_obs|0|"
)

read_custom_env() {
    # 输出 agent custom_env JSON 的扁平 key=value（一行一个）；读取失败返回非零
    local out
    out=$(multica agent env get "${AGENT_ID}" --output json 2>/dev/null) || return 1
    [[ -z "${out}" ]] && return 1
    python3 -c "
import json,sys
d=json.load(sys.stdin)
env=d.get('custom_env',{})
for k,v in env.items():
    print(f'{k}={v}')
" <<< "${out}" || return 1
}

read_conf_file() {
    # 读取 INI 配置文件，输出 key=value（一行一个）
    local file="$1"
    [[ -f "${file}" ]] || return 0
    while IFS='=' read -r key val; do
        [[ -z "${key}" || "${key}" == \#* ]] && continue
        echo "${key}=${val}"
    done < "${file}"
}

mask_value() {
    # 敏感值脱敏：前 8 位 + ***
    local val="$1" sensitive="$2"
    if [[ "${sensitive}" == "1" && -n "${val}" ]]; then
        echo "${val:0:8}***"
    else
        echo "${val}"
    fi
}

get_env_value() {
    # 从 custom_env + 配置文件聚合取值（当前值）
    local key="$1"
    # 优先 custom_env，其次配置文件
    for entry in "${CUSTOM_ENV_LINES[@]}" "${CONF_OBS_LINES[@]}" "${CONF_DB_LINES[@]}"; do
        [[ "${entry}" == "${key}="* ]] && echo "${entry#${key}=}" && return
    done
    echo ""
}

confirm_env() {
    # 交互确认所有环境变量，输出最终 JSON 到 env_json_file
    local env_json_file="$1"
    local key source sensitive default val answer newval
    local -A final_env=()

    echo ""
    echo "==> 环境变量确认（回车=接受，n=修改，skip=跳过该项）"
    echo ""

    for def in "${ENV_DEFS[@]}"; do
        IFS='|' read -r key source sensitive default <<< "${def}"
        val="$(get_env_value "${key}")"
        [[ -z "${val}" ]] && val="${default}"

        if [[ "${NO_CONFIRM}" -eq 1 ]]; then
            # --no-confirm：无交互，直接用当前值（或默认值），空值跳过
            [[ -n "${val}" ]] && final_env["${key}"]="${val}"
            continue
        fi

        if [[ -z "${val}" ]]; then
            # 无当前值：询问是否设置
            printf "  [%s] (无当前值) 设置? (y/n, skip=跳过): " "${key}"
            read -r answer
            case "${answer}" in
                n|N) final_env["${key}"]="" ;;
                skip|SKIP) continue ;;
                *) printf "    新值: "; read -r newval; final_env["${key}"]="${newval}" ;;
            esac
            continue
        fi

        # 有当前值：显示并确认
        printf "  [%s] 当前值 = %s  确认? (y/n, view=明文, skip=跳过): " "${key}" "$(mask_value "${val}" "${sensitive}")"
        read -r answer
        case "${answer}" in
            ""|y|Y) final_env["${key}"]="${val}" ;;
            n|N) printf "    新值: "; read -r newval; final_env["${key}"]="${newval}" ;;
            view|VIEW) echo "    明文: ${val}"; final_env["${key}"]="${val}" ;;
            skip|SKIP) continue ;;
            *) final_env["${key}"]="${val}" ;;
        esac
    done

    # 输出 JSON（空值排除）
    python3 -c "
import json,sys
d={}
for line in sys.stdin:
    k,_,v=line.partition('=')
    if v: d[k]=v.rstrip('\n')
print(json.dumps(d, ensure_ascii=False))
" < <(for k in "${!final_env[@]}"; do echo "${k}=${final_env[$k]}"; done) > "${env_json_file}"

    echo ""
    echo "==> 环境变量确认完成"
}

write_custom_env() {
    # 写入 agent custom_env：只写 custom_env 来源的键；已有但未确认的键用 **** 保留
    local env_json_file="$1"
    local env_out="${TMP_PKG}/custom_env.json"
    local def key source sensitive default entry
    local custom_keys=() existing_keys=()

    # 收集 ENV_DEFS 中 source=custom_env 的键
    for def in "${ENV_DEFS[@]}"; do
        IFS='|' read -r key source sensitive default <<< "${def}"
        [[ "${source}" == "custom_env" ]] && custom_keys+=("${key}")
    done

    # 收集 agent custom_env 中已有的键（CUSTOM_ENV_LINES 为 key=value 列表）
    for entry in "${CUSTOM_ENV_LINES[@]}"; do
        [[ -n "${entry}" ]] && existing_keys+=("${entry%%=*}")
    done

    python3 - "${env_json_file}" "${env_out}" "${custom_keys[*]}" "${existing_keys[*]}" <<'PYEOF'
import json,sys
env=json.load(open(sys.argv[1]))
custom_keys=set(sys.argv[3].split()) if sys.argv[3] else set()
existing_keys=set(sys.argv[4].split()) if len(sys.argv)>4 and sys.argv[4] else set()
confirmed=set(env)
out={}
# 只写 custom_env 来源且已确认的键
for k in custom_keys & confirmed:
    out[k]=env[k]
# 已有但未确认的键保留（**** → multica 保留原值）
for k in existing_keys - confirmed:
    out[k]='****'
json.dump(out, open(sys.argv[2],'w'), ensure_ascii=False)
PYEOF
    multica agent env set "${AGENT_ID}" --custom-env-file "${env_out}" >/dev/null 2>&1 \
        && echo "    custom_env 已更新" || { echo "error: custom_env 写入失败" >&2; return 1; }
}

write_config_files() {
    # 更新 eval-db.conf 与 obs-cred.conf（保留其他键；python 处理特殊字符，避免 sed 分隔符冲突）
    local env_json_file="$1"
    local changed

    changed=$(python3 - "${env_json_file}" "${OBS_CRED_FILE}" "${DB_CONF_FILE}" <<'PYEOF'
import json, sys, os
env = json.load(open(sys.argv[1]))
obs_file, db_file = sys.argv[2], sys.argv[3]

def update_ini(path, keys):
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    key_index = {}
    for i, line in enumerate(lines):
        if line.startswith('#'):
            continue
        if '=' in line:
            k = line.split('=', 1)[0].strip()
            if k and k not in key_index:
                key_index[k] = i
    changed = False
    for k in keys:
        if k not in env:
            continue
        if k in key_index:
            if lines[key_index[k]] != f'{k}={env[k]}':
                lines[key_index[k]] = f'{k}={env[k]}'
                changed = True
        else:
            lines.append(f'{k}={env[k]}')
            key_index[k] = len(lines) - 1
            changed = True
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    return changed

obs = update_ini(obs_file, ['OBS_AK','OBS_SK','OBS_BUCKET','OBS_CDN_DOMAIN','OBS_REGION','DEFAULT_REPO'])
db = update_ini(db_file, ['EVAL_DB_HOST','EVAL_DB_USER','EVAL_DB_PASSWORD','EVAL_DB_NAME'])
print('obs' if obs else '')
print('db' if db else '')
PYEOF
)
    [[ "${changed}" == *obs* ]] && echo "    obs-cred.conf 已更新" || true
    [[ "${changed}" == *db* ]] && echo "    eval-db.conf 已更新" || true
}

resolve_workspace_id() {
    # 返回目标 workspace ID；WORKSPACE_ARG 支持 ID/名称；空则读 config.json
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
            if [[ -z "${found}" ]]; then
                echo "error: workspace '${arg}' not found. Available:" >&2
                multica workspace list 2>&1 | sed 's/^/  /' >&2
                exit 1
            fi
            echo "${found}"
        fi
    else
        python3 -c "import json; print(json.load(open('$HOME/.multica/config.json'))['workspace_id'])" 2>/dev/null || { echo "error: cannot read workspace_id from ~/.multica/config.json" >&2; exit 1; }
    fi
}

resolve_agent_id() {
    # 返回目标 agent ID；AGENT_ARG 支持 ID/名称；空则用默认评测机制报告
    local arg="${AGENT_ARG:-${DEFAULT_AGENT}}"
    if [[ "${arg}" =~ ^[0-9a-f-]{8,}$ ]]; then
        echo "${arg}"
    else
        local found
        found=$(multica agent list --output json 2>/dev/null | RESOLVE_AGENT="${arg}" python3 -c "
import json,sys,os
name=os.environ['RESOLVE_AGENT']
for a in json.load(sys.stdin):
    if a.get('name')==name:
        print(a.get('id')); sys.exit(0)
sys.exit(1)
")
        if [[ -z "${found}" ]]; then
            echo "error: agent '${arg}' not found. Available:" >&2
            multica agent list 2>&1 | sed 's/^/  /' >&2
            exit 1
        fi
        echo "${found}"
    fi
}

# --- 1. token 更新 ---
if [[ -n "${TOKEN_ARG}" && "${DRY_RUN}" -eq 0 ]]; then
    echo "==> 更新 multica token..."
    multica login --token "${TOKEN_ARG}" >/dev/null 2>&1 \
        && echo "    token 已更新" || { echo "error: token login failed" >&2; exit 1; }
elif [[ -n "${TOKEN_ARG}" ]]; then
    echo "==> [dry-run] 将执行: multica login --token <redacted>"
fi

# --- 2. workspace 解析 ---
WORKSPACE_ID="$(resolve_workspace_id)"
echo "==> 目标工作区: ${WORKSPACE_ID}"
export MULTICA_WORKSPACE_ID="${WORKSPACE_ID}"

# --- 3. agent 解析 ---
AGENT_ID="$(resolve_agent_id)"
echo "==> 目标 agent: ${AGENT_ID}"

# --- 4. 检查源目录（先校验，再写入环境/部署，避免校验失败后已改 agent 环境） ---
PR_SRC="${SOURCE_DIR}/${PR_SKILL}"
AUDIT_SRC="${SOURCE_DIR}/${AUDIT_SKILL}"
[[ -d "${PR_SRC}" && -f "${PR_SRC}/SKILL.md" ]] || { echo "error: fetch-pr-skill not found at ${PR_SRC}" >&2; exit 1; }
[[ -d "${AUDIT_SRC}" && -f "${AUDIT_SRC}/SKILL.md" ]] || { echo "error: huawei-cloud-skill-audit not found at ${AUDIT_SRC}" >&2; exit 1; }

# --- 5. 环境变量确认与写入 ---
if [[ "${SKIP_ENV}" -eq 0 ]]; then
    mkdir -p "${TMP_PKG}"
    CUSTOM_ENV_LINES=($(read_custom_env))
    CONF_OBS_LINES=($(read_conf_file "${OBS_CRED_FILE}"))
    CONF_DB_LINES=($(read_conf_file "${DB_CONF_FILE}"))
    ENV_JSON="${TMP_PKG}/env_confirm.json"

    if [[ -n "${ENV_FILE}" ]]; then
        echo "==> 从 --env-file 读取环境变量..."
        cp "${ENV_FILE}" "${ENV_JSON}"
    elif [[ "${NO_CONFIRM}" -eq 1 ]]; then
        echo "==> --no-confirm：用当前环境变量直接部署..."
        confirm_env "${ENV_JSON}"  # 用当前值生成 JSON（无交互，全接受）
    else
        confirm_env "${ENV_JSON}"
    fi

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "    [dry-run] 将写入 agent custom_env (${AGENT_ID}) + 配置文件"
    else
        write_custom_env "${ENV_JSON}"
        write_config_files "${ENV_JSON}"
    fi
else
    echo "==> --skip-env：跳过环境变量确认与写入"
fi

# --- 6. 部署 audit skill（复制到 agent 运行机） ---
echo "==> 部署 ${AUDIT_SKILL} -> ${AUDIT_DIR}"
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "    [dry-run] cp -r ${AUDIT_SRC} ${AUDIT_DIR}"
else
    [[ -n "${AUDIT_DIR}" && "${AUDIT_DIR}" != "/" ]] || { echo "error: invalid --audit-dir: ${AUDIT_DIR}" >&2; exit 1; }
    mkdir -p "$(dirname "${AUDIT_DIR}")"
    rm -rf "${AUDIT_DIR}"
    cp -r "${AUDIT_SRC}" "${AUDIT_DIR}"
    echo "    复制完成 ($(find "${AUDIT_DIR}" -type f | wc -l) 文件)"
fi

# --- 7. 密钥安全检查（fetch-pr-skill） ---
echo "==> 扫描敏感信息..."
LEAKS=$(rg -n "sk-[a-zA-Z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN.*PRIVATE|BEGIN RSA" "${PR_SRC}" \
        --glob '!*.zip' --glob '!*.skill' 2>/dev/null | grep -v "__pycache__" || true)
if [[ -n "${LEAKS}" ]]; then
    echo "WARNING: 发现疑似敏感信息（继续部署但请检查）:"
    echo "${LEAKS}" | head -10
fi

# --- 8. 打包 fetch-pr-skill ---
mkdir -p "${TMP_PKG}"
PKG="${TMP_PKG}/${PR_SKILL}.skill"
python3 - "${PR_SRC}" "${PR_SKILL}" "${PKG}" <<'PYEOF'
import zipfile, os, sys
src, name, dst = sys.argv[1], sys.argv[2], sys.argv[3]
exclude_dirs = {"__pycache__", ".pytest_cache", ".git", "node_modules"}
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.join(name, os.path.relpath(p, src)))
print(f"    打包完成: {dst} ({os.path.getsize(dst)} bytes)")
PYEOF

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo ""
    echo "==> [dry-run] 将执行:"
    echo "    1. multica skill import --file ${PKG} --on-conflict overwrite"
    echo "    2. multica agent skills add ${AGENT_ID} --skill-ids <new_id>"
    echo "    3. workspace: ${WORKSPACE_ID}, agent: ${AGENT_ID}"
    echo "==> dry-run 完成（未实际部署）"
    exit 0
fi

# --- 9. 记录当前 skill id ---
OLD_ID=$(multica skill list --output json 2>/dev/null | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s.get('name') == '${PR_SKILL}':
        print(s.get('id')); break
" || true)

# --- 10. 导入 ---
echo "==> 导入 ${PR_SKILL} 到 multica..."
IMPORT_JSON=$(multica skill import --file "${PKG}" --on-conflict overwrite --output json 2>/dev/null || true)
NEW_ID=$(echo "${IMPORT_JSON}" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get('id',''))
except Exception:
    print('')
" 2>/dev/null)

if [[ -z "${NEW_ID}" ]]; then
    NEW_ID=$(multica skill list --output json 2>/dev/null | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s.get('name') == '${PR_SKILL}':
        print(s.get('id')); break
" || true)
fi

[[ -z "${NEW_ID}" ]] && { echo "error: 导入失败，无法获取 skill id" >&2; exit 1; }
echo "    skill id: ${NEW_ID}"

# --- 11. 分配 agent ---
echo "==> 分配 skill 给 agent: ${AGENT_ID}"
if multica agent skills add "${AGENT_ID}" --skill-ids "${NEW_ID}" --output json >/dev/null 2>&1; then
    echo "    分配完成"
else
    echo "error: 分配 agent 失败" >&2
    exit 1
fi

# --- 12. 汇总 ---
echo ""
echo "==> 部署完成"
echo "    fetch-pr-skill     : ${NEW_ID}"
echo "    huawei-cloud-skill-audit: ${AUDIT_DIR}"
echo "    workspace          : ${WORKSPACE_ID}"
echo "    agent              : ${AGENT_ID}"
if [[ -n "${OLD_ID}" && "${OLD_ID}" != "${NEW_ID}" ]]; then
    echo "    ⚠️  skill id 已变化 (${OLD_ID} -> ${NEW_ID})，agent 分配已更新"
fi
