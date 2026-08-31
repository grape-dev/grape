#!/bin/bash
# run-skill-eval-gate.sh — 跑skill评测执行门禁
# 强制 skill-eval-tester-runner 的步骤 A-H 顺序执行，不得跳步
#
# 默认全自动无人值守：--enter 进入步骤后自动确认，无需人工介入
# 如需人工确认模式，加 --interactive 参数恢复逐步骤确认行为
#
# 用法：
#   1. 初始化评测:           run-skill-eval-gate.sh --init --skill <name> --repo <url>
#   2. 进入步骤(自动确认):   run-skill-eval-gate.sh --enter --step <A-H>
#   3. 校验文件命名:         run-skill-eval-gate.sh --validate-names --step <C|D|E|G> --files <path1:path2:...>
#   4. 查看状态:             run-skill-eval-gate.sh --status --skill <name>
#   5. 审计报告:             run-skill-eval-gate.sh --audit --skill <name>
#   6. 清理:                 run-skill-eval-gate.sh --cleanup --skill <name>
#
# 步骤流转: A -> B -> C -> D -> E -> F -> G -> H
# 违规检测: 进入步骤X时，若步骤X-1未确认，拒绝并提示
# 文件校验: 步骤 C/D/E/G 要求产出文件名符合规范

set -euo pipefail

STATE_DIR="$HOME/.skill-eval-gate"
mkdir -p "$STATE_DIR"

# --- 颜色 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- 步骤定义 ---
STEPS=("A" "B" "C" "D" "E" "F" "G" "H")
STEP_NAMES=(
    "A:执行评测(tester)"
    "B:问题归类分析"
    "C:生成初始问题单"
    "D:分析非pass项"
    "E:标准化报告优化建议"
    "F:去重分析"
    "G:生成最终汇总问题单"
    "H:归档"
)
STEP_DESC=(
    "用 huawei-cloud-skill-tester 跑 run-test-pipeline.sh --skills <目标skill>"
    "分析 tester 产出，按 HIGH/MEDIUM/LOW 归类，区分 tester 自身问题 vs 被测 skill 问题"
    "生成初始问题单文件，文件名: <skill名>_问题单_初始.md"
    "分析非 pass 项，文件名: <skill名>_非pass分析.md"
    "标准化报告和优化建议，文件名: <skill名>_报告优化.md"
    "去重分析，合并重复问题，文件名: <skill名>_去重分析.md"
    "生成最终汇总问题单，文件名: <skill名>_问题单.md"
    "归档到被测skill目录: <skill名>/tester/ 下，提交 GitCode"
)

# --- 参数解析 ---
ACTION=""
SKILL=""
STEP=""
FILES=""
REPO=""
AUTO_APPROVE="1"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --init)     ACTION="init"; shift ;;
        --enter)    ACTION="enter"; shift ;;
        --confirm)  ACTION="confirm"; shift ;;
        --validate-names) ACTION="validate-names"; shift ;;
        --status)   ACTION="status"; shift ;;
        --audit)    ACTION="audit"; shift ;;
        --cleanup)  ACTION="cleanup"; shift ;;
        --auto-approve) AUTO_APPROVE="1"; shift ;;
        --interactive)  AUTO_APPROVE=""; shift ;;
        --skill)    SKILL="$2"; shift 2 ;;
        --step)     STEP="$2"; shift 2 ;;
        --files)    FILES="$2"; shift 2 ;;
        --repo)     REPO="$2"; shift 2 ;;
        *)          echo "未知参数: $1"; exit 1 ;;
    esac
done

# --- 辅助函数 ---
get_state_file() {
    echo "$STATE_DIR/${SKILL}.state"
}

get_step_index() {
    local s="$1"
    for i in "${!STEPS[@]}"; do
        if [[ "${STEPS[$i]}" == "$s" ]]; then
            echo "$i"
            return
        fi
    done
    echo "-1"
}

get_prev_step() {
    local idx
    idx=$(get_step_index "$1")
    if [[ "$idx" -le 0 ]]; then
        echo ""
    else
        echo "${STEPS[$((idx - 1))]}"
    fi
}

is_step_confirmed() {
    local state_file
    state_file=$(get_state_file)
    if [[ ! -f "$state_file" ]]; then
        return 1
    fi
    grep -q "^step_$1=confirmed" "$state_file" 2>/dev/null
}

is_step_entered() {
    local state_file
    state_file=$(get_state_file)
    if [[ ! -f "$state_file" ]]; then
        return 1
    fi
    grep -q "^step_$1=entered" "$state_file" 2>/dev/null
}

print_step_info() {
    local idx
    idx=$(get_step_index "$1")
    if [[ "$idx" -ge 0 ]]; then
        echo -e "${CYAN}步骤 ${STEP_NAMES[$idx]}${NC}"
        echo "  要求: ${STEP_DESC[$idx]}"
    fi
}

# --- 动作: init ---
do_init() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    if [[ -z "$REPO" ]]; then
        echo -e "${RED}错误: 需要 --repo <url> 指定归档仓库地址${NC}"
        echo -e "${YELLOW}示例: --repo https://gitcode.com/<user>/<repo>/tree/main${NC}"
        exit 1
    fi
    local state_file
    state_file=$(get_state_file)
    cat > "$state_file" << EOF
skill=$SKILL
repo=$REPO
created=$(date '+%Y-%m-%d %H:%M:%S')
current_step=A
step_A=pending
step_B=pending
step_C=pending
step_D=pending
step_E=pending
step_F=pending
step_G=pending
step_H=pending
EOF
    echo -e "${GREEN}评测会话已初始化${NC}"
    echo "  被测 skill: $SKILL"
    echo "  归档仓库:   $REPO"
    echo "  状态文件:   $state_file"
    echo ""
    echo -e "${YELLOW}下一步: --enter --step A${NC}"
    print_step_info "A"
}

# --- 动作: enter ---
do_enter() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    if [[ -z "$STEP" ]]; then
        echo -e "${RED}错误: 需要 --step <A-H>${NC}"
        exit 1
    fi

    local state_file
    state_file=$(get_state_file)
    if [[ ! -f "$state_file" ]]; then
        echo -e "${RED}错误: 评测会话未初始化，请先 --init --skill $SKILL${NC}"
        exit 1
    fi

    # 检查步骤合法性
    local idx
    idx=$(get_step_index "$STEP")
    if [[ "$idx" -lt 0 ]]; then
        echo -e "${RED}错误: 无效步骤 $STEP，应为 A-H${NC}"
        exit 1
    fi

    # 检查前置步骤
    local prev
    prev=$(get_prev_step "$STEP")
    if [[ -n "$prev" ]] && ! is_step_confirmed "$prev"; then
        echo -e "${RED}拒绝: 步骤 $prev 尚未确认完成${NC}"
        echo -e "${YELLOW}请先完成并确认步骤 $prev: --confirm --step $prev${NC}"
        exit 1
    fi

    # 检查是否已进入
    if is_step_entered "$STEP"; then
        echo -e "${YELLOW}步骤 $STEP 已进入（重复进入允许，但请勿重复执行）${NC}"
    fi

    # 标记进入（仅当尚未 confirmed 时置 entered，避免状态回退）
    if ! is_step_confirmed "$STEP"; then
        sed -i "s/^step_${STEP}=.*/step_${STEP}=entered/" "$state_file"
    fi
    sed -i "s/^current_step=.*/current_step=${STEP}/" "$state_file"

    echo -e "${CYAN}=== 进入步骤 $STEP ===${NC}"
    print_step_info "$STEP"
    echo ""

    if [[ "$AUTO_APPROVE" == "1" ]]; then
        echo -e "${GREEN}  ⚡ 自动模式: 步骤 $STEP 自动确认（无人值守）${NC}"
        do_confirm
    else
        echo -e "${YELLOW}完成本步骤后，运行: --confirm --step $STEP${NC}"
    fi
}

# --- 动作: confirm ---
do_confirm() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    if [[ -z "$STEP" ]]; then
        echo -e "${RED}错误: 需要 --step <A-H>${NC}"
        exit 1
    fi

    local state_file
    state_file=$(get_state_file)
    if [[ ! -f "$state_file" ]]; then
        echo -e "${RED}错误: 评测会话未初始化${NC}"
        exit 1
    fi

    # 检查是否已进入
    if ! is_step_entered "$STEP"; then
        echo -e "${RED}拒绝: 步骤 $STEP 尚未进入${NC}"
        echo -e "${YELLOW}请先进入: --enter --step $STEP${NC}"
        exit 1
    fi

    # 检查是否已确认
    if is_step_confirmed "$STEP"; then
        echo -e "${YELLOW}步骤 $STEP 已确认（重复确认忽略）${NC}"
        return 0
    fi

    # 标记确认
    sed -i "s/^step_${STEP}=.*/step_${STEP}=confirmed/" "$state_file"

    echo -e "${GREEN}步骤 $STEP 已确认完成${NC}"

    # 提示下一步
    local idx
    idx=$(get_step_index "$STEP")
    local next_idx=$((idx + 1))
    if [[ "$next_idx" -lt "${#STEPS[@]}" ]]; then
        local next_step="${STEPS[$next_idx]}"
        echo ""
        echo -e "${YELLOW}下一步: --enter --step $next_step${NC}"
        print_step_info "$next_step"
    else
        echo ""
        echo -e "${GREEN}所有步骤已完成！${NC}"
        echo "运行 --audit --skill $SKILL 查看审计报告"
    fi
}

# --- 动作: validate-names ---
do_validate_names() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    if [[ -z "$STEP" ]]; then
        echo -e "${RED}错误: 需要 --step <C|D|E|G>${NC}"
        exit 1
    fi
    if [[ -z "$FILES" ]]; then
        echo -e "${RED}错误: 需要 --files <path1:path2:...>${NC}"
        exit 1
    fi

    # 文件命名规范
    local expected_patterns
    case "$STEP" in
        C) expected_patterns="${SKILL}_问题单_初始.md" ;;
        D) expected_patterns="${SKILL}_非pass分析.md" ;;
        E) expected_patterns="${SKILL}_报告优化.md" ;;
        G) expected_patterns="${SKILL}_问题单.md" ;;
        *) echo -e "${RED}错误: 步骤 $STEP 不需要文件命名校验（仅 C/D/E/G 需要）${NC}"; exit 1 ;;
    esac

    echo -e "${CYAN}=== 文件命名校验 (步骤 $STEP) ===${NC}"
    echo "期望文件名: $expected_patterns"
    echo ""

    local all_ok=true
    IFS=':' read -ra file_array <<< "$FILES"
    for f in "${file_array[@]}"; do
        local basename
        basename=$(basename "$f")
        if [[ -f "$f" ]]; then
            echo -e "  ${GREEN}✓${NC} $f (存在)"
        else
            echo -e "  ${RED}✗${NC} $f (文件不存在)"
            all_ok=false
        fi
        # 检查文件名是否包含期望模式
        if [[ "$basename" == *"$expected_patterns"* ]] || [[ "$basename" == "$expected_patterns" ]]; then
            echo -e "    ${GREEN}命名符合规范${NC}"
        else
            echo -e "    ${RED}命名: $basename (期望含: $expected_patterns)${NC}"
            all_ok=false
        fi
    done

    if [[ "$all_ok" == true ]]; then
        echo ""
        echo -e "${GREEN}文件校验通过${NC}"
    else
        echo ""
        echo -e "${RED}文件校验失败: 有文件不存在${NC}"
        exit 1
    fi
}

# --- 动作: status ---
do_status() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    local state_file
    state_file=$(get_state_file)
    if [[ ! -f "$state_file" ]]; then
        echo -e "${RED}评测会话未初始化${NC}"
        exit 1
    fi

    echo -e "${CYAN}=== 评测状态: $SKILL ===${NC}"
    local repo
    repo=$(grep "^repo=" "$state_file" | cut -d= -f2)
    echo "归档仓库: ${repo:-未指定}"
    echo ""
    for s in "${STEPS[@]}"; do
        local status
        status=$(grep "^step_${s}=" "$state_file" | cut -d= -f2)
        local idx
        idx=$(get_step_index "$s")
        local icon
        case "$status" in
            confirmed) icon="${GREEN}✓${NC}" ;;
            entered)   icon="${YELLOW}▶${NC}" ;;
            pending)   icon="○" ;;
            *)         icon="?" ;;
        esac
        echo -e "  $icon 步骤 ${STEP_NAMES[$idx]}"
    done
}

# --- 动作: audit ---
do_audit() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    local state_file
    state_file=$(get_state_file)
    if [[ ! -f "$state_file" ]]; then
        echo -e "${RED}评测会话未初始化${NC}"
        exit 1
    fi

    echo -e "${CYAN}=== 审计报告: $SKILL ===${NC}"
    echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
    local repo
    repo=$(grep "^repo=" "$state_file" | cut -d= -f2)
    echo "归档仓库: ${repo:-未指定}"
    echo ""

    local all_confirmed=true
    local violations=0

    for s in "${STEPS[@]}"; do
        local status
        status=$(grep "^step_${s}=" "$state_file" 2>/dev/null | cut -d= -f2 || echo "missing")
        local idx
        idx=$(get_step_index "$s")
        echo -e "步骤 ${STEP_NAMES[$idx]}: $status"
        if [[ "$status" != "confirmed" ]]; then
            all_confirmed=false
            violations=$((violations + 1))
        fi
    done

    echo ""
    if [[ "$all_confirmed" == true ]]; then
        echo -e "${GREEN}所有 8 个步骤均已确认完成，无违规${NC}"
    else
        echo -e "${RED}有 $violations 个步骤未完成${NC}"
    fi
}

# --- 动作: cleanup ---
do_cleanup() {
    if [[ -z "$SKILL" ]]; then
        echo -e "${RED}错误: 需要 --skill <name>${NC}"
        exit 1
    fi
    local state_file
    state_file=$(get_state_file)
    if [[ -f "$state_file" ]]; then
        rm -f "$state_file"
        echo -e "${GREEN}已清理 $SKILL 的评测状态${NC}"
    else
        echo -e "${YELLOW}无状态文件可清理${NC}"
    fi
}

# --- 主逻辑 ---
case "$ACTION" in
    init)           do_init ;;
    enter)          do_enter ;;
    confirm)        do_confirm ;;
    validate-names) do_validate_names ;;
    status)         do_status ;;
    audit)          do_audit ;;
    cleanup)        do_cleanup ;;
    "")             echo "用法: run-skill-eval-gate.sh --init --skill <name> --repo <url>|--enter|--confirm|--validate-names|--status|--audit|--cleanup --skill <name> [--step <A-H>] [--files <paths>]"; exit 1 ;;
    *)              echo "未知动作: $ACTION"; exit 1 ;;
esac
