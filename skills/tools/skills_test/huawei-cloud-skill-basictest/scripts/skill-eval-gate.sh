#!/bin/bash
# skill-eval-gate.sh — 通用 skill 评测执行门禁
# 适用于所有 skill 的基本功评测，强制每条用例执行前做规范自检并登记执行
#
# 默认全自动无人值守（--auto-approve）：register 时自动确认放行，无需人工介入
# 如需人工确认模式，加 --interactive 参数恢复逐条确认行为
#
# 用法：
#   1. 注册用例（执行前）:  skill-eval-gate.sh --register --skill <name> --case <id> --input <text> --trigger <words> --type <positive|negative> --resource-tag <tag>
#   2. 记录完成（执行后）:   skill-eval-gate.sh --complete --case <id> --result <pass|warn|fail>
#   3. 审计检查:             skill-eval-gate.sh --audit --skill <name>
#   4. 清理:                 skill-eval-gate.sh --cleanup --skill <name>
#   5. 中断恢复检查:         skill-eval-gate.sh --resume-check --skill <name>
#
# 状态流转: registered -> confirmed(自动/手动) -> completed
# 违规检测: 如果 complete 时 case 状态不是 confirmed，标记为 VIOLATION

set -euo pipefail

STATE_DIR="$HOME/.skill-eval-gate"
mkdir -p "$STATE_DIR"

# --- 颜色 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- 参数解析 ---
ACTION=""
SKILL=""
CASE=""
INPUT=""
TRIGGER=""
TYPE=""
RESULT=""
RESOURCE_TAG=""
ARCHIVE_DIR=""
AUTO_APPROVE="1"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --register) ACTION="register"; shift ;;
        --confirm)  ACTION="confirm"; shift ;;
        --complete) ACTION="complete"; shift ;;
        --audit)    ACTION="audit"; shift ;;
        --resume-check) ACTION="resume-check"; shift ;;
        --cleanup)  ACTION="cleanup"; shift ;;
        --validate-archive) ACTION="validate-archive"; shift ;;
        --auto-approve) AUTO_APPROVE="1"; shift ;;
        --interactive)  AUTO_APPROVE=""; shift ;;
        --skill)    SKILL="${2:-}"; shift 2 ;;
        --case)     CASE="${2:-}"; shift 2 ;;
        --input)    INPUT="${2:-}"; shift 2 ;;
        --trigger)  TRIGGER="${2:-}"; shift 2 ;;
        --type)     TYPE="${2:-}"; shift 2 ;;
        --result)   RESULT="${2:-}"; shift 2 ;;
        --resource-tag) RESOURCE_TAG="${2:-}"; shift 2 ;;
        --archive-dir) ARCHIVE_DIR="${2:-}"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

# --- 辅助函数 ---
get_state_file() {
    if [[ -n "$SKILL" ]]; then
        echo "$STATE_DIR/${SKILL}_${CASE}.json"
    else
        # 按 case ID 搜索
        local matches=$(ls "$STATE_DIR"/*_${CASE}.json 2>/dev/null || true)
        if [[ -n "$matches" ]]; then
            echo "$matches" | head -1
        else
            echo ""
        fi
    fi
}

get_state() {
    local f=$(get_state_file)
    if [[ -f "$f" ]]; then
        cat "$f"
    else
        echo ""
    fi
}

# --- 动作: register ---
if [[ "$ACTION" == "register" ]]; then
    if [[ -z "$SKILL" || -z "$CASE" || -z "$INPUT" || -z "$TYPE" ]]; then
        echo -e "${RED}[错误] 缺少必要参数: --skill, --case, --input, --type 均为必填${NC}"
        exit 1
    fi

    f=$(get_state_file)
    ts=$(date '+%Y-%m-%d %H:%M:%S')

    # 顺序强制检查：同一 skill 的前序用例必须全部 completed
    if [[ -n "$SKILL" ]]; then
        pending_files=$(ls "$STATE_DIR"/${SKILL}_*.json 2>/dev/null || true)
        if [[ -n "$pending_files" ]]; then
            incomplete=$(python3 -c "
import json, glob, sys
incomplete = []
for f in glob.glob('$STATE_DIR/${SKILL}_*.json'):
    try:
        with open(f) as fh:
            d = json.load(fh)
        if d.get('case') != '$CASE' and d.get('state') != 'completed':
            incomplete.append(f\"  {d.get('case','?')}: 状态={d.get('state','?')}\")
    except: pass
if incomplete:
    print('\\n'.join(incomplete))
" 2>/dev/null || true)
            if [[ -n "$incomplete" ]]; then
                echo -e "${RED}[顺序强制] 同一 skill 存在未完成的用例，拒绝注册新用例${NC}"
                echo -e "${RED}未完成的用例:${NC}"
                echo "$incomplete"
                echo -e "${YELLOW}请先完成前序用例（--complete），再注册新用例${NC}"
                exit 1
            fi
        fi
    fi

    # 写入状态文件（Python 原子写入 + argv 传参防注入）
    python3 -c "
import json,sys,os
f='$f'; ts='$ts'
d={'skill':sys.argv[1],'case':sys.argv[2],'input':sys.argv[3],'trigger':sys.argv[4],
   'type':sys.argv[5],'resource_tag':sys.argv[6],'state':'registered','registered_at':ts,
   'confirmed_at':'','completed_at':'','result':'','violation':False}
tmp=f+'.tmp'
with open(tmp,'w',encoding='utf-8') as fh:
    json.dump(d,fh,ensure_ascii=False,indent=2)
os.replace(tmp,f)
" "$SKILL" "$CASE" "$INPUT" "$TRIGGER" "$TYPE" "${RESOURCE_TAG:-未提供}"

    # 输出规范自检清单
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  评测执行门禁 — 规范自检${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  Skill:    ${SKILL}"
    echo -e "  用例编号: ${CASE}"
    echo -e "  用例类型: ${TYPE}"
    echo -e "  用户原话: ${INPUT}"
    echo -e "  触发词:   ${TRIGGER:-（未提供）}"
    echo -e "  时间:     ${ts}"
    echo ""
    echo -e "${YELLOW}── 规范自检清单 ──${NC}"
    echo ""
    echo -e "  [1] 触发词验证: 用户原话是否命中至少一个 SKILL.md 触发词?"
    echo -e "      → 请确认: ${TRIGGER:-未提供触发词，需补充}"
    echo ""
    if [[ "$AUTO_APPROVE" == "1" ]]; then
        echo -e "  [2] 用户确认: ${GREEN}自动放行（--auto-approve 模式，无人值守）${NC}"
        echo -e "  [3] 资源标识: 如涉及创建云资源，资源名称必须包含 ${RESOURCE_TAG:-未提供} 标识"
        echo -e "      → 请在执行后检查"
        echo -e "  [4] 规范自检: 自动模式已通过门禁校验"
        echo ""
        # 自动确认
        sleep 0.3
        ts2=$(date '+%Y-%m-%d %H:%M:%S')
        python3 -c "
import json
with open('$f') as fh:
    d = json.load(fh)
if d['state'] != 'registered':
    print(f'[错误] 用例状态为 {d[\"state\"]}，不能确认')
    exit(1)
d['state'] = 'confirmed'
d['confirmed_at'] = '$ts2'
with open('$f', 'w') as fh:
    json.dump(d, fh, indent=2)
print('  ✅ 用例 $CASE 已自动确认（auto-approve）')
"
        echo ""
        echo -e "${GREEN}  ⚡ 自动模式: 用例已确认，可直接执行 --complete${NC}"
        exit 0
    else
        echo -e "  [2] 用户确认: 是否已向用户展示本用例信息并等待确认?"
        echo -e "      → 状态: ${YELLOW}等待用户确认${NC}"
        echo ""
        echo -e "  [3] 资源标识: 如涉及创建云资源，资源名称是否包含 ${RESOURCE_TAG:-未提供} 标识?"
        echo -e "      → 请在执行后检查"
        echo ""
        echo -e "  [4] 规范自检: 本清单是否已展示给用户?"
        echo -e "      → ${GREEN}是，本清单即为自检展示${NC}"
        echo ""
        echo -e "${YELLOW}── 操作指引 ──${NC}"
        echo ""
        echo -e "  请将以上信息展示给用户，等待用户确认后执行:"
        echo -e "  ${GREEN}skill-eval-gate.sh --confirm --case ${CASE}${NC}"
        echo ""
        echo -e "${RED}  ⚠ 退出码 1 — 未确认，阻断执行${NC}"
        echo ""
        exit 1
    fi
fi

# --- 动作: confirm ---
if [[ "$ACTION" == "confirm" ]]; then
    if [[ -z "$CASE" ]]; then
        echo -e "${RED}[错误] 缺少 --case 参数${NC}"
        exit 1
    fi

    f=$(get_state_file)
    if [[ ! -f "$f" ]]; then
        echo -e "${RED}[错误] 用例 ${CASE} 未注册，请先调用 --register${NC}"
        exit 1
    fi

    ts=$(date '+%Y-%m-%d %H:%M:%S')

# 更新状态（含时间间隔检查）
python3 -c "
import json, time, os
from datetime import datetime
f='$f'; ts='$ts'; case='$CASE'
with open(f,encoding='utf-8') as fh:
    d = json.load(fh)
if d['state'] != 'registered':
    print(f'[错误] 用例状态为 {d[\"state\"]}，不能确认')
    exit(1)
# 检查 register 和 confirm 之间的时间间隔（至少 3 秒）
if '${AUTO_APPROVE}' != '1':
    reg_time = datetime.strptime(d['registered_at'], '%Y-%m-%d %H:%M:%S')
    now = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
    diff = (now - reg_time).total_seconds()
    if diff < 3:
        print(f'[错误] 注册后仅 {diff:.0f} 秒就确认，请等待用户确认后再执行（至少间隔 3 秒）')
        exit(1)
d['state'] = 'confirmed'
d['confirmed_at'] = ts
tmp=f+'.tmp'
with open(tmp,'w',encoding='utf-8') as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
os.replace(tmp,f)
print(f'✅ 用例 {case} 已确认，可以执行')
"

    exit 0
fi

# --- 动作: complete ---
if [[ "$ACTION" == "complete" ]]; then
    if [[ -z "$CASE" || -z "$RESULT" ]]; then
        echo -e "${RED}[错误] 缺少 --case 或 --result 参数${NC}"
        exit 1
    fi

    f=$(get_state_file)
    if [[ ! -f "$f" ]]; then
        echo -e "${RED}[错误] 用例 ${CASE} 未注册${NC}"
        exit 1
    fi

    ts=$(date '+%Y-%m-%d %H:%M:%S')

    # 更新状态，检测违规（argv 传参防注入 + 原子写入）
    python3 -c "
import json,sys,os
f='$f'; ts='$ts'; case='$CASE'
with open(f,encoding='utf-8') as fh:
    d = json.load(fh)
violation = False
if d['state'] != 'confirmed':
    violation = True
    print(f'⚠ 违规: 用例状态为 {d[\"state\"]}，未经确认就执行')
d['state'] = 'completed'
d['completed_at'] = ts
d['result'] = sys.argv[1]
d['violation'] = violation
tmp=f+'.tmp'
with open(tmp,'w',encoding='utf-8') as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
os.replace(tmp,f)
if violation:
    print(f'❌ 用例 {case} 已完成(结果: {sys.argv[1]})，但标记为违规')
else:
    print(f'✅ 用例 {case} 已完成(结果: {sys.argv[1]})')
" "$RESULT"

    exit 0
fi

# --- 动作: audit ---
if [[ "$ACTION" == "audit" ]]; then
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  评测执行审计报告${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    if [[ -n "$SKILL" ]]; then
        files=$(ls "$STATE_DIR"/${SKILL}_*.json 2>/dev/null || true)
    else
        files=$(ls "$STATE_DIR"/*.json 2>/dev/null || true)
    fi

    if [[ -z "$files" ]]; then
        echo "  无记录"
        echo ""
        exit 0
    fi

    printf "  %-8s %-10s %-12s %-10s %-10s %s\n" "用例" "类型" "状态" "结果" "违规" "确认时间"
    printf "  %-8s %-10s %-12s %-10s %-10s %s\n" "----" "----" "----" "----" "----" "----"

    total=0
    violations=0

    for f in $files; do
        python3 -c "
import json
with open('$f') as fh:
    d = json.load(fh)
viol = '❌是' if d.get('violation') else '否'
print(f'  {d[\"case\"]:<8} {d[\"type\"]:<10} {d[\"state\"]:<12} {d.get(\"result\",\"\") or \"-\":<10} {viol:<10} {d.get(\"confirmed_at\",\"\") or \"未确认\"}')
" 2>/dev/null || true
        total=$((total + 1))
        if python3 -c "import json; d=json.load(open('$f')); exit(0 if d.get('violation') else 1)" 2>/dev/null; then
            violations=$((violations + 1))
        fi
    done

    echo ""
    echo -e "  总计: ${total} 条用例, ${GREEN}合规 $((total - violations))${NC}, ${RED}违规 ${violations}${NC}"
    echo ""
    exit 0
fi

# --- 动作: resume-check ---
if [[ "$ACTION" == "resume-check" ]]; then
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  评测中断恢复检查${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    if [[ -n "$SKILL" ]]; then
        files=$(ls "$STATE_DIR"/${SKILL}_*.json 2>/dev/null || true)
    else
        files=$(ls "$STATE_DIR"/*.json 2>/dev/null || true)
    fi

    if [[ -z "$files" ]]; then
        echo "  无门禁记录，可能是首次执行或已清理"
        echo ""
        echo -e "  ${GREEN}→ 可直接开始执行下一条用例（走完整门禁流程）${NC}"
        echo ""
        exit 0
    fi

    # 分析各用例状态
    python3 -c "
import json, glob, sys

files = sorted(glob.glob('$STATE_DIR/${SKILL}_*.json')) if '$SKILL' else sorted(glob.glob('$STATE_DIR/*.json'))
if not files:
    print('  无门禁记录')
    sys.exit(0)

completed = []
incomplete = []
violations = []

for f in files:
    try:
        with open(f) as fh:
            d = json.load(fh)
    except:
        continue
    case = d.get('case', '?')
    state = d.get('state', '?')
    result = d.get('result', '')
    viol = d.get('violation', False)
    case_type = d.get('type', '?')

    if state == 'completed':
        completed.append((case, case_type, result))
    else:
        incomplete.append((case, case_type, state))
    if viol:
        violations.append((case, state))

print('  ── 已完成用例 ──')
if completed:
    for case, ctype, result in completed:
        print(f'    ✅ {case} ({ctype}) → {result or \"-\"}')
else:
    print('    （无）')

print()
print('  ── 未完成用例 ──')
if incomplete:
    for case, ctype, state in incomplete:
        print(f'    ⚠️  {case} ({ctype}) → 状态: {state}')
else:
    print('    （无）')

print()
if violations:
    print('  ── 违规记录 ──')
    for case, state in violations:
        print(f'    ❌ {case} → 未经确认就执行 (状态: {state})')
    print()

print('  ── 恢复指引 ──')
if incomplete:
    print(f'    存在 {len(incomplete)} 条未完成用例，必须先处理:')
    for case, ctype, state in incomplete:
        if state == 'registered':
            print(f'      {case}: 已注册未确认 → 需用户确认后 --confirm --case {case}')
        elif state == 'confirmed':
            print(f'      {case}: 已确认未执行 → 需执行后 --complete --case {case}')
        else:
            print(f'      {case}: 状态 {state} → 需人工处理')
    print()
    print('    处理完未完成用例后，才能注册新用例')
    print('    恢复后第一条新用例仍须走完整门禁流程 (register → 确认 → confirm → 执行 → complete)')
    sys.exit(1)
else:
    print('    所有用例已完成，可直接注册新用例')
    print('    新用例仍须走完整门禁流程 (register → 确认 → confirm → 执行 → complete)')
    sys.exit(0)
"

    echo ""
    exit 0
fi

# --- 动作: validate-archive ---
if [[ "$ACTION" == "validate-archive" ]]; then
    if [[ -z "$ARCHIVE_DIR" ]]; then
        echo -e "${RED}[错误] 缺少 --archive-dir 参数，指向 YYYY-MM-DD/ 归档目录${NC}"
        exit 1
    fi

    if [[ ! -d "$ARCHIVE_DIR" ]]; then
        echo -e "${RED}[错误] 归档目录不存在: $ARCHIVE_DIR${NC}"
        exit 1
    fi

    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  归档格式校验${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""

    errors=0
    warnings=0
    cases_found=0

    # 遍历归档目录下的用例目录
    for case_dir in "$ARCHIVE_DIR"/*/; do
        [[ -d "$case_dir" ]] || continue
        dir_name=$(basename "$case_dir")
        cases_found=$((cases_found + 1))

        echo -e "  ${CYAN}检查: $dir_name${NC}"

        # --- 检查1: 目录命名格式 ---
        # 应匹配 {P|N}{数字}_{场景描述}_{通过|告警|失败}
        if echo "$dir_name" | grep -qE '^[PN][0-9]+_.+_(通过|告警|失败)$'; then
            echo -e "    [1] 目录命名格式: ${GREEN}✅ 合规${NC}"
        else
            echo -e "    [1] 目录命名格式: ${RED}❌ 不合规${NC} — 应为 {编号}_{场景描述}_{状态}，实际: $dir_name"
            errors=$((errors + 1))
        fi

        # --- 检查2: 用例说明.md ---
        desc_file="${case_dir}用例说明.md"
        if [[ ! -f "$desc_file" ]]; then
            echo -e "    [2] 用例说明.md: ${RED}❌ 文件不存在${NC}"
            errors=$((errors + 1))
        else
            desc_err=""
            # 检查标题
            if ! grep -q '^# 用例说明:' "$desc_file"; then
                desc_err="${desc_err}  标题缺'# 用例说明:'\n"
            fi
            # 检查元信息 bullet 列表
            for field in "用例类型" "用例编号" "测试日期" "状态"; do
                if ! grep -q "^- ${field}:" "$desc_file"; then
                    desc_err="${desc_err}  元信息缺'${field}'\n"
                fi
            done
            # 检查必需子节
            for section in "## 用户原话" "## 触发验证" "## 预期行为" "## 实际行为" "## 报错质量" "## 测试结果"; do
                if ! grep -q "^${section}" "$desc_file"; then
                    desc_err="${desc_err}  缺子节'${section}'\n"
                fi
            done

            if [[ -z "$desc_err" ]]; then
                echo -e "    [2] 用例说明.md: ${GREEN}✅ 合规${NC}"
            else
                echo -e "    [2] 用例说明.md: ${RED}❌ 不合规${NC}"
                echo -e "$desc_err" | sed 's/^/      /'
                errors=$((errors + 1))
            fi
        fi

        # --- 检查3: 问题单.md（如存在）---
        issue_file="$case_dir问题单.md"
        if [[ ! -f "$issue_file" ]]; then
            echo -e "    [3] 问题单.md: ${YELLOW}（无问题单，跳过）${NC}"
        else
            issue_err=""
            # 检查标题
            if ! grep -q '^# 问题单:' "$issue_file"; then
                issue_err="${issue_err}  标题缺'# 问题单:'\n"
            fi
            # 检查文件头元信息
            for field in "来源" "测试日期" "被评测 skill" "评测方式" "问题数量" "缺陷分类"; do
                if ! grep -q "^- ${field}:" "$issue_file"; then
                    issue_err="${issue_err}  文件头缺'${field}'\n"
                fi
            done
            # 检查问题属性字段
            for field in "编号" "严重级别" "缺陷分类" "发现来源" "复现次数"; do
                if ! grep -q "| ${field} |" "$issue_file"; then
                    issue_err="${issue_err}  问题属性缺'${field}'\n"
                fi
            done
            # 检查必需子节
            for section in "### 触发输入" "### 预期行为" "### 实际行为" "### 报错质量" "### 具体问题" "### 影响分析" "### 改进点"; do
                if ! grep -q "^${section}" "$issue_file"; then
                    issue_err="${issue_err}  缺子节'${section}'\n"
                fi
            done
            # 检查汇总
            if ! grep -q '^## 汇总' "$issue_file"; then
                issue_err="${issue_err}  缺'## 汇总'\n"
            fi
            if ! grep -q '^### 按严重级别统计' "$issue_file"; then
                issue_err="${issue_err}  缺'### 按严重级别统计'\n"
            fi
            if ! grep -q '^### 按报错质量统计' "$issue_file"; then
                issue_err="${issue_err}  缺'### 按报错质量统计'\n"
            fi

            if [[ -z "$issue_err" ]]; then
                echo -e "    [3] 问题单.md: ${GREEN}✅ 合规${NC}"
            else
                echo -e "    [3] 问题单.md: ${RED}❌ 不合规${NC}"
                echo -e "$issue_err" | sed 's/^/      /'
                errors=$((errors + 1))
            fi
        fi

        echo ""
    done

    # --- 汇总 ---
    echo -e "${CYAN}── 校验汇总 ──${NC}"
    echo "  用例目录数: $cases_found"
    if [[ $errors -eq 0 ]]; then
        echo -e "  结果: ${GREEN}✅ 全部合规${NC}"
        exit 0
    else
        echo -e "  结果: ${RED}❌ ${errors} 处不合规${NC}"
        echo -e "  ${YELLOW}必须修复后重新校验，通过后方可继续后续流程${NC}"
        exit 1
    fi
fi

# --- 动作: cleanup ---
if [[ "$ACTION" == "cleanup" ]]; then
    if [[ -n "$SKILL" ]]; then
        rm -f "$STATE_DIR"/${SKILL}_*.json
        echo "已清理 ${SKILL} 的门禁记录"
    else
        rm -f "$STATE_DIR"/*.json
        echo "已清理所有门禁记录"
    fi
    exit 0
fi

echo "用法: skill-eval-gate.sh --register|--confirm|--complete|--audit|--resume-check|--validate-archive|--cleanup ..."
exit 1
