---
name: huawei-cloud
description: "华为云综合技能包 — 包含 ECS、VPC、OBS、CCE 等 22 个子技能"
version: 1
---

# 华为云技能包

本技能包包含以下 22 个华为云子技能，覆盖计算、网络、存储、数据库、安全等常见服务场景。

## 子技能列表

- **huawei-apig**: Use when creating or managing API Gateway (APIG). Covers API creation, throttling, auth (IAM/APP/basic), CORS, publishin
- **huawei-billing**: Use when querying bills, costs, resource usage, or billing details on Huawei Cloud (BSS). Triggers: billing, BSS, cost, 
- **huawei-cbr**: Use when creating or managing Cloud Backup and Recovery (CBR) vaults, backups, and restore operations on Huawei Cloud. T
- **huawei-cce**: Use when creating or managing CCE Kubernetes clusters. Covers cluster creation, node pools, SWR registry, autoscaling. T
- **huawei-cloud-eye**: Use when setting up monitoring, alarms, dashboards, or event rules on Huawei Cloud Eye (CES). Triggers: Cloud Eye, CES, 
- **huawei-cloud-find-skills**: |
- **huawei-cts**: Use when managing Cloud Trace Service (CTS) audit logs, trackers, and traces on Huawei Cloud. Triggers: CTS, Cloud Trace
- **huawei-dds-dcs**: Use when creating or managing Document Database Service (DDS/MongoDB-compatible) or Distributed Cache Service (DCS/Redis
- **huawei-deployment**: Use when creating, managing, or running deployment tasks and pipelines on Huawei Cloud CloudDeploy. Triggers: CloudDeplo
- **huawei-dew**: Use when managing secrets, credentials, encryption keys, certificates, or any sensitive data on Huawei Cloud. Covers CSM
- **huawei-ecs**: Use when creating, configuring, managing, or troubleshooting ECS instances on Huawei Cloud. Covers instance creation (hc
- **huawei-functiongraph**: Use when creating, deploying, or managing serverless functions on FunctionGraph. Covers triggers (APIG/OBS/timer/SMN), c
- **huawei-gaussdb**: Use when creating or managing GaussDB distributed database on Huawei Cloud. Covers GaussDB(for MySQL), GaussDB(for openG
- **huawei-getting-started**: Use when starting fresh with Huawei Cloud or KooCLI, installing prerequisites, setting up authentication, or exploring w
- **huawei-iam**: Use when managing IAM users, groups, roles, policies, agencies, projects, or access keys on Huawei Cloud. Covers IAM pol
- **huawei-modelarts**: Use when training, deploying, or managing AI/ML models on Huawei Cloud ModelArts. Covers training jobs, model registry, 
- **huawei-obs**: Use when creating, configuring, or managing OBS buckets and objects on Huawei Cloud. Covers bucket creation, lifecycle p
- **huawei-rds**: Use when creating, configuring, managing, or troubleshooting RDS instances on Huawei Cloud. Covers MySQL, PostgreSQL, SQ
- **huawei-sandbox**: Use when creating, connecting, or managing Huawei Cloud Sandbox instances and workspace terminals, or when a task needs 
- **huawei-smn-dms**: Use when creating or managing SMN topics/subscriptions/messages or DMS Kafka/RabbitMQ/RocketMQ queues on Huawei Cloud. T
- **huawei-vpc**: Use when creating, configuring, or managing VPC networks, subnets, security groups, EIPs, NAT gateways, VPN connections,
- **huawei-waf-aad**: Use when configuring Web Application Firewall (WAF) policies/rules, or Anti-DDoS (AAD) protection on Huawei Cloud. Trigg
- **grape-deploy**: Grape 平台专用部署子技能。当 SRE Agent 在 Grape 平台执行华为云 ECS 部署任务时使用。核心：用 Grape 注入的 HUAWEICLOUD_SDK_AK/SK 环境变量配置 hcloud CLI 认证，确保使用仓库 owner 的凭据而非 worker 机器预配凭据。Triggers: Grape A2A 交办部署、PR 合并后触发部署、用户请求 ECS 部署

## 路由规则

根据用户请求中的关键词，选择对应的子技能加载：

| 用户请求关键词 | 目标子技能 | 说明 |
|---------------|-----------|------|
| ecs, instance, 实例, 云服务器, flavor, 规格, 镜像 | huawei-ecs | ECS 弹性云服务器 |
| vpc, 网络, 子网, subnet, 安全组, security group, eip, nat, vpn, 路由, 带宽 | huawei-vpc | VPC 虚拟私有云 |
| obs, 存储桶, bucket, 对象存储, 生命周期, versioning, presigned | huawei-obs | OBS 对象存储 |
| cce, 容器, kubernetes, k8s, 集群, node pool, swr, 镜像仓库 | huawei-cce | CCE 云容器引擎 |
| iam, 权限, policy, 角色, role, 用户, user, group, ak/sk, access key, agency, 委托 | huawei-iam | IAM 统一身份认证 |
| rds, 数据库, mysql, postgresql, sqlserver, 关系型 | huawei-rds | RDS 关系型数据库 |
| gaussdb, 高斯, 分布式数据库 | huawei-gaussdb | GaussDB 数据库 |
| apig, api 网关, 接口, throttling, 流控 | huawei-apig | APIG API 网关 |
| billing, 账单, 费用, 成本, bss, 消费 | huawei-billing | Billing 费用中心 |
| cbr, 备份, 恢复, vault, backup, restore | huawei-cbr | CBR 云备份 |
| cloud eye, ces, 监控, 告警, dashboard, 指标, alarm | huawei-cloud-eye | Cloud Eye 云监控 |
| cts, 审计, 日志, trace, tracker, 操作记录 | huawei-cts | CTS 云审计 |
| dds, mongodb, dcs, redis, memcached, 缓存, 文档数据库 | huawei-dds-dcs | DDS/DCS 数据库与缓存 |
| deployment, clouddeploy, 部署, 发布, pipeline | huawei-deployment | Deployment 资源编排 |
| dew, 密钥, secret, credential, 加密, kms, csms, 证书, certificate | huawei-dew | DEW 数据加密 |
| functiongraph, 函数, 函数计算, serverless, 触发器 | huawei-functiongraph | FunctionGraph 函数计算 |
| modelarts, ai, 模型, 训练, 推理, 机器学习 | huawei-modelarts | ModelArts AI 开发平台 |
| smn, dms, 消息, 通知, kafka, rabbitmq, rocketmq, 队列, topic | huawei-smn-dms | SMN/DMS 消息服务 |
| waf, aad, 防火墙, ddos, 安全防护, web安全 | huawei-waf-aad | WAF/AAD Web安全与DDoS |
| sandbox, 沙箱, workspace, 终端, web app, 网页部署 | huawei-sandbox | Sandbox 沙箱环境 |
| 入门, 新手, 开始, kooCLI, hcloud, 安装, 初始化, auth, 认证 | huawei-getting-started | 入门指南 |
| grape, 部署, deploy, PR 合并, A2A, ECS 部署, 应用部署 | grape-deploy | Grape 平台部署（注入凭据 + ECS 创建 + 应用部署） |
| 其他未匹配的华为云关键词 | huawei-cloud-find-skills | 技能搜索 |

**使用步骤：**
1. 解析用户请求中的关键词，对照上表找到匹配的子技能
2. 用 `read references/<技能名>/SKILL.md` 加载该技能
3. 按 SKILL.md 中的步骤执行操作
4. 如果用户请求涉及多个服务（如"创建 ECS 并关联 VPC"），依次加载对应子技能


## 使用方式

根据用户需求，用 read tool 加载对应的子技能文件：

```
read references/<技能名>/SKILL.md
```

例如：
- 创建 ECS 实例 → 读 `references/huawei-ecs/SKILL.md`
- 配置 VPC 网络 → 读 `references/huawei-vpc/SKILL.md`
- 管理 OBS 存储桶 → 读 `references/huawei-obs/SKILL.md`
- Grape 平台部署应用 → 读 `references/grape-deploy/SKILL.md`

每个子技能的 references/ 目录下包含该服务的详细操作文档和参数参考。
