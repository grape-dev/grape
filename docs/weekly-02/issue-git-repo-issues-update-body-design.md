# git-repo `issues update` 支持修改 Issue 正文（body）——平台侧实现方案

> 需求来源：grape-cli 仓库 `docs/grape-server-requirement-issues-update-body.md`
> 目标仓库：grape 主仓库（`src/api/git_repo_api.py` / OpenAPI 契约）
> 优先级：P1（能力补齐；消除 agent「200 空转 + 无限重试」）
> 日期：2026-09-11

## 一、问题背景

多个 VOD/Issue Agent 会话反复执行 `grape git-repo issues update --json '{"body":"<需求全文>"}'`
期望写入 Issue 正文，但**永远不生效**：

| 会话 | 时间 | 现象 |
|------|------|------|
| VOD（Issue #7） | 15:48 | 同一 update 重试 ~6 次 + 绕过 patch，最终放弃改正文转写评论 |
| Issue（Issue #7） | 15:52 | 改用评论承载（正确绕过） |
| VOD（Issue #8） | 17:27 | 再次 update，指数退避重试并用 `issues list` 验证"没变更"后继续重试 |

**根因**：`PATCH /api/repos/{owner}/{repo}/issues/{number}`（`gitrepo_issues_update`）
请求体 `IssueUpdateBody` **只有 `labels / assignee / milestone_id / state`，没有 `body`**。
服务端收到未知字段 `body` 直接忽略并返回 **200** —— 静默空转、无报错、无变更，
agent 只能靠"验证是否变更"发现失败，陷入无限重试。

## 二、现状核对（已读源码）

### 2.1 请求体模型（`src/api/git_repo_api.py`）

```python
class PRUpdateBody(BaseModel):        # PR 更新：已支持 body ✓
    title: str = ""
    body: str = ""
    labels: str = ""

class IssueCreateBody(BaseModel):     # Issue 创建：已支持 body ✓
    title: str
    body: str = ""
    assignee: str = ""

class IssueUpdateBody(BaseModel):     # Issue 更新：缺 body ✗
    labels: str = ""
    assignee: str = ""
    milestone_id: int = 0
    state: str = ""
```

### 2.2 路由实现（`update_issue`，L275-283）

```python
@router.patch("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_update", summary="更新 Issue")
def update_issue(owner, repo, number, body: IssueUpdateBody, user=..., p=...):
    payload = {k: v for k, v in {
        "labels": body.labels, "assignee": body.assignee,
        "milestone_id": body.milestone_id or None, "state": body.state,
    }.items() if v not in (None, "", 0)}
    return _gitcode(p, "PATCH", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
```

- 创建 Issue（`create_issue`）用 `body` 字段透传（GitCode 平台认 `body`）。
- `_gitcode` 直接透传 body 到平台（GitCode/GitHub 分支）。

## 三、实现方案（选项 A：IssueUpdateBody 加 body + 透传）

> 选选项 A（改现有端点），不用选项 B（新建独立端点）：字段对齐、改动最小、
> 与 PRUpdateBody 的 body 能力一致。

### 3.1 请求体添加 `body` 字段

```python
class IssueUpdateBody(BaseModel):
    labels: str = ""
    assignee: str = ""
    milestone_id: int = 0
    state: str = ""
    body: str = ""          # 新增：Issue 正文（与 IssueCreateBody/PRUpdateBody 对齐）
```

### 3.2 路由透传 body

```python
def update_issue(owner, repo, number, body: IssueUpdateBody, user=..., p=...):
    payload = {k: v for k, v in {
        "labels": body.labels, "assignee": body.assignee,
        "milestone_id": body.milestone_id or None, "state": body.state,
        "body": body.body,                 # 新增；空值会被下方过滤掉
    }.items() if v not in (None, "", 0)}
    return _gitcode(p, "PATCH", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
```

> 现有 `if v not in (None, "", 0)` 过滤已天然跳过空 `body`，不影响 `labels/assignee/milestone_id/state`。

### 3.3 平台兼容

- **GitCode**：`PATCH /api/v5/repos/{o}/{r}/issues/{n}` 接受 `body` 字段（与创建一致）。
- **GitHub**：`PATCH /repos/{o}/{r}/issues/{n}` 接受 `body` 字段（官方支持）。

## 四、改动与测试清单

| # | 项 | 说明 | 优先级 |
|---|----|------|--------|
| 1 | `git_repo_api.py` `IssueUpdateBody` | 加 `body` 字段 | 必须 |
| 2 | `git_repo_api.py` `update_issue` | payload 加 body 透传 | 必须 |
| 3 | `tests/test_git_repo_api.py` | 断言：body 更新后 `issues get` 可读新正文；labels/assignee/milestone_id/state 不回归 | 必须 |
| 4 | `help/api/10-git-repos.md` | PATCH `issues/{number}` 入参 Body 表补充 `body` 字段说明（同步刷新 help 下 api 文档） | 必须 |
| 5 | OpenAPI 契约 | `openapi.json` 的 `IssueUpdateBody` 自动带 body（FastAPI 自动生成，无需手改） | 自动 |
| 6 | CLI 侧 | `Body fields` 帮助自动更新（CLI 从契约生成，无需人工改） | 自动 |

## 五、验收标准

- [ ] `grape git-repo issues update --owner o --repo r --number 7 --json '{"body":"新正文"}'` 后 `issues get` 读到新正文
- [ ] `labels / assignee / milestone_id / state` 更新不回归
- [ ] 契约 `openapi.json` 的 `IssueUpdateBody` 出现 `body` 字段
- [ ] CLI 侧 `Body fields: assignee, body, labels, milestone_id, state`（或按契约顺序）自动更新
- [ ] **`help/api/10-git-repos.md` 的 PATCH `issues/{number}` Body 表含 `body` 字段**（help 文档同步刷新）
- [ ] 不破坏现有调用（空 body 时行为等同现状）

## 六、影响评估

| 项 | 说明 |
|----|------|
| Agent 体验 | 消除"200 空转 + 无限重试"，VOD/Issue Agent 可直写 Issue 正文 |
| 契约/帮助 | `Body fields` 自动带 body，agent 一眼可见可选项 |
| 兼容性 | 仅新增字段，存量调用不受影响 |
| 工作量 | 小：1 处模型 + 1 处路由 + 测试 |

## 七、关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/git_repo_api.py:50` | `IssueUpdateBody`（加 body） |
| `src/api/git_repo_api.py:275-283` | `update_issue`（payload 加 body 透传） |
| `src/api/git_repo_api.py:44-47` | `IssueCreateBody`（参照：已有 body） |
| `src/api/git_repo_api.py:38-41` | `PRUpdateBody`（参照：已有 body） |
| `tests/test_git_repo_api.py` | 现有 git repo API 测试（补 body 用例） |