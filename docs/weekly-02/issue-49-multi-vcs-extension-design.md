# Issue #49 扩展: 多 VCS 平台 Webhook 事件过滤统一架构

> **状态**: 待实施
> **创建**: 2026-09-10
> **分支**: multitask
> **前置**: Issue #49 已实施（event_action_filter + event_rule.py + gitcode/github backend）

## 1. 背景与目标

当前 Grape 已支持 GitCode 和 GitHub 两个 VCS 后端（`src/vcs/gitcode.py` / `src/vcs/github.py`），未来将扩展 GitLab、Gitee 等平台。Issue #49 的事件过滤机制（`event_action_filter` DB 表 + `event_rule.py` L1 准入判定）已具备平台无关的基础，但存在以下扩展瓶颈：

1. **事件归一化不统一**：GitHub 的 `issues` event 不区分 action（open/close/reopen/assign 全归为 `issue_opened`），而 GitCode 已细分
2. **event_action_filter 规则按 event_type 匹配**：但不同平台的 action 语义不一致（GitCode `update` vs GitHub `edited`），需要平台维度的规则隔离
3. **缺少 GitLab 适配**：GitLab webhook payload 结构与 GitCode/GitHub 差异大（`object_kind` + `object_attributes.action`）
4. **repo_key provider 前缀硬编码 gitcode**：`to_repo_key()` 默认 provider="gitcode"，新增平台时需显式传参
5. **API 客户端分散**：各平台 REST API（评论/PR/Issue）调用散落在 `repogit.py`、`tasks.py` 等，缺少统一抽象

## 2. 现状架构

```
                    ┌──────────────────────────────────────────────────┐
                    │              webhook.py (入口)                   │
                    │  X-GitCode-Event / X-GitHub-Event → source 判定   │
                    └──────────┬───────────────────────────────────────┘
                               │
                    ┌──────────▼───────────────────────────────────────┐
                    │           vcs/adapter.py                         │
                    │  normalize_event(raw, header, source)            │
                    │  _BACKENDS = {gitcode, github}                   │
                    └──────────┬───────────────────────────────────────┘
                               │
           ┌───────────────────┼───────────────────┐
           ▼                   ▼                   ▼
    vcs/gitcode.py       vcs/github.py       (vcs/gitlab.py 待实现)
    Issue Hook → action  issues → issue_opened
    Note Hook → comment  pull_request → opened
    MR Hook → merged     (action 未细分)
           │                   │
           └───────────┬───────┘
                       ▼
              event_pipeline.run_pipeline
                       │
            ┌──────────▼──────────────────┐
            │ event_rule.evaluate_event_action │
            │ DB: event_action_filter          │
            │ (event_type + action → decision) │
            └──────────┬──────────────────┘
                       │ allow / skip / side_effect
                       ▼
              _resolve_issue_stage_agent
              (阶段矩阵路由)
```

### 2.1 当前 event_action_filter 规则

```
event_type          action    allow  actor  require_changes      block_changes        side_effect
issue_updated       update    1      user   [description,title]  [assignees,labels]
issue_updated       close     2      any                                            close_issue_tasks
issue_updated       reopen    0      any
pull_request_opened open      1      any
pull_request_opened update    1      user
pull_request_opened close     0      any
pull_request_opened merge     1      any
pull_request_merged merge     1      any
```

问题：规则按 `(event_type, action)` 匹配，但 GitHub 的 `issues` event 的 action 是 `opened/closed/reopened/edited/assigned/labeled`，与 GitCode 的 `update/close/reopen` 不一致。

## 3. 设计方案

### 3.1 统一事件类型规范（Canonical Event Types）

定义跨平台统一的内部事件类型，所有 VCS backend 归一化时必须映射到这些类型：

```python
# src/vcs/event_types.py（新建）

# === Issue 事件 ===
ISSUE_OPENED    = "issue_opened"       # 新建 Issue
ISSUE_UPDATED   = "issue_updated"      # 内容更新（title/description 变更）
ISSUE_CLOSED    = "issue_closed"       # Issue 关闭
ISSUE_REOPENED  = "issue_reopened"     # Issue 重开
ISSUE_ASSIGNED  = "issue_assigned"     # 责任人变更
ISSUE_LABELED   = "issue_labeled"      # 标签变更
ISSUE_COMMENT   = "issue_comment"      # Issue 评论

# === PR 事件 ===
PR_OPENED       = "pull_request_opened"    # 新建 PR
PR_UPDATED      = "pull_request_updated"   # PR 更新（new commit / title 变更）
PR_CLOSED       = "pull_request_closed"    # PR 关闭（未合并）
PR_MERGED       = "pull_request_merged"    # PR 合并
PR_REVIEW       = "pull_request_review"    # PR Review
PR_COMMENT      = "note_hook_pr"           # PR 评论

# === 其他 ===
PUSH            = "push"
TAG_CREATED     = "tag_created"
RELEASE_CREATED = "release_created"
BUILD_START     = "build_start"
BUILD_FAILURE   = "build_failure"
DEPLOY_START    = "deploy_start"
DEPLOY_FINISH   = "deploy_finish"

# 所有需要阶段矩阵路由的事件
ROUTABLE_EVENTS = {
    ISSUE_OPENED, ISSUE_UPDATED, ISSUE_COMMENT, ISSUE_LABELED,
    PR_OPENED, PR_UPDATED, PR_MERGED, PR_COMMENT, PR_REVIEW,
}

# 不需要 Agent 处理的事件（直接过滤）
SKIP_EVENTS = {
    ISSUE_CLOSED, ISSUE_REOPENED, ISSUE_ASSIGNED,
    PR_CLOSED, PUSH,
}

# 副作用事件（执行副作用后短路，不进入 Agent）
SIDE_EFFECT_EVENTS = {
    ISSUE_CLOSED,  # → close_issue_tasks
}
```

### 3.2 各平台 Action 映射表

#### GitCode

| GitCode Header | action | → Canonical Event Type |
|----------------|--------|------------------------|
| Issue Hook | open/create | `issue_opened` |
| Issue Hook | close | `issue_closed` |
| Issue Hook | reopen | `issue_reopened` |
| Issue Hook | update + changes.assignees | `issue_assigned` |
| Issue Hook | update + changes.labels | `issue_labeled` |
| Issue Hook | update + changes.title/description | `issue_updated` |
| Note Hook | — | `issue_comment` / `note_hook_pr` |
| Merge Request Hook | open | `pull_request_opened` |
| Merge Request Hook | update | `pull_request_updated` |
| Merge Request Hook | close | `pull_request_closed` |
| Merge Request Hook | merge | `pull_request_merged` |

#### GitHub

| GitHub Header | action | → Canonical Event Type |
|---------------|--------|------------------------|
| issues | opened | `issue_opened` |
| issues | closed | `issue_closed` |
| issues | reopened | `issue_reopened` |
| issues | assigned | `issue_assigned` |
| issues | labeled | `issue_labeled` |
| issues | edited | `issue_updated` |
| issue_comment | created | `issue_comment` |
| pull_request | opened | `pull_request_opened` |
| pull_request | edited | `pull_request_updated` |
| pull_request | closed (merged=false) | `pull_request_closed` |
| pull_request | closed (merged=true) | `pull_request_merged` |
| pull_request | reopened | `pull_request_opened` |
| pull_request_review | submitted | `pull_request_review` |
| pull_request_review_comment | created | `note_hook_pr` |

#### GitLab（待实现）

| GitLab object_kind | action | → Canonical Event Type |
|--------------------|--------|------------------------|
| issue | open | `issue_opened` |
| issue | close | `issue_closed` |
| issue | reopen | `issue_reopened` |
| issue | update + assignee | `issue_assigned` |
| issue | update + labels | `issue_labeled` |
| issue | update + title/description | `issue_updated` |
| note | — | `issue_comment` / `note_hook_pr` |
| merge_request | open | `pull_request_opened` |
| merge_request | update | `pull_request_updated` |
| merge_request | close | `pull_request_closed` |
| merge_request | merge | `pull_request_merged` |

### 3.3 VCS Backend 增强规范

每个 VCS backend 的 `normalize()` 方法必须：

1. **返回统一的 canonical event_type**（从 `event_types.py` 导入）
2. **在 payload 中保留原始 action**（`payload["_raw_action"]`）供规则引擎使用
3. **在 payload 中保留原始 changes**（`payload["_changes"]`）供 require_changes/block_changes 判定
4. **统一 changes 键名**：不同平台的 changes 字段名不同，归一化为统一键

```python
# 统一 changes 键名映射
CHANGES_KEY_MAP = {
    # GitCode → canonical
    "description": "description",
    "title": "title",
    "assignees": "assignees",
    "labels": "labels",
    # GitHub → canonical
    "body": "description",      # GitHub issue body = description
    "assignee": "assignees",    # GitHub 单数 assignee → 复数
    "label": "labels",          # GitHub 单数 label → 复数
    # GitLab → canonical
    "description": "description",
    "title": "title",
    "assignee": "assignees",
    "labels": "labels",
}
```

### 3.4 GitHub Backend 增强（`src/vcs/github.py`）

```python
class GitHubBackend(VcsBackend):
    # ... 现有代码 ...

    def _map_event_type(self, header: str, payload: dict) -> str:
        action = ""
        obj = payload.get("object_attributes") or payload
        if isinstance(obj, dict):
            action = (obj.get("action") or "").lower()

        if header == "issues":
            if action == "opened": return ISSUE_OPENED
            if action == "closed": return ISSUE_CLOSED
            if action == "reopened": return ISSUE_REOPENED
            if action == "assigned": return ISSUE_ASSIGNED
            if action == "labeled": return ISSUE_LABELED
            if action == "edited": return ISSUE_UPDATED
            return ISSUE_UPDATED
        if header == "issue_comment":
            return ISSUE_COMMENT
        if header == "pull_request":
            merged = payload.get("pull_request", {}).get("merged", False)
            if action == "closed" and merged: return PR_MERGED
            if action == "closed" and not merged: return PR_CLOSED
            if action == "opened" or action == "reopened": return PR_OPENED
            if action == "edited": return PR_UPDATED
            return PR_OPENED
        if header == "pull_request_review":
            return PR_REVIEW
        if header == "pull_request_review_comment":
            return PR_COMMENT
        # ... 其他 ...
```

### 3.5 GitLab Backend 新增（`src/vcs/gitlab.py`）

```python
class GitLabBackend(VcsBackend):
    name = "gitlab"
    webhook_path = "/webhook/gitlab"
    event_header_key = "X-Gitlab-Event"

    def normalize(self, raw, event_header) -> dict:
        payload = self.decode_payload(raw)
        object_kind = payload.get("object_kind", "")
        obj = payload.get("object_attributes", {})
        action = (obj.get("action") or "").lower() if isinstance(obj, dict) else ""

        evt_type = self._map_event_type(object_kind, action, payload)

        # GitLab changes 统一映射
        changes = payload.get("changes", {})
        normalized_changes = self._normalize_changes(changes)

        repo = self._extract_repo(payload)
        actor = self._extract_actor(payload)
        branch = self._extract_branch(payload)
        title = obj.get("title", "") if isinstance(obj, dict) else ""

        return {
            "source": "gitlab", "type": evt_type,
            "repository": repo, "branch": branch,
            "actor": actor, "payload": {**payload, "_raw_action": action, "_changes": normalized_changes},
            "title": title,
        }

    def _map_event_type(self, object_kind, action, payload):
        if object_kind == "issue":
            if action == "open": return ISSUE_OPENED
            if action == "close": return ISSUE_CLOSED
            if action == "reopen": return ISSUE_REOPENED
            if action == "update": return self._classify_issue_update(payload)
            return ISSUE_UPDATED
        if object_kind == "note":
            noteable_type = payload.get("object_attributes", {}).get("noteable_type", "")
            return PR_COMMENT if noteable_type == "MergeRequest" else ISSUE_COMMENT
        if object_kind == "merge_request":
            if action == "merge": return PR_MERGED
            if action == "open": return PR_OPENED
            if action == "update": return PR_UPDATED
            if action == "close": return PR_CLOSED
            return PR_OPENED
        if object_kind == "push": return PUSH
        if object_kind == "tag_push": return TAG_CREATED
        return object_kind

    def _classify_issue_update(self, payload):
        changes = payload.get("changes", {})
        if "assignee_id" in changes or "assignee_ids" in changes: return ISSUE_ASSIGNED
        if "labels" in changes: return ISSUE_LABELED
        return ISSUE_UPDATED
```

### 3.6 event_action_filter 规则扩展

现有规则按 `(event_type, action)` 匹配。增强为支持平台维度：

```sql
-- 新增 vcs_source 列（可选，NULL=所有平台）
ALTER TABLE event_action_filter ADD COLUMN vcs_source TEXT DEFAULT '';
-- '' = 所有平台，'gitcode'/'github'/'gitlab' = 指定平台
```

规则匹配优先级：
1. `(vcs_source=具体平台, event_type, action)` — 平台专属规则
2. `(vcs_source='', event_type, action)` — 通用规则（fallback）

```python
# event_rule.py 增强
def evaluate_event_action(event_type, payload, vcs_source="") -> dict:
    rules = load_event_action_rule()
    # 优先匹配平台专属规则
    evt_rules = rules.get((vcs_source, event_type)) or rules.get(("", event_type)) or {}
    # ... 后续判定逻辑不变 ...
```

### 3.7 repo_key 增强

```python
# src/repo_key.py
def to_repo_key(owner_repo: str, provider: str = "") -> str:
    """owner/repo → provider:owner/repo。无前缀时自动检测或默认 gitcode。"""
    if not owner_repo:
        return ""
    if ":" in owner_repo:
        return owner_repo
    if not provider:
        provider = "gitcode"  # 向后兼容
    return f"{provider}:{owner_repo}"
```

### 3.8 VCS API 客户端统一抽象

新建 `src/vcs/api_client.py`，统一各平台 REST API 调用：

```python
class VcsApiClient:
    """统一 VCS API 客户端：Issue 评论 / PR 操作 / 文件读写"""
    def __init__(self, provider, token, owner, repo):
        self.provider = provider
        self.token = token
        self.owner = owner
        self.repo = repo

    def post_issue_comment(self, issue_number, body) -> dict:
        if self.provider == "gitcode":
            return self._gitcode_comment(issue_number, body)
        elif self.provider == "github":
            return self._github_comment(issue_number, body)
        elif self.provider == "gitlab":
            return self._gitlab_note(issue_number, body)

    def get_issue(self, issue_number) -> dict:
        ...

    def create_pr(self, title, head, base, body) -> dict:
        ...

    def merge_pr(self, pr_number) -> dict:
        ...
```

### 3.9 adapter.py 注册扩展

```python
# src/vcs/adapter.py
from .gitcode import GitCodeBackend
from .github import GitHubBackend
from .gitlab import GitLabBackend  # 新增

_BACKENDS = {
    "gitcode": GitCodeBackend(),
    "github": GitHubBackend(),
    "gitlab": GitLabBackend(),   # 新增
}

_WEBHOOK_PATH_MAP = {
    "/webhook/gitcode": "gitcode",
    "/webhook/github": "github",
    "/webhook/gitlab": "gitlab",  # 新增
}
```

## 4. 实施计划

### Phase 1: 统一事件类型规范（1 day）
- 新建 `src/vcs/event_types.py`（canonical event types + SKIP/SIDE_EFFECT 集合）
- GitCode backend 归一化增强：Issue Hook action 细分为 issue_closed/reopened/assigned
- GitHub backend 归一化增强：issues/pull_request event action 细分

### Phase 2: event_rule 增强（0.5 day）
- event_action_filter 表新增 vcs_source 列
- event_rule.py 支持平台维度规则匹配（优先平台专属 → fallback 通用）
- 种子规则更新（GitCode + GitHub 各自的 action 映射）

### Phase 3: GitLab Backend（1 day）
- 新建 `src/vcs/gitlab.py`（object_kind + action 归一化）
- adapter.py 注册 GitLab
- webhook.py 路径映射

### Phase 4: VCS API 客户端统一（1 day）
- 新建 `src/vcs/api_client.py`（统一 Issue/PR REST API 调用）
- repogit.py 逐步迁移到 VcsApiClient

### Phase 5: 测试与验证（0.5 day）
- 各平台事件归一化测试
- event_rule 平台维度规则测试
- 端到端 webhook 模拟测试

## 5. 架构全景

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                   webhook.py (统一入口)                   │
                    │  /webhook/gitcode  → source=gitcode                      │
                    │  /webhook/github   → source=github                       │
                    │  /webhook/gitlab   → source=gitlab                       │
                    └──────────┬──────────────────────────────────────────────┘
                               │
                    ┌──────────▼──────────────────────────────────────────────┐
                    │              vcs/adapter.py                              │
                    │  normalize_event(raw, header, source)                   │
                    │  _BACKENDS = {gitcode, github, gitlab}                  │
                    └──────────┬──────────────────────────────────────────────┘
                               │
           ┌───────────────────┼───────────────────┬───────────────────┐
           ▼                   ▼                   ▼                   ▼
    vcs/gitcode.py       vcs/github.py       vcs/gitlab.py      vcs/event_types.py
    Issue Hook → action  issues → action     object_kind →      (canonical types)
    Note Hook → type     pull_request →      action             SKIP_EVENTS
    MR Hook → action     action              note → type         SIDE_EFFECT_EVENTS
           │                   │                   │               ROUTABLE_EVENTS
           └───────────────────┼───────────────────┘
                               ▼
                    ┌──────────────────────────────────────────────────────────┐
                    │              event_pipeline.run_pipeline                  │
                    │  1. actor 自循环检测                                      │
                    │  2. event_rule.evaluate_event_action(vcs_source)         │
                    │     → DB: event_action_filter (vcs_source, event_type,   │
                    │       action → allow/skip/side_effect)                   │
                    │  3. SKIP_EVENTS → return                                 │
                    │  4. SIDE_EFFECT_EVENTS → 副作用 + return                 │
                    │  5. ROUTABLE_EVENTS → _resolve_issue_stage_agent         │
                    └──────────────────────────────────────────────────────────┘
                               │
                    ┌──────────▼──────────────────────────────────────────────┐
                    │           vcs/api_client.py (统一 API 抽象)              │
                    │  post_issue_comment / get_issue / create_pr / merge_pr   │
                    │  gitcode / github / gitlab 各自实现                       │
                    └──────────────────────────────────────────────────────────┘
```

## 6. 兼容性

- **向后兼容**：现有 gitcode/github 规则不变（vcs_source='' 时 fallback 到通用规则）
- **渐进迁移**：repogit.py 逐步迁移到 VcsApiClient，不一次性重写
- **插件扩展**：`register_vcs_backend()` 已支持第三方注册，新平台可通过插件接入

## 7. 预期收益

| 维度 | 优化前 | 优化后 |
|------|--------|--------|
| 事件归一化 | GitCode 已细分，GitHub 未细分 | 所有平台统一 canonical event types |
| 过滤规则 | event_type+action 通用匹配 | 支持平台维度规则（专属优先 → 通用 fallback） |
| GitLab 支持 | 不支持 | 完整 webhook 事件归一化 |
| API 调用 | 分散在 repogit.py 等 | 统一 VcsApiClient 抽象 |
| 新平台接入 | 需改动多处 | 新建 backend + 注册即可 |
