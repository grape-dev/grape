# Issue #49: 优化 GitCode Webhook 事件对应的 Grape 处理

> **状态**: 待实施
> **创建**: 2026-09-10
> **分支**: multitask

## 1. 问题描述

当前 Grape 接收 GitCode webhook 后，只要是 Issue/PR 相关事件（包括责任人变更、Issue 关闭、标签变更等），都会进入 `run_pipeline` 全流程处理。这导致：

1. **大量无意义处理**：Issue 责任人变更、Issue 关闭/重开、标签添加/删除等事件不需要触发 Agent 执行
2. **Token 浪费**：每次事件都经过 `_resolve_issue_stage_agent` → `_match_repo_subscription` → 可能创建 event + 分发 Agent
3. **Event 表膨胀**：`append_skipped_event` 记录大量 skip 事件，占用存储
4. **日志噪音**：大量 skip 日志淹没真正需要关注的事件

## 2. 现状分析

### 2.1 事件归一化（`src/vcs/gitcode.py`）

GitCode webhook 的 `X-GitCode-Event` header 归一化为内部 event_type：

| GitCode Header | action | 归一化 event_type | 说明 |
|----------------|--------|-------------------|------|
| Issue Hook | open/create | `issue_opened` | 新建 Issue |
| Issue Hook | close/reopen/update/assign/label | `issue_updated` | **所有其他 Issue 变更混在一起** |
| Note Hook | — | `issue_comment` / `note_hook_pr` | 评论（区分 Issue/PR） |
| Merge Request Hook | merge | `pull_request_merged` | PR 合并 |
| Merge Request Hook | open/update/close | `pull_request_opened` | **PR 打开/更新/关闭混在一起** |
| push | — | `push` | 推送（已忽略） |
| tag_push | — | `tag_created` | 标签 |
| release | — | `release_created` | 发布 |
| pipeline | — | `build_start` | 构建 |

### 2.2 问题根因

1. **Issue Hook 的 action 粒度不足**：`close`/`reopen`/`assign`/`label` 全部归为 `issue_updated`，无法区分"内容更新"和"状态变更"
2. **Merge Request Hook 的 action 粒度不足**：`close`（关闭未合并）归为 `pull_request_opened`，无实际意义
3. **issue_updated 处理过重**：`issue_updated` 在 `_resolve_issue_stage_agent` 中走阶段矩阵，可能路由到 Agent 执行
4. **issue_labeled 虽有独立 type 但无 action 细分**：添加/删除标签混在一起

### 2.3 当前处理流程

```
webhook 接收 → normalize_event → run_pipeline
  → actor 自循环检测（bot 跳过）
  → _resolve_issue_stage_agent（阶段矩阵路由）
  → _match_repo_subscription（订阅兜底）
  → agent_type 为空 → append_skipped_event + return
  → agent_type 非空 → 创建 event + 分发 Agent
```

问题在于：`issue_updated`（含 close/reopen/assign）在阶段矩阵中有非空路由（`issue` agent），所以会创建 event 并尝试分发。

## 3. 技术方案

### 3.1 核心思路

**事件过滤网关**：在 `run_pipeline` 入口处（actor 检测之后、路由之前）增加事件过滤层，根据 `action` 细分判断事件是否需要处理。

### 3.2 事件归一化增强（`src/vcs/gitcode.py`）

细化 Issue Hook 和 Merge Request Hook 的 action 归一化：

```python
# Issue Hook action 细分
if event_header == "Issue Hook":
    obj = payload.get("object_attributes", {})
    action = obj.get("action", "")
    if action in ("open", "create"):
        evt_type = "issue_opened"
    elif action == "close":
        evt_type = "issue_closed"
    elif action == "reopen":
        evt_type = "issue_reopened"
    elif action == "update":
        # 进一步区分：assignee 变更 vs 内容更新
        changes = payload.get("changes", {})
        if "assignee" in changes:
            evt_type = "issue_assigned"
        elif "labels" in changes:
            evt_type = "issue_labeled"
        else:
            evt_type = "issue_updated"
    else:
        evt_type = "issue_updated"

# Merge Request Hook action 细分
if event_header in ("Merge Request Hook", "merge_request"):
    obj = payload.get("object_attributes", {})
    action = obj.get("action", "")
    if action == "merge":
        evt_type = "pull_request_merged"
    elif action == "open":
        evt_type = "pull_request_opened"
    elif action == "update":
        evt_type = "pull_request_updated"
    elif action == "close":
        evt_type = "pull_request_closed"  # 关闭未合并
    elif action == "reopen":
        evt_type = "pull_request_opened"
    else:
        evt_type = "pull_request_opened"
```

### 3.3 事件过滤网关（`src/api/event_pipeline.py`）

在 `run_pipeline` 中新增过滤逻辑（位于 actor 检测之后、路由之前）：

```python
# === Issue #49: 事件过滤网关 ===
# 不需要触发 Agent 的事件类型 → 直接跳过（不创建 event、不分发）
_SKIP_EVENT_TYPES = {
    "issue_closed",       # Issue 关闭：由 close_issue_tasks 处理（pending_review/done）
    "issue_reopened",     # Issue 重开：无 Agent 动作
    "issue_assigned",     # 责任人变更：无 Agent 动作
    "pull_request_closed", # PR 关闭未合并：无 Agent 动作
    "push",               # 推送：已忽略
}

# issue_labeled 需要进一步判断：只有 VOD 阶段需要响应标签变更
# （VOD 可能通过标签识别场景），其他阶段跳过

if event_type in _SKIP_EVENT_TYPES:
    log.info(f"Pipeline skip: event_type={event_type} (filtered by Issue #49 gateway)")
    return result
```

### 3.4 issue_closed 特殊处理

Issue 关闭事件仍需触发 `close_issue_tasks`（Issue #51: → pending_review），但不能走正常 pipeline 分发。处理方式：

```python
if event_type == "issue_closed":
    # Issue 关闭 → 触发 close_issue_tasks（pending_review 流程），不走 pipeline 分发
    _issue_id = resolve_issue_id(evt.get("payload", {}))
    if _issue_id:
        from src.api.routes.tasks import close_issue_tasks
        close_issue_tasks(repo_key, _issue_id)
        log.info(f"Issue closed → close_issue_tasks triggered for {_issue_id}")
    return result
```

### 3.5 issue_updated 精细化

当前 `issue_updated` 会路由到 `issue` agent。优化为：

- **内容更新**（title/body 变更）→ 走正常 pipeline（issue agent 可能需要重新分析）
- **assignee 变更** → 跳过（无 Agent 动作需要）
- **label 变更** → 仅 VOD 阶段响应（可能影响场景识别），其他阶段跳过

```python
if event_type == "issue_updated":
    changes = evt.get("payload", {}).get("changes", {})
    # 仅 assignee/label 变更 → 跳过
    if "assignee" in changes and "title" not in changes and "description" not in changes:
        log.info("Pipeline skip: issue_updated is assignee-only change")
        return result
```

### 3.6 routing.py 更新

```python
# 新增事件类型到 ISSUE_EVENT_TYPES
ISSUE_EVENT_TYPES = {
    "issue_opened", "issue_updated", "issue_comment", "issue_labeled",
    "issue_closed", "issue_reopened", "issue_assigned"
}

PR_EVENT_TYPES = {
    "pull_request_opened", "pull_request_updated", "pull_request_merged",
    "pull_request_closed", "note_hook_pr"
}
```

### 3.7 事件摘要更新（event_pipeline.py §5.5）

新增事件类型的摘要描述：

```python
elif event_type == "issue_closed":
    summary = f"Issue {issue_id} 已关闭"
elif event_type == "issue_reopened":
    summary = f"Issue {issue_id} 已重开"
elif event_type == "issue_assigned":
    summary = f"Issue {issue_id} 责任人变更"
elif event_type == "pull_request_closed":
    summary = f"PR {issue_id} 已关闭（未合并）"
elif event_type == "pull_request_updated":
    summary = f"PR {issue_id} 已更新"
```

## 4. 影响范围

### 4.1 修改文件

| 文件 | 改动 |
|------|------|
| `src/vcs/gitcode.py` | Issue Hook / MR Hook action 细分归一化 |
| `src/api/event_pipeline.py` | 新增事件过滤网关 + issue_closed 特殊处理 + 摘要更新 |
| `src/api/routing.py` | ISSUE_EVENT_TYPES / PR_EVENT_TYPES 新增类型 |

### 4.2 不受影响

- `webhook.py` — 接收和 token 校验逻辑不变
- `tasks.py` — close_issue_tasks 已有逻辑不变
- 前端 — 无需改动（事件展示用 event_type 字符串，自动适配）

### 4.3 兼容性

- 旧的 `issue_updated` 事件仍正常处理（webhook payload 无 changes 字段时走默认 `issue_updated`）
- 旧的 `pull_request_opened` 事件仍正常处理（action=open → pull_request_opened）

## 5. 预期收益

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| Issue 关闭事件处理 | 走完整 pipeline + skip event | 直接 close_issue_tasks + return |
| Issue assign 变更 | 走完整 pipeline + skip event | 直接 return |
| PR 关闭未合并 | 走完整 pipeline + skip event | 直接 return |
| Event 表记录 | 大量 skip 记录 | 仅有意义事件记录 |
| Token 消耗 | 每次事件可能触发 Agent | 仅有意义事件触发 Agent |
| 日志清晰度 | 大量 skip 日志 | 仅 skip 原因日志 |

## 6. 测试用例

### 6.1 事件归一化测试

| 用例 | GitCode Header | action | 预期 event_type |
|------|----------------|--------|-----------------|
| TC-01 | Issue Hook | open | issue_opened |
| TC-02 | Issue Hook | close | issue_closed |
| TC-03 | Issue Hook | reopen | issue_reopened |
| TC-04 | Issue Hook | update (assignee) | issue_assigned |
| TC-05 | Issue Hook | update (title) | issue_updated |
| TC-06 | Issue Hook | update (labels) | issue_labeled |
| TC-07 | Merge Request Hook | merge | pull_request_merged |
| TC-08 | Merge Request Hook | open | pull_request_opened |
| TC-09 | Merge Request Hook | update | pull_request_updated |
| TC-10 | Merge Request Hook | close | pull_request_closed |

### 6.2 过滤网关测试

| 用例 | event_type | 预期行为 |
|------|------------|----------|
| TC-11 | issue_closed | close_issue_tasks + return |
| TC-12 | issue_reopened | return（不处理） |
| TC-13 | issue_assigned | return（不处理） |
| TC-14 | pull_request_closed | return（不处理） |
| TC-15 | push | return（不处理） |
| TC-16 | issue_opened | 正常 pipeline 分发 |
| TC-17 | issue_comment | 正常 pipeline 分发 |
| TC-18 | pull_request_merged | 正常 pipeline 分发 |
| TC-19 | issue_updated (title) | 正常 pipeline 分发 |
| TC-20 | issue_updated (assignee only) | return（不处理） |

### 6.3 pytest

| 用例 | 测试文件 | 验证点 |
|------|----------|--------|
| TC-21 | test_webhook_filter.py | issue_closed → close_issue_tasks |
| TC-22 | test_webhook_filter.py | issue_assigned → 不创建 event |
| TC-23 | test_webhook_filter.py | pull_request_closed → 不创建 event |
| TC-24 | test_webhook_filter.py | issue_updated (title) → 正常分发 |
| TC-25 | test_webhook_filter.py | issue_opened → 正常分发 |

## 7. 实施计划

1. **Phase 1**: 事件归一化增强（gitcode.py action 细分）
2. **Phase 2**: 事件过滤网关（event_pipeline.py 过滤 + issue_closed 特殊处理）
3. **Phase 3**: routing.py 更新（事件类型集合扩展）
4. **Phase 4**: 测试用例编写 + 验证
5. **Phase 5**: 部署 + 观察 webhook 日志确认过滤效果
