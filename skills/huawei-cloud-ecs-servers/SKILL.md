---
name: huawei-cloud-ecs-servers
version: 0.1.0
description: 查询华为云 ECS 云服务器（列表/详情/规格/可用区）。通过 hcloud CLI 调用，AK/SK 从项目知识/环境变量获取。
triggers:
  - 查询华为云ECS
  - ECS服务器列表
  - 云服务器查询
  - 华为云ECS
tags:
  - huawei-cloud
  - ecs
  - server
  - query
tools:
  - hcloud
  - python3
---

# huawei-cloud-ecs-servers

## 安全硬约束

1. 调用华为云 API 只用 hcloud CLI 已配置的 AK/SK，严禁从数据库/configs/日志/历史中寻找其他凭据。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
4. 凭据/token 不得写入任何文件/日志/评论。
5. 所有 hcloud 调用设 30s 超时——超时判定失败，不 hang。
6. 严禁 mock 模式/假数据：所有 API 调用必须真实访问华为云。
7. 调用 ECS API 一律以 `references/ecs-servers-api.md` 为准。

## Overview / 概述

本 skill 通过 hcloud CLI 查询华为云 ECS（弹性云服务器），支持查询服务器列表、服务器详情、规格列表、可用区。

- **执行模式**：CLI（hcloud KooCLI）
- **认证方式**：AK/SK（hcloud configure）
- **默认区域**：cn-north-4
- **权限要求**：ECS ReadOnlyAccess

## Prerequisites / 前置条件

- hcloud CLI 已安装（`hcloud --version` 可执行）
- AK/SK 已配置（优先项目知识 .project-info/ 下 JSON 文件，缺失时环境变量 HUAWEI_AK/HUAWEI_SK）
- IAM 权限：ECS ReadOnlyAccess（详见 `references/iam-policies.md`）
- CLI 安装指南：`references/cli-installation-guide.md`

## Workflow / 工作流

1. 检查 hcloud CLI 可用性（health-check）
2. 确定区域（默认 cn-north-4，用户可指定）
3. 执行查询操作（list/show/flavors/az）
4. 输出结果（JSON 或 Markdown 表格）

## Core Commands / 核心命令

### 服务器查询

```bash
# 列出服务器
python3 scripts/huawei-cloud-ecs-servers.py list --region cn-north-4 --limit 10

# 查询服务器详情
python3 scripts/huawei-cloud-ecs-servers.py show --region cn-north-4 --server-id <id>

# 按名称过滤
python3 scripts/huawei-cloud-ecs-servers.py list --name xxx

# 按状态过滤
python3 scripts/huawei-cloud-ecs-servers.py list --status ACTIVE
```

### 规格与可用区

```bash
# 查询规格
python3 scripts/huawei-cloud-ecs-servers.py flavors --region cn-north-4

# 查询可用区
python3 scripts/huawei-cloud-ecs-servers.py az --region cn-north-4
```

## KooCLI Command Format Standard

- Service: `ECS`
- Operations: `ListServersDetails`, `ShowServer`, `ListFlavors`, `NovaListAvailabilityZones`
- Region: `--cli-region=cn-north-4`（必填）
- 参数格式: `--param=value`（不是 `--param value`）
- 输出: 默认 JSON

## Parameter Confirmation / 参数确认

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| --region | string | 是 | cn-north-4 | 区域 |
| --limit | integer | 否 | 10 | 每页条数（最大 1000） |
| --offset | integer | 否 | - | 分页偏移 |
| --name | string | 否 | - | 服务器名称（模糊匹配） |
| --status | string | 否 | - | 服务器状态（ACTIVE/SHUTOFF） |
| --flavor | string | 否 | - | 规格 ID |
| --server-id | string | 是（show） | - | 服务器 ID |
| --format | string | 否 | json | 输出格式（json/md） |

## Reference Documents / 参考文档

- [ECS API 端点](references/ecs-servers-api.md)
- [IAM 权限策略](references/iam-policies.md)
- [CLI 安装指南](references/cli-installation-guide.md)
- [测试用例](templates/test-vars.json)

## 任务结束

完成所有业务动作后，在最终答复末尾输出以下 JSON 块并结束任务：

```json
{
  "actions": ["做了什么"],
  "conclusion": "处理结论",
  "artifacts": ["产生的产物"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```

注意：只输出一个 JSON 块，放在答复末尾。
