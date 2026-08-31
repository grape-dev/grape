#!/usr/bin/env bash
# ============================================================================
# skills_dev 一键部署脚本（支持多 profile / token 变更）
# 在新机器上部署 Multica Skill 开发工具链（skills + scripts + 运行环境），
# 并配置 Multica profile（支持多个工作区 / token 变更场景）。
#
# 用法:
#   bash deploy-skills-dev.sh [--repo <skillsopr-git-url>]
#   bash deploy-skills-dev.sh [--profile <name>]
#
# 可选环境变量:
#   MULTICA_SERVER_URL    Multica 服务地址 (默认 https://multica.clouddeveloper.club)
#   MULTICA_PROFILE       profile 名（默认 dev）
#   MULTICA_WORKSPACE_ID  目标工作区 ID
#   MULTICA_TOKEN         Multica API token（mul_ 开头）
#   GITCODE_TOKEN         GitCode API token（私有仓库克隆用）
#
# 行为:
#   - Hermes 优先: 工具链 skills 优先部署到 ~/.hermes/skills/<category>/（默认 huawei-cloud 分类，
#     可被 HERMES_SKILLS_CATEGORY 覆盖），同时兼容部署到 ~/.agents/skills/（multica agent 环境）；
#     设 SKIP_AGENTS_SKILLS=1 则仅部署 Hermes
#   - 多 profile: 每个工作区一个 profile，config 存 ~/.multica/profiles/<name>/config.json
#   - token 变更: 提供新 token 即更新该 profile 的 config，并自动验证有效性
#   - 幂等: 重复运行不会破坏已有配置，只更新指定 profile
#   - dev agent 指令: 按 tools/skills_dev/deploy/agent_dev_instructions.md 模板
#     更新 dev agent（Skills开发、测试、PR智能体）的 instructions（可 DEV_AGENT_NAME 覆盖）
#   - 环境配置: 生成 ~/.agents/skills/.env 模板（GITCODE/DB/AK-SK 等需手动填写新机器实际值，
#     不写入 ~/.bashrc，避免污染全局环境；daemon 拉起的 agent 通过脚本显式 source .env）
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 参数解析: --repo <url> / --profile <name>
REPO_ARG=""; PROFILE_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_ARG="${2:-}"; shift 2 ;;
    --profile) PROFILE_ARG="${2:-}"; shift 2 ;;
    *) REPO_ARG="$1"; shift ;;
  esac
done
REPO="${REPO_ARG:-git@gitcode.com:developer-skill/skillsopr.git}"
SKILLS_SRC="tools/skills_dev/skills"
SCRIPTS_SRC="tools/skills_dev/scripts"
# Hermes 优先: 部署到 Hermes skills 目录（按分类子目录）
HERMES_SKILLS_BASE="${HERMES_SKILLS_BASE:-${HOME}/.hermes/skills}"
HERMES_SKILLS_CATEGORY="${HERMES_SKILLS_CATEGORY:-huawei-cloud}"
# agents 环境（multica）兼容部署目标
SKILLS_BASE="${HOME}/.agents/skills"
SKIP_AGENTS_SKILLS="${SKIP_AGENTS_SKILLS:-0}"
MULTICA_BIN="${MULTICA_BIN:-}"
CONFIG_DIR="${HOME}/.multica"
MULTICA_SERVER_URL="${MULTICA_SERVER_URL:-https://multica.clouddeveloper.club}"
MULTICA_PROFILE="${MULTICA_PROFILE:-${PROFILE_ARG:-dev}}"
PROFILE_CONFIG_DIR="${CONFIG_DIR}/profiles/${MULTICA_PROFILE}"

log()  { echo -e "\033[1;34m[deploy]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
fail() { echo -e "\033[1;31m[FAIL]\033[0m $*"; exit 1; }

ask() { # ask <var> <prompt> [default]
  local var="$1" prompt="$2" default="${3:-}"
  if [ -z "${!var:-}" ]; then
    if [ -n "$default" ]; then
      read -rp "$prompt [$default]: " val
      eval "$var=\"${val:-$default}\""
    else
      read -rp "$prompt: " val
      eval "$var=\"$val\""
    fi
  fi
}

# 查找 multica CLI（环境变量 / PATH / 常见位置）
find_multica() {
  if [ -n "$MULTICA_BIN" ] && [ -x "$MULTICA_BIN" ]; then return 0; fi
  if command -v multica >/dev/null 2>&1; then MULTICA_BIN="$(command -v multica)"; return 0; fi
  for p in "$(command -v multica 2>/dev/null)" /usr/local/bin/multica /opt/multica/multica; do
    if [ -x "$p" ]; then MULTICA_BIN="$p"; return 0; fi
  done
  return 1
}

# ---------------------------------------------------------------- prereq
log "=== 1. 检测系统依赖 ==="
MISSING=""
for cmd in python3 pip3 jq git curl node; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    MISSING="$MISSING $cmd"
  fi
done
if [ -n "$MISSING" ]; then
  warn "缺少依赖:$MISSING，尝试安装..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y >/dev/null 2>&1
    sudo apt-get install -y python3 python3-pip jq git curl nodejs >/dev/null 2>&1
    ok "apt 依赖已安装"
  else
    warn "非 apt 系统，请手动安装: python3 pip3 jq git curl node"
  fi
fi
python3 -c "import pymysql" 2>/dev/null || { log "安装 pymysql..."; pip3 install pymysql -i https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1 && ok "pymysql 已安装" || warn "pymysql 安装失败，DB 相关脚本不可用"; }
# hcloud KooCLI(工具链核心依赖, 61 处引用: 华为云真实调用)
if ! command -v hcloud >/dev/null 2>&1; then
  warn "未找到 hcloud KooCLI, 请先安装并配置 AK/SK (工具链核心依赖):"
  warn "  安装: https://support.huaweicloud.com/qs-hcli/hcli_02_003.html"
  warn "  配置: hcloud configure set --cli-mode=AKSK"
fi

# ---------------------------------------------------------------- multica CLI
log "=== 2. 检测 multica CLI ==="
if find_multica; then
  ok "multica CLI: $MULTICA_BIN"
else
  fail "未找到 multica CLI。请将 multica 可执行文件放到 PATH 或用 MULTICA_BIN 环境变量指定"
fi

# ---------------------------------------------------------------- skills
log "=== 3. 拉取工具链 Skills ==="
TMP=$(mktemp -d)
log "克隆 $REPO ..."
GIT_CLONE_OK=0
if git clone --depth=1 "$REPO" "$TMP/skillsopr" >/dev/null 2>&1; then
  GIT_CLONE_OK=1
else
  if [ -n "${GITCODE_TOKEN:-}" ]; then
    REPO_HOST="${REPO#*@}"; REPO_HOST="${REPO_HOST%%:*}"
    REPO_PATH="${REPO##*:}"
    REPO_HTTPS="https://oauth2:${GITCODE_TOKEN}@${REPO_HOST}/${REPO_PATH}"
    log "SSH clone 失败，尝试 HTTPS + token ..."
    git clone --depth=1 "$REPO_HTTPS" "$TMP/skillsopr" >/dev/null 2>&1 && GIT_CLONE_OK=1
  fi
  if [ "$GIT_CLONE_OK" -ne 1 ]; then
    warn "仓库克隆失败。若仓库为私有，请设置环境变量 GITCODE_TOKEN 后重试："
    warn "  export GITCODE_TOKEN=<你的GitCode token>"
    rm -rf "$TMP"
    exit 1
  fi
fi
HERMES_SKILL_DIR="$HERMES_SKILLS_BASE/$HERMES_SKILLS_CATEGORY"
mkdir -p "$HERMES_SKILL_DIR"
if [ -d "$TMP/skillsopr/$SKILLS_SRC" ]; then
  for d in "$TMP/skillsopr/$SKILLS_SRC"/*/; do
    name=$(basename "$d")
    # Hermes 优先部署
    cp -r "$d" "$HERMES_SKILL_DIR/$name"
    log "  部署 skill 到 Hermes: $HERMES_SKILLS_CATEGORY/$name"
    # agents 环境兼容部署（可跳过）
    if [ "$SKIP_AGENTS_SKILLS" != "1" ]; then
      mkdir -p "$SKILLS_BASE"
      cp -r "$d" "$SKILLS_BASE/$name"
      log "  部署 skill 到 agents: $name"
    fi
  done
  ok "Skills 已部署到 $HERMES_SKILL_DIR"
  [ "$SKIP_AGENTS_SKILLS" = "1" ] || ok "Skills 已兼容部署到 $SKILLS_BASE"
else
  warn "仓库中未找到 $SKILLS_SRC 目录"
fi

# ---------------------------------------------------------------- scripts
log "=== 4. 部署核心脚本（按 skill 目录精确部署） ==="
SCRIPTS_SRC_DIR="$TMP/skillsopr/$SCRIPTS_SRC"
if [ -d "$SCRIPTS_SRC_DIR" ]; then
  # skill-auto-test-pr 的脚本
  for f in pre-create-hook.sh process-skill.sh run-pipeline.sh report.mjs; do
    if [ -f "$SCRIPTS_SRC_DIR/$f" ]; then
      mkdir -p "$HERMES_SKILL_DIR/skill-auto-test-pr/scripts"
      cp "$SCRIPTS_SRC_DIR/$f" "$HERMES_SKILL_DIR/skill-auto-test-pr/scripts/" 2>/dev/null
      if [ "$SKIP_AGENTS_SKILLS" != "1" ]; then
        mkdir -p "$SKILLS_BASE/skill-auto-test-pr/scripts"
        cp "$SCRIPTS_SRC_DIR/$f" "$SKILLS_BASE/skill-auto-test-pr/scripts/" 2>/dev/null
      fi
    fi
  done
  # huawei-cloud-skill-creator 的脚本
  for f in feature-gate.sh report-dev-done.sh report-dev-failed.sh report-dev-processing.sh; do
    if [ -f "$SCRIPTS_SRC_DIR/$f" ]; then
      mkdir -p "$HERMES_SKILL_DIR/huawei-cloud-skill-creator/scripts"
      cp "$SCRIPTS_SRC_DIR/$f" "$HERMES_SKILL_DIR/huawei-cloud-skill-creator/scripts/" 2>/dev/null
      if [ "$SKIP_AGENTS_SKILLS" != "1" ]; then
        mkdir -p "$SKILLS_BASE/huawei-cloud-skill-creator/scripts"
        cp "$SCRIPTS_SRC_DIR/$f" "$SKILLS_BASE/huawei-cloud-skill-creator/scripts/" 2>/dev/null
      fi
    fi
  done
  chmod +x "$HERMES_SKILL_DIR/skill-auto-test-pr/scripts/"*.sh 2>/dev/null
  chmod +x "$HERMES_SKILL_DIR/huawei-cloud-skill-creator/scripts/"*.sh 2>/dev/null
  [ "$SKIP_AGENTS_SKILLS" = "1" ] || chmod +x "$SKILLS_BASE/skill-auto-test-pr/scripts/"*.sh 2>/dev/null
  [ "$SKIP_AGENTS_SKILLS" = "1" ] || chmod +x "$SKILLS_BASE/huawei-cloud-skill-creator/scripts/"*.sh 2>/dev/null
  ok "脚本已按对应 skill 目录部署"
fi

# 提前保存 dev agent 指令模板（$TMP 即将清理，步骤 5.2 复用）
DEV_AGENT_INSTRUCTIONS_FILE=""
if [ -f "$TMP/skillsopr/tools/skills_dev/deploy/agent_dev_instructions.md" ]; then
  DEV_AGENT_INSTRUCTIONS_FILE="${HERMES_SKILL_DIR}/.agent_dev_instructions.md"
  cp "$TMP/skillsopr/tools/skills_dev/deploy/agent_dev_instructions.md" "$DEV_AGENT_INSTRUCTIONS_FILE"
fi
rm -rf "$TMP"

# ---------------------------------------------------------------- multica profile config
log "=== 5. 配置 Multica profile [$MULTICA_PROFILE] ==="
ask MULTICA_WORKSPACE_ID "工作区 ID" ""
ask MULTICA_TOKEN "Multica API token (mul_ 开头)" ""
if [ -z "${MULTICA_WORKSPACE_ID:-}" ]; then
  fail "工作区 ID 不能为空"
fi
if [ -z "${MULTICA_TOKEN:-}" ]; then
  fail "Multica token 不能为空"
fi
mkdir -p "$PROFILE_CONFIG_DIR"
cat > "$PROFILE_CONFIG_DIR/config.json" <<EOF
{
  "server_url": "$MULTICA_SERVER_URL",
  "app_url": "$MULTICA_SERVER_URL",
  "workspace_id": "$MULTICA_WORKSPACE_ID",
  "token": "$MULTICA_TOKEN"
}
EOF
chmod 600 "$PROFILE_CONFIG_DIR/config.json"
ok "profile [$MULTICA_PROFILE] 配置已写入 $PROFILE_CONFIG_DIR/config.json"

# ---------------------------------------------------------------- token 验证
log "=== 5.1 验证 profile 连通性 ==="
if "$MULTICA_BIN" --profile "$MULTICA_PROFILE" workspace list --output json >/dev/null 2>&1; then
  ok "profile [$MULTICA_PROFILE] 认证成功"
else
  warn "profile [$MULTICA_PROFILE] 认证失败（token 无效或工作区无权限）"
  warn "请确认 token 与 workspace_id 正确后重跑:"
  warn "  MULTICA_TOKEN=... MULTICA_WORKSPACE_ID=... bash deploy-skills-dev.sh --profile $MULTICA_PROFILE"
fi

# 同时写入默认 config（兼容无 --profile 的直接调用）
# 注: 若已存在默认 config 且未指定 profile，则保留原默认；仅当当前 profile 为 dev 时同步
if [ "$MULTICA_PROFILE" = "dev" ] && [ ! -f "$CONFIG_DIR/config.json" ]; then
  cp "$PROFILE_CONFIG_DIR/config.json" "$CONFIG_DIR/config.json"
  ok "默认 config 已同步（无 --profile 时的配置）"
fi

# ---------------------------------------------------------------- dev agent 指令配置
log "=== 5.2 配置 dev agent 指令 ==="
if [ "$SKIP_AGENTS_SKILLS" != "1" ] && [ -n "$DEV_AGENT_INSTRUCTIONS_FILE" ] && [ -f "$DEV_AGENT_INSTRUCTIONS_FILE" ]; then
  DEV_AGENT_NAME="${DEV_AGENT_NAME:-Skills开发、测试、PR智能体}"
  DEV_AGENT_ID=$("$MULTICA_BIN" --profile "$MULTICA_PROFILE" agent list --output json 2>/dev/null \
    | AGENT_NAME="$DEV_AGENT_NAME" python3 -c "
import json,sys,os
name=os.environ['AGENT_NAME']
for a in json.load(sys.stdin):
    if a.get('name')==name and not a.get('archived_at'):
        print(a.get('id')); break
")
  if [ -n "${DEV_AGENT_ID}" ]; then
    log "  dev agent「${DEV_AGENT_NAME}」已存在 (${DEV_AGENT_ID})，更新 instructions..."
    # 指令模板中的部署期占位符在此替换(agent 指令是纯文本, 不会做变量展开)
    INSTRUCTIONS="$(cat "$DEV_AGENT_INSTRUCTIONS_FILE")"
    SQUAD_TEST_ID=$("$MULTICA_BIN" --profile "$MULTICA_PROFILE" squad list --output json 2>/dev/null \
      | SQUAD_NAME="${SQUAD_TEST_NAME:-skills_test}" python3 -c "
import json,sys,os
name=os.environ['SQUAD_NAME']
for s in json.load(sys.stdin):
    if s.get('name')==name:
        print(s.get('id')); break
")
    if [ -n "$SQUAD_TEST_ID" ]; then
      INSTRUCTIONS="${INSTRUCTIONS//\$\{SQUAD_TEST_ID\}/$SQUAD_TEST_ID}"
    else
      warn "  未找到小队 ${SQUAD_TEST_NAME:-skills_test}, 指令中 \${SQUAD_TEST_ID} 保持占位符"
    fi
    INSTRUCTIONS="${INSTRUCTIONS//\$\{GITCODE_PR_ASSIGNEE\}/${GITCODE_PR_ASSIGNEE:-}}"
    "$MULTICA_BIN" --profile "$MULTICA_PROFILE" agent update "${DEV_AGENT_ID}" \
      --instructions "$INSTRUCTIONS" >/dev/null 2>&1 \
      && ok "dev agent instructions 已更新" \
      || warn "dev agent instructions 更新失败（可能 token 权限不足）"
  else
    warn "未找到 dev agent「${DEV_AGENT_NAME}」，跳过指令配置（请先创建该 agent）"
  fi
  rm -f "$DEV_AGENT_INSTRUCTIONS_FILE"
else
  warn "跳过 dev agent 指令配置（SKIP_AGENTS_SKILLS=1 或模板缺失）"
fi

# ---------------------------------------------------------------- reporter config
log "=== 6. 配置 reporter ==="
mkdir -p "${HOME}/.config/huawei-cloud-skill-reporter"
REPORTER_URL="${REPORTER_URL:-http://skillsop.topxtopx.com/api/telemetry}"
echo "$REPORTER_URL" > "${HOME}/.config/huawei-cloud-skill-reporter/server_url.txt"
ok "reporter 上报地址已配置: $REPORTER_URL"

# ---------------------------------------------------------------- env 配置
log "=== 7. 生成工具链配置 .env（新机器请填写自己的值） ==="
ENV_FILE="${SKILLS_BASE}/.env"
if [ -f "$ENV_FILE" ]; then
  ok "检测到已有 $ENV_FILE，跳过生成（如需重配请删除后重跑）"
else
  cat > "$ENV_FILE" <<'EOF'
# ============================================================================
# skills_dev 工具链配置
# 请将下列值替换为新机器/新环境的实际配置！
# 脚本读取方式：环境变量优先，其次 source 本文件。
# 安全提示：本文件含敏感信息，权限建议 600。
# ============================================================================
# --- multica CLI 路径 ---
export MULTICA="$(command -v multica 2>/dev/null || echo /usr/local/bin/multica)"

# --- GitCode ---
export GITCODE_TOKEN=""
export GITCODE_REPO_SSH="git@gitcode.com:developer-skill/huaweicloud-skills-dev"
export GITCODE_OWNER="developer-skill"
export GITCODE_REPO="huaweicloud-skills-dev"
export GITCODE_CHECKOUT="huaweicloud-skills-dev"
export GITCODE_API="https://api.gitcode.com/api/v5"
# PR 合并人（审查人）login — 必须是目标仓库 Owner/Maintainer 级别用户才有权设置
# 可配置项：通过环境变量覆盖，默认 g30074593
export GITCODE_PR_ASSIGNEE="${GITCODE_PR_ASSIGNEE:-lccode}"
# 合并人数字用户 id（内网 API）——默认 11027048 = g30074593
export GITCODE_PR_ASSIGNEE_ID="${GITCODE_PR_ASSIGNEE_ID:-11935679}"
# 内网 API Bearer JWT + 刷新（设置合并人/issue 负责人用）；从浏览器 cookie 获取
export GITCODE_ACCESS_TOKEN="${GITCODE_ACCESS_TOKEN:-}"
export GITCODE_REFRESH_TOKEN="${GITCODE_REFRESH_TOKEN:-}"

# --- 数据库（DB 相关脚本需要） ---
# 必填: DB_HOST/DB_USER/DB_PASS/DB_NAME 已在脚本中移除内置默认, 必须在此填写真实值。
# 注意: 请填写真实密码, 不要保留 export DB_PASS="" (空字符串会覆盖脚本默认值,
# 导致 MySQL 连接失败 —— 2026-08-04 SKI-133 事故根因)。
export DB_HOST=""
export DB_USER=""
export DB_PASS=""
export DB_NAME=""

# --- 华为云 AK/SK（tester 真实环境测试需要） ---
export HUAWEICLOUD_SDK_AK=""
export HUAWEICLOUD_SDK_SK=""

# --- Multica(可选; 默认用工作区 slug 拼 issue 链接, 换工作区时改这里) ---
export MULTICA_SERVER_URL="https://multica.clouddeveloper.club"
export MULTICA_WORKSPACE_SLUG="skill-test"

# --- 技能索引仓库(find-skills 查询用, 可选) ---
export SKILLS_INDEX_REPO="2501_91318609/skills-for-index"
EOF
  chmod 600 "$ENV_FILE"
  warn "已生成配置模板: $ENV_FILE"
  warn "⚠️  请编辑 $ENV_FILE 填写新机器的实际配置后再使用工具链！"
  ok ".env 模板已生成（GITCODE/DB/AK/SK 等值需手动填写）"
fi
log "=== 8. 配置 GitCode SSH ==="
if [ -f "${HOME}/.ssh/id_ed25519" ]; then
  ok "SSH key 已存在"
  ssh -T -o StrictHostKeyChecking=no git@gitcode.com 2>&1 | grep -q "successfully" && ok "GitCode SSH 连通" || warn "SSH key 未添加到 GitCode 或网络不通"
else
  warn "未找到 ~/.ssh/id_ed25519，请在新机器生成并添加到 GitCode:"
  warn "  ssh-keygen -t ed25519 -C 'you@example.com'"
  warn "  然后访问 https://gitcode.com/setting/ssh 添加公钥"
fi

# ---------------------------------------------------------------- verify
log "=== 9. 验证部署 ==="
verify() {
  [ -d "$HERMES_SKILL_DIR/huawei-cloud-skill-creator" ] && ok "[Hermes] skill-creator 已部署" || warn "[Hermes] skill-creator 缺失"
  [ -d "$HERMES_SKILL_DIR/huawei-cloud-skill-tester" ] && ok "[Hermes] skill-tester 已部署" || warn "[Hermes] skill-tester 缺失"
  [ -d "$HERMES_SKILL_DIR/skill-auto-test-pr" ] && ok "[Hermes] skill-auto-test-pr 已部署" || warn "[Hermes] skill-auto-test-pr 缺失"
  [ -d "$HERMES_SKILL_DIR/huawei-cloud-skill-reporter" ] && ok "[Hermes] skill-reporter 已部署" || warn "[Hermes] skill-reporter 缺失"
  if [ "$SKIP_AGENTS_SKILLS" != "1" ]; then
    [ -d "$SKILLS_BASE/huawei-cloud-skill-creator" ] && ok "[agents] skill-creator 已部署" || warn "[agents] skill-creator 缺失"
    [ -d "$SKILLS_BASE/skill-auto-test-pr" ] && ok "[agents] skill-auto-test-pr 已部署" || warn "[agents] skill-auto-test-pr 缺失"
    [ -x "$SKILLS_BASE/skill-auto-test-pr/scripts/run-pipeline.sh" ] && bash -n "$SKILLS_BASE/skill-auto-test-pr/scripts/run-pipeline.sh" && ok "[agents] run-pipeline.sh 语法 OK" || warn "[agents] run-pipeline.sh 语法异常"
    [ -f "$SKILLS_BASE/.env" ] && ok "[agents] .env 配置已生成（请填写实际值）" || warn "[agents] .env 未生成"
  else
    [ -x "$HERMES_SKILL_DIR/skill-auto-test-pr/scripts/run-pipeline.sh" ] && bash -n "$HERMES_SKILL_DIR/skill-auto-test-pr/scripts/run-pipeline.sh" && ok "[Hermes] run-pipeline.sh 语法 OK" || warn "[Hermes] run-pipeline.sh 语法异常"
  fi
  # .env 必填项检查(脚本已移除内置默认, 缺失会导致 DB/GitCode 报错)
  if [ -f "${SKILLS_BASE}/.env" ]; then
    grep -qE '^export (DB_PASS|GITCODE_TOKEN)=".+"' "${SKILLS_BASE}/.env" \
      && ok "[.env] DB_PASS/GITCODE_TOKEN 已填写" \
      || warn "[.env] DB_PASS 或 GITCODE_TOKEN 未填写, 工具链 DB/GitCode 功能不可用!"
  fi
}
verify

echo ""
echo "=============================================="
echo "  部署完成！"
echo "  profile:     $MULTICA_PROFILE"
echo "  workspace:   $MULTICA_WORKSPACE_ID"
echo "  config:      $PROFILE_CONFIG_DIR/config.json"
echo "  skills(Hermes): $HERMES_SKILL_DIR"
[ "$SKIP_AGENTS_SKILLS" = "1" ] || echo "  skills(agents): $SKILLS_BASE"
echo ""
echo "  ⚠️  重要：请编辑 $SKILLS_BASE/.env 填写新机器的实际配置！"
echo "      包括 GITCODE_TOKEN / GITCODE_REPO_SSH / DB_HOST / DB_PASS / AK-SK 等"
echo ""
echo "  Hermes 加载新 skills 后即可在本会话中使用；若 Hermes 未识别，可运行:"
echo "    hermes skills list | grep huawei-cloud"
echo "  使用方式:"
echo "    multica --profile $MULTICA_PROFILE workspace list        # 查看该工作区"
echo "    multica --profile $MULTICA_PROFILE daemon start           # 启动 daemon"
echo "  多工作区: 每加一个工作区跑一次 --profile <新名字>"
echo "  token 变更: 重新提供 MULTICA_TOKEN 运行即可更新该 profile"
echo "=============================================="
