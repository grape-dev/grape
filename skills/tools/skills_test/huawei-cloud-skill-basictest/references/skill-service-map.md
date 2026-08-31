# Skill → 云服务映射表

本映射表用于在汇总报告中标注每个 skill 对应的华为云服务。

| Skill 名称 | 云服务 |
|------------|--------|
| huawei-cloud-obs-bucket-create | OBS 对象存储 |
| huawei-cloud-obs-upload | OBS 对象存储 |
| huawei-cloud-obs-stats | OBS 对象存储 |
| huawei-cloud-obs-website-host | OBS 对象存储 + DNS |
| huawei-cloud-swr-image-management | SWR 容器镜像仓库 |
| huawei-cloud-swr-image-automation | SWR 容器镜像仓库 |
| huawei-cloud-swr-image-governance | SWR 容器镜像仓库 |
| huawei-cloud-swr-enterprise-instance | SWR 容器镜像仓库 |
| huawei-cloud-ecs-passwordless-login | ECS 弹性云服务器 + COC |
| huawei-cloud-computing-query | ECS/BMS/IMS/AS 计算 |
| huawei-cloud-network-query | VPC/EIP/ELB/NAT/VPN/DNS 网络 |
| huawei-cloud-storage-query | EVS/OBS/SFS/CBR 存储 |
| huawei-cloud-iam-query | IAM 身份认证 |
| huawei-cloud-monitoring-query | CES/EPS 监控 |
| huawei-cloud-billing-scout | BSS 计费 |
| huawei-cloud-business-support-query | BSS 计费 |
| huawei-cloud-business-tf-support | BSS 计费 + Terraform |
| huawei-cloud-cci-instance-management | CCI 云容器实例 |
| huawei-cloud-codearts-code-gen | CodeArts 代码生成 |
| huawei-cloud-functiongraph-function-create | FunctionGraph 函数 |
| huawei-cloud-functiongraph-trigger-create | FunctionGraph 函数 |
| huawei-cloud-ges-graph | GES 图数据库 |
| huawei-cloud-skill-audit | Skill 审计工具 |
| huawei-cloud-skill-creator | Skill 创建工具 |
| huawei-cloud-skill-tester | Skill 测试工具 |
| huawei-cloud-terraform-generator | Terraform IaC |
| huawei-cloud-terraform-installer | Terraform 安装 |
| huawei-cloud-ucs-cluster-onboarding-manager | UCS 集群纳管 |
| huawei-cloud-ucs-policy-governor | UCS 策略治理 |
| huawei-cloud-vod-collector | VOC 体验反馈 |
| huawei-cloud-find-skills | Skill 发现工具 |
| huawei-cloud-flexus-l-deploy-jiuwenswarm | Flexus L 部署 |
| huawei-cloud-rds-list | RDS 云数据库 |

## 更新说明

当新增 skill 时，需要在此表和 `scripts/generate_summary.py` 中的 `SKILL_SERVICE_MAP` 字典同步添加映射。

未在映射表中的 skill 将显示为 "—"。
