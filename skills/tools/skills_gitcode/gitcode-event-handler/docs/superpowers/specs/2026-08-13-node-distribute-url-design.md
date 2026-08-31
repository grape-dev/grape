# 设计文档：node_distribute 节点 URL 统一指向 Multica 任务

日期: 2026-08-13
状态: 已批准（用户 clarify 选择"全部统一"）
目标脚本: /root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py

## 需求

t_skills_dev_flow 中 node_distribute_status 为 processing 时，node_distribute_url
统一改为 Multica 任务 URL。用户确认扩展：node_distribute 节点（含 in_progress
路径）URL 全部指向 Multica 任务。

## 现状（改动前）

| 位置 | 场景 | status | url | 是否符合 |
|------|------|--------|-----|----------|
| 6386 行 | 逻辑8 main 合入，需求已存在未指派→指派后 | processing | 仓库主页 https://gitcode.com/{repo} | ❌ |
| 6443 行 | 逻辑8 main 合入，未命中→新建分发任务 | processing | Multica 任务 URL (m_issue_url) | ✅ |
| 6923 行 | 逻辑7 distribute 分支合入，命中原任务 | in_progress | PR URL (pr_url) | ❌ |

## 改动（2 处）

1. 6386 行: url `f"https://gitcode.com/{repo}"` → `f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"`
   （m_issue_id 在 6358 行定义，multica_issue_get/assign 已用）
2. 6923 行: url `pr_url` → `f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"`
   （m_issue_id = matched["multica_issue_id"]，6911 行定义）

6443 行已符合（m_issue_url = {MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}），不动。

## 语义说明

node_distribute 节点的 url 语义统一为"分发任务在 Multica 的地址"；
PR 链接语义由 node_source/node_repo 承担（node_source=PR URL，node_repo=仓库主页）。

## 测试计划

新增 test_node_distribute_url.py:
- 逻辑8 路径（dispatch_merged_pr 指派分支）: 断言 db_update_flow 收到
  {MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}
- 逻辑7 路径（handle_distribute_pr 命中分支）: 断言 db_update_flow 收到
  {MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}

## 风险边界

- 存量数据 5 条 done+multica URL 不受影响（无 processing/in_progress 存量）
- 不涉及状态词本身（processing/in_progress 保持原样，仅 URL 统一）
