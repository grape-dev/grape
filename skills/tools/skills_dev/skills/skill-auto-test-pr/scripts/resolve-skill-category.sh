#!/usr/bin/env bash
# resolve-skill-category.sh — 根据 skill 名解析目标仓库分类目录 {category}/{subcategory}
#
# 用法: resolve-skill-category.sh <skill-name>
# 输出: 如 computing/ecs、storage/sfsturbo；前缀未命中时兜底 devtools/common
#
# 说明: 本脚本是分类映射的唯一权威来源，供两条链路共用，保证落盘路径一致:
#   1) skill-auto-test-pr/scripts/run-pipeline.sh — 自动测试构建 PR 时决定
#      skills/{category}/{subcategory}/{skill-name}/ 落盘目录
#   2) huawei-cloud-skill-creator — dev1/dev2 创建新技能时按同一映射决定目录层级
#
# 新增子分类: 在下方 case 中按技能名前缀添加分支即可
#   (如 huawei-cloud-sfsturbo-*) category="storage/sfsturbo" ;;)
# 一级分类沿用现有 12 类: computing/container/storage/network/security/
#   monitoring/ai/bigdata/bss/cdi/devtools/solution；subcategory 用服务简称。
# 强约束: 一级分类必须在这 12 类内；解析结果不在白名单（自创如 application、
#   历史遗留如 compute）一律强制归 devtools/common。

set -u

name="$1"

resolve_skill_category() {
  local name="$1"
  local category="devtools/common"

  case "$name" in
    huawei-cloud-cce-env-*)          category="solution/csm" ;;
    huawei-cloud-ecs-alert)          category="monitoring/ecs" ;;
    huawei-cloud-ecs-sqlbot-*)       category="computing/flexus-x" ;;
    huawei-cloud-ecs-*)               category="computing/ecs" ;;
    huawei-cloud-flexus-l-*)          category="computing/flexus-l" ;;
    huawei-cloud-flexus-x-*)          category="computing/flexus-x" ;;
    huawei-cloud-functiongraph-*)     category="computing/functiongraph" ;;
    huawei-cloud-computing-*)         category="computing/ops" ;;
    huawei-cloud-bms-*)              category="compute/bms" ;;
    huawei-cloud-cce-*|huawei-cloud-kubectl-*) category="container/cce" ;;
    huawei-cloud-cci-*)              category="container/cci" ;;
    huawei-cloud-swr-*)              category="container/swr" ;;
    huawei-cloud-ucs-*)              category="container/ucs" ;;
    huawei-cloud-asm-*)              category="container/asm" ;;
    huawei-cloud-obs-*)              category="storage/obs" ;;
    huawei-cloud-vbs-*|huawei-cloud-cbr-*) category="storage/cbr" ;;
    huawei-cloud-rds-*)              category="storage/rds" ;;
    huawei-cloud-storage-*)          category="storage/obs" ;;
    huawei-cloud-sfsturbo-*)         category="storage/sfsturbo" ;;
    huawei-cloud-dcs-*)              category="storage/dcs" ;;
    huawei-cloud-evs-*)              category="storage/evs" ;;
    huawei-cloud-gaussdb-*)          category="storage/gaussdb" ;;
    huawei-cloud-vpc-*|huawei-cloud-sg-*|huawei-cloud-network-*) category="network/ops" ;;
    huawei-cloud-nat-*)              category="network/nat" ;;
    huawei-cloud-eip-*|huawei-cloud-ip-*) category="network/eip" ;;
    huawei-cloud-dns-*)              category="network/dns" ;;
    huawei-cloud-vpcep-*)            category="network/vpcep" ;;
    huawei-cloud-iam-*|huawei-cloud-aksk-*) category="security/iam" ;;
    huawei-cloud-ces-*)              category="monitoring/ces" ;;
    huawei-cloud-monitoring-*)       category="monitoring/ops" ;;
    huawei-cloud-modelarts-*|huawei-cloud-ascend-*|huawei-cloud-ascendc-*|huawei-cloud-msmodelslim-*|huawei-cloud-msot-*) category="ai/modelarts" ;;
    huawei-cloud-maas-*)             category="ai/maas" ;;
    huawei-cloud-ges-*)              category="ai/ges" ;;
    huawei-cloud-dws-*)              category="bigdata/dws" ;;
    huawei-cloud-mrs-*)              category="bigdata/mrs" ;;
    huawei-cloud-css-*)              category="bigdata/css" ;;
    huawei-cloud-billing-*|huawei-cloud-business-*) category="bss/billing" ;;
    huawei-cloud-bss-*)              category="bss/billing" ;;
    huawei-cloud-icp-*)              category="cdi/assist" ;;
    huawei-cloud-cli-*|huawei-cloud-codearts-*) category="devtools/cli" ;;
    huawei-cloud-skill-creator|huawei-cloud-skill-tester|huawei-cloud-find-skills|huawei-cloud-vod-*) category="devtools/common" ;;
    huawei-cloud-terraform-*)        category="devtools/terraform" ;;
    huawei-cloud-install-openjiuwen*) category="devtools/openjiuwen" ;;
    huawei-cloud-sac-*)              category="solution/sac" ;;
  esac

  # 强约束: 一级分类必须是 12 个注册分类之一, 否则强制兜底 devtools/common
  # (覆盖自创分类如 application/apig、历史遗留 compute/bms 等未注册一级分类)
  case "${category%%/*}" in
    computing|container|storage|network|security|monitoring|ai|bigdata|bss|cdi|devtools|solution) ;;
    *) category="devtools/common" ;;
  esac

  echo "$category"
}

resolve_skill_category "$name"
