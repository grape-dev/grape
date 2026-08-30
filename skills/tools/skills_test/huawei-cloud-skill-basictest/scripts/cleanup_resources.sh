#!/bin/bash
# cleanup_resources.sh — 评测资源检测与释放脚本
# 按资源标识扫描并释放评测过程中创建的华为云资源（ECS/EIP/安全组/IP地址组/ELB）
# 无论测试是否发现 BUG，评测收尾时自动调用
#
# 用法：
#   bash cleanup_resources.sh --resource-tag <标识> [--region <区域>] [--dry-run]
#     --resource-tag  必填，只释放名称/描述含此标识的资源（如 dbq20260803）
#     --region        可选，默认 cn-north-4
#     --dry-run       只扫描不释放（检测模式）
#   bash cleanup_resources.sh --report <日期目录> --resource-tag <标识>
#     将结果报告写入指定目录
#
# 退出码：
#   0 全部资源释放成功或未发现目标资源
#   1 存在释放失败的资源

set -uo pipefail

HCLOUD="${HCLOUD:-hcloud}"
REGION="${REGION:-cn-north-4}"
TAG=""
DRY_RUN=""
REPORT_DIR=""

# --- 参数解析 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --resource-tag) TAG="$2"; shift 2 ;;
        --region)       REGION="$2"; shift 2 ;;
        --dry-run)      DRY_RUN="1"; shift ;;
        --report)       REPORT_DIR="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [[ -z "$TAG" ]]; then
    echo "错误: 缺少 --resource-tag 参数" >&2
    exit 1
fi

if ! command -v "$HCLOUD" >/dev/null 2>&1; then
    # 尝试常见安装路径
    if [[ -x "$HOME/.local/hcloud/hcloud" ]]; then
        HCLOUD="$HOME/.local/hcloud/hcloud"
    else
        echo "错误: 未找到 hcloud CLI，无法执行资源释放（资源可能遗留）。请先安装 hcloud。" >&2
        exit 2
    fi
fi

OUTPUT_DIR="${REPORT_DIR:-$(pwd)}"
mkdir -p "$OUTPUT_DIR"
REPORT_FILE="$OUTPUT_DIR/资源释放报告.md"

RELEASED=0
FAILED=0
SCANNED=0
SKIPPED_DRYRUN=0
RELEASED_ITEMS=()
FAILED_ITEMS=()

log() { echo -e "$*"; }
report() { echo -e "$*" >> "$REPORT_FILE"; }

init_report() {
    : > "$REPORT_FILE"
    report "# 资源释放报告"
    report ""
    report "- 执行时间: $(date '+%Y-%m-%d %H:%M:%S')"
    report "- 资源标识: \`$TAG\`"
    report "- 区域: \`$REGION\`"
    report "- 模式: $([[ -n "$DRY_RUN" ]] && echo 'dry-run（仅检测）' || echo '正常（检测并释放）')"
    report ""
}

# 扫描并释放通用函数
# 参数: $1=资源类型, $2=List命令, $3=Delete命令, $4=顶层字段, $5=ID字段, $6=Name字段, $7=删除ID参数名
scan_release() {
    local type="$1" list_cmd="$2" del_cmd="$3" top_field="$4" id_field="$5" name_field="$6" del_id_param="$7"
    local i id name
    log ""
    log "──────────────────────────────────────────────────────"
    log "扫描资源类型: ${type}"
    log "──────────────────────────────────────────────────────"
    report ""
    report "## ${type}"
    report ""

    local list_out
    list_out=$("$HCLOUD" $list_cmd --cli-region="$REGION" --cli-output=json 2>/dev/null || true)
    if [[ -z "$list_out" || "$list_out" == *"error"* || "$list_out" == *"Unauthorized"* || "$list_out" == *"required"* ]]; then
        log "  跳过: 无法查询（无凭证或服务不可用）"
        report "- 无法查询（无凭证或服务不可用），跳过"
        return 0
    fi

    # 提取对应顶层字段数组，按资源标识过滤（name/名称字段）
    local found
    found=$(echo "$list_out" | jq --arg tag "$TAG" --arg top "$top_field" --arg nf "$name_field" '
        .[$top] // []
        | map(select((.[$nf] // .name // "") | contains($tag)))
    ' 2>/dev/null || true)
    if [[ -z "$found" || "$found" == "null" ]]; then
        found="[]"
    fi

    local count
    count=$(echo "$found" | jq 'length' 2>/dev/null || echo 0)
    if [[ "$count" == "0" ]]; then
        log "  未发现含标识 \`$TAG\` 的 ${type} 资源"
        report "- 未发现含标识 \`$TAG\` 的资源"
        return 0
    fi

    log "  发现 ${count} 个含标识 \`$TAG\` 的 ${type} 资源"
    SCANNED=$((SCANNED + count))

    for i in $(seq 0 $((count - 1))); do
        id=$(echo "$found" | jq -r ".[$i].[\"$id_field\"] // empty" 2>/dev/null)
        name=$(echo "$found" | jq -r ".[$i].[\"$name_field\"] // .[$i].name // empty" 2>/dev/null)
        [[ -z "$id" ]] && id=$(echo "$found" | jq -r ".[$i].id // empty")
        log "  - ${name:-unknown} (${id:-unknown})"

        if [[ -n "$DRY_RUN" ]]; then
            SKIPPED_DRYRUN=$((SKIPPED_DRYRUN + 1))
            log "    [dry-run] 将释放"
            report "- [dry-run] 将释放: ${name:-unknown} (${id:-unknown})"
            continue
        fi

        local result
        result=$("$HCLOUD" $del_cmd --cli-region="$REGION" "--$del_id_param=$id" 2>&1 || true)
        if [[ -n "$result" ]] && (echo "$result" | grep -qiE "error|failed|失败|denied|not supported|USE_ERROR" ); then
            FAILED=$((FAILED + 1))
            FAILED_ITEMS+=("$type:${name}")
            log "    ❌ 释放失败: $result"
            report "- ❌ 释放失败: ${name:-unknown} (${id:-unknown}): $result"
        else
            RELEASED=$((RELEASED + 1))
            RELEASED_ITEMS+=("$type:${name}")
            log "    ✅ 已释放"
            report "- ✅ 已释放: ${name:-unknown} (${id:-unknown})"
        fi
    done
}

init_report

# --- ECS 实例（ListServersDetails 返回 servers[]，删除用 NovaDeleteServer --server_id）---
scan_release "ECS实例" "ECS ListServersDetails" "ECS NovaDeleteServer" "servers" "id" "name" "server_id"

# --- EIP 弹性公网IP（返回 publicips[]，删除用 DeletePublicip --publicip_id）---
scan_release "弹性公网IP(EIP)" "EIP ListPublicips" "EIP DeletePublicip" "publicips" "id" "public_ip_address" "publicip_id"

# --- 安全组（返回 security_groups[]，删除用 DeleteSecurityGroup/v3 --security_group_id）---
scan_release "安全组" "VPC ListSecurityGroups/v3" "VPC DeleteSecurityGroup/v3" "security_groups" "id" "name" "security_group_id"

# --- IP地址组（返回 address_groups[]，删除用 DeleteAddressGroup --address_group_id）---
scan_release "IP地址组" "VPC ListAddressGroup" "VPC DeleteAddressGroup" "address_groups" "id" "name" "address_group_id"

# --- ELB 负载均衡（返回 loadbalancers[]，删除用 DeleteLoadBalancer/v2 --loadbalancer_id）---
scan_release "ELB负载均衡" "ELB ListLoadBalancers" "ELB DeleteLoadBalancer/v2" "loadbalancers" "id" "name" "loadbalancer_id"

# --- 汇总 ---
log ""
log "══════════════════════════════════════════════"
log "  资源释放汇总"
log "══════════════════════════════════════════════"
log "  扫描到目标资源: ${SCANNED}"
log "  已释放: ${RELEASED}"
log "  释放失败: ${FAILED}"
[[ -n "$DRY_RUN" ]] && log "  (dry-run 模式，未实际释放 ${SKIPPED_DRYRUN} 个)"

report ""
report "## 汇总"
report ""
report "| 指标 | 数量 |"
report "|------|------|"
report "| 扫描到目标资源 | ${SCANNED} |"
report "| 已释放 | ${RELEASED} |"
report "| 释放失败 | ${FAILED} |"
[[ -n "$DRY_RUN" ]] && report "| dry-run 跳过 | ${SKIPPED_DRYRUN} |"
report ""

if [[ ${#FAILED_ITEMS[@]} -gt 0 ]]; then
    log "  释放失败的资源: ${FAILED_ITEMS[*]}"
    report "释放失败清单: ${FAILED_ITEMS[*]}"
    echo "资源释放报告: $REPORT_FILE"
    exit 1
else
    log "  ✅ 全部资源已释放（或未发现目标资源）"
    echo "资源释放报告: $REPORT_FILE"
    exit 0
fi
