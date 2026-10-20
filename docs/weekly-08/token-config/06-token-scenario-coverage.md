# 非 webhook 场景 Token 使用全覆盖

> 本文提取自设计文档第五章 R20.7-R20.11（第 516-632 行）。
> 覆盖 29 个 token 使用场景的统一改造、Chat/飞书 token 过期处理、非 webhook agent 自循环检测、Repo.bot_token 统一入口。

---

## 需求项

### R20.7 非 webhook 场景的 token 使用覆盖（R20.7.1-R20.7.15）

**问题**：Chat 会话、飞书 IM、events redispatch、onboarding、project_info 等场景也使用 token，不经过 TokenResolver/TokenActionHandler，不享受短路/过期检测。

15 个子项改造：

| 子项 | 场景 | 改造方案 |
|------|------|---------|
| R20.7.1 | Repo.bot_token | 改为调 service 层方法 `get_repo_bot_token()`（R20.11），内部调 `TokenResolver.get_token()`——所有调 Repo.bot_token 的场景自动继承短路/过期检测 |
| R20.7.2 | Chat `_resolve_gitcode_token` | 改为调 `TokenResolver.resolve()` + `Handler.get_comment_token()`——Chat agent 注入的 token 经过过期检测和级别感知 |
| R20.7.3 | 飞书 `sync_repo_feishu_mapping` | 改为调 `TokenResolver.get_token()`——sync mapping 享受短路（token 过期时跳过 sync 而非 401 报错）。**GitHub 侧也必须走 TokenResolver**（当前 `get_github_token` 未委托 TokenResolver，需改为 `Repo.bot_token` 或 `resolve_repo_token`） |
| R20.7.4 | 飞书 VOD handler | 改为调 `TokenResolver.resolve()`——VOD agent 注入的 token 经过级别感知 |
| R20.7.5 | events redispatch / complete | 改为调 `Repo.bot_token`（内部已委托 TokenResolver） |
| R20.7.6 | onboarding / project_info | 改为调 `Repo.bot_token`（内部已委托 TokenResolver）——git clone/push 在 token 过期时短路返回空 → clone 失败有明确日志 |
| R20.7.7 | scenes experience | 保持 `with_oauth_token`（用户主动操作，不走 repo token 链）——但 token 过期时返回友好提示 |
| R20.7.8 | Issues confirm_done close / comment | 改为调 `Repo.bot_token`（内部已委托 TokenResolver）——close/comment 享受短路，token 过期时返回 `bot_token_failed` 而非 401 |
| R20.7.9 | scheduler autopilot | 改为调 `Repo.bot_token`（内部已委托 TokenResolver）——autopilot agent 注入的 token 经过短路检测 |
| R20.7.10 | vcs/api_client `create_client` | 改为调 `Repo.bot_token`（内部已委托 TokenResolver）——VcsApiClient 享受短路 |
| R20.7.11 | vcs/providers bot_token | 改为调 `Repo.bot_token`（内部已委托 TokenResolver）——vcs 层 token 获取统一入口 |
| R20.7.12 | deps.py `get_bot_token` | 改为调 `Repo.bot_token`（内部已委托 TokenResolver）——FastAPI Depends 链路统一 |
| R20.7.13 | webhook SRE/PR `resolve_pr_issue_id` | 改为调 `Repo.bot_token`——token 过期时短路返回空 → resolve_pr_issue_id 失败 → 降级为 `resolve_issue_id`（不调 API）或 `evt.issue_id` |
| R20.7.14 | settings `repair_host` | 改为调 `Repo.bot_token`——repair 享受短路，token 过期时返回明确错误 |
| R20.7.15 | agents start/restart | 改为调 `Repo.bot_token`——写入 `session.repo_gitcode_token` 的 token 经过短路检测（过期 token 不写入 session） |

### R20.8 Chat 场景 token 过期处理

**问题**：Chat agent 用用户 token 执行，token 过期时 agent 401 失败，Chat 没有 fail_task 逻辑，用户只看到 agent 报错。

- **R20.8.1**：Chat agent 执行返回 401/auth 错误时 → 检测是否 token 过期（复用 R16.1 检测逻辑，调 `TokenResolver.detect_and_mark_expired`）→ 返回友好提示"Token 已过期，请在设置页重新绑定 Token 后重试"。**覆盖两个时机**：
  - **启动前检测**：token 获取返回空串时（已实现，`_resolve_gitcode_token` 内调 `detect_and_mark_expired`）
  - **执行后检测**：agent 返回 exit_code < 0 且 error 含 401/auth 关键词时（**遗漏项**，需在 `send_message` / `_enqueue_chat` 返回结果后检测）
- **R20.8.2**：Chat agent 执行返回 401 但 token 验证有效 → 返回"权限不足或 Token 权限范围不够"
- **R20.8.3**：Chat agent token 过期提示在 Web Chat 界面显示（不是 Issue 评论）
- **R20.8.4**：Chat agent token 过期时不重试（避免用过期 token 反复 401）
- **R20.8.5**（新增）：飞书 Chat handler_async 同理——agent 执行后 exit_code < 0 且 error 含 401 关键词时，调 `detect_and_mark_expired` 检测，飞书发卡片通知用户
- **R20.8.6**（新增）：Chat `send_message_stream`（SSE 流模式）agent 执行后 401 标记——agent_worker 的 done 事件含 exit_code != 0 + errors/stderr 含 401 关键词时，在 finally 块中调 `detect_and_mark_expired` 标记。**核心目的不是提示（SSE 流中 token 不出现），而是标记**：不标记会导致用户立即重发 send_stream → 启动前检测不命中（token 未被标记）→ agent 再次 401 → 死循环，直到健康检查或非 stream 模式标记。基于 done 事件的 errors/stderr 字段判断，不扫 final_text
- **R20.8.7**（新增）：Chat ChatTaskManager（新版 Chat 异步任务管理器）agent 执行后 401 标记——stream_chat_agent 的 done 事件 exit_code != 0 + errors/stderr 含 401 关键词时，在 finally 块中调 `detect_and_mark_expired` 标记。ChatTaskManager 替代旧版 _execute_chat_task，有 done 事件的 error 数据可用但未检测。不标记 → 死循环（同 R20.8.6 逻辑）

### R20.8a webhook pipeline direct 模式 401 标记（新增）

**问题**：webhook pipeline direct 模式（exec_agent）done 事件 exit_code != 0 时走 `update_task_status`（不经过 `fail_task`），没有 `detect_and_mark_expired` 调用。pipeline direct 模式的 agent 通常做 git clone/push（coding/sre agent），token 过期会导致 401。不标记 → 下次 pipeline 触发时 TokenResolver 不短路 → 死循环。

- **R20.8a.1**：exec_agent done 事件 exit_code != 0 时，基于 done 事件的 errors/stderr 字段判断是否含 401 关键词 → 调 `detect_and_mark_expired` 标记。queue 模式的标记由 fail_task 覆盖（worker 侧），direct 模式不经过 fail_task → 必须在此补充标记

### R20.8b event dialogue direct 模式 401 标记（新增）

**问题**：event dialogue direct 模式（stream_devops_agent）的 finally 块写入 DB，没有 `detect_and_mark_expired` 调用。event dialogue agent 可能调 VCS API（读 issue 评论等），token 过期会 401。不标记 → 死循环。

- **R20.8b.1**：event dialogue direct 模式 stream_devops_agent done 事件 exit_code != 0 + errors/stderr 含 401 关键词时，在 finally 块中调 `detect_and_mark_expired` 标记。queue 模式的标记由 fail_task 覆盖（worker 侧），direct 模式不经过 fail_task → 必须在此补充标记

### R20.9 飞书场景 token 过期处理

**问题**：飞书 sync mapping / VOD agent 用用户 token，token 过期时操作静默失败。

- **R20.9.1**：飞书 `sync_repo_feishu_mapping` token 过期时（`get_repo_token_with_refresh` 返回空或 401）→ 跳过 sync + 飞书群发通知卡片"仓库 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"
- **R20.9.2**：飞书 VOD agent token 过期时 → VOD agent 返回提示"Token 已过期，请在设置页重新绑定"→ 飞书发卡片通知用户
- **R20.9.3**：飞书场景 token 过期通知通过飞书消息卡片（不是 Issue 评论），因为飞书是独立通知渠道
- **R20.9.4**：飞书 VOD agent token 过期不重试
- **R20.9.5**（新增）：飞书 VOD handler direct 模式 agent 执行后 401 标记——agent 执行返回 exit_code != 0 且 final_text 含 401 关键词时，调 `detect_and_mark_expired` 标记 token 过期。**核心目的不是提示，而是标记**：不标记会导致下次 VOD agent 启动时 TokenResolver 不短路 → 继续用过期 token → 401 → 不标记 → 死循环，直到健康检查或用户手动重新绑定。queue 模式的标记由 fail_task 覆盖（worker 侧），direct 模式不经过 fail_task → 必须在此补充标记

### R20.10 非 webhook agent 执行的自循环检测覆盖

**问题**：飞书 VOD agent、Chat direct 模式、scheduler autopilot 不走 agent_tasks 表 → 自循环检测条件 4（check_running_task）查不到 → agent 评论触发重复执行。

- **R20.10.1**：飞书 VOD agent 执行时在 agent_tasks 表创建 task 行（status=running）→ 自循环检测条件 4 可查到
- **R20.10.2**：Chat direct 模式执行时在 agent_tasks 表创建 task 行（status=running）
- **R20.10.3**：scheduler autopilot 执行时在 agent_tasks 表创建 task 行（status=running）（P0 修复）
- **R20.10.4**：非 webhook agent 执行完成后更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发
- **R20.10.5**：条件 4 补充查询——除 `agent_tasks.status=running/dispatched` 外，也查 `devops_agent_session.last_active_at > now-300s AND status=active`（覆盖非 webhook agent 执行场景）
- **R20.10.6**：Chat worker 模式（`_enqueue_chat`）低风险——Chat agent 通常不调 VCS API，如 skill 有 VCS 操作则走 R20.10.2 创建 task 行

### R20.11 Repo.bot_token 统一入口

**问题**：Repo.bot_token 是所有非 pipeline 场景的 token 入口，但未纳入 TokenResolver 体系。

- **R20.11.1**：Repo.bot_token 改为调 service 层方法 `get_repo_bot_token(repo_key)`（返回 str，签名不变）
- **R20.11.2**：所有调 Repo.bot_token 的场景（chat/飞书/events/onboarding/project_info）自动继承短路/过期检测，无需改动调用方
- **R20.11.3**：Repo.bot_token 返回空串时（token 过期短路）→ 调用方应处理空 token 场景（git clone 失败有明确日志，agent 注入空 token 有 warning）
- **R20.11.4**：需要级别信息的调用方（Chat `_resolve_gitcode_token` / 飞书 VOD handler）改为调 `TokenResolver.resolve()` 获取 TokenResult + Handler
- **R20.11.5**：Repo 是数据模型（domain object），不在属性内部直接 import TokenResolver——避免数据模型反向依赖 resolver（TokenResolver 内部策略类可能反向引用 Repo 查 `host_user_id`，形成循环依赖）。改为 service 层方法注入（`src/token_service.py`）

---

## 设计方案

### 29 场景全览表

| # | 场景 | token 获取方式 | 改造方案 | 状态 |
|---|------|--------------|---------|------|
| 1 | webhook pipeline exec_agent | `get_repo_token_with_refresh` | → TokenResolver | 已覆盖 |
| 2 | webhook `_post_result_to_issue` | `repo_gitcode_token` | → Handler | 已覆盖 |
| 3 | webhook `_post_result_to_pr` | `repo_gitcode_token` | → Handler | 已覆盖 |
| 4 | webhook `create_pr_for_repo` | `repo_gitcode_token` | → TokenResolver | 已覆盖 |
| 5 | webhook SRE `resolve_pr_issue_id` | `get_repo_token_with_refresh` | → Repo.bot_token（R20.7.13） | 已覆盖 |
| 6 | webhook PR `resolve_pr_issue_id` | `get_repo_token_with_refresh` | → Repo.bot_token（R20.7.13） | 已覆盖 |
| 7 | webhook project_info builder | `Repo.bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 8 | webhook fail_task | `comment_token` | → Handler | 已覆盖 |
| 9 | Chat `send_message` | `_resolve_gitcode_token` | → resolve()+Handler（R20.7.2） | 已覆盖 |
| 10 | Chat `ensure_issue_conversation` | `Repo(rk).bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 11 | Chat `list_messages` | `Repo(_rk).bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 12 | Chat `_enqueue_chat` | `gitcode_token` | → resolve() | 已覆盖 |
| 13 | 飞书 `sync_repo_feishu_mapping` | `get_repo_token_with_refresh` | → TokenResolver（R20.7.3） | **GitCode 已覆盖，GitHub 遗漏**（get_github_token 未走 TokenResolver） |
| 14 | 飞书 VOD handler | `agent_session.repo_gitcode_token` → `Repo.bot_token` | → resolve()（R20.7.4） | 已覆盖 |
| 15 | Issues `confirm_done` close | `get_repo_bot_token` / `get_github_token` → `with_oauth_token` | → Repo.bot_token（R20.7.8） | 已覆盖 |
| 16 | Issues comment | `get_github_token` / `get_repo_bot_token` | → Repo.bot_token（R20.7.8） | 已覆盖 |
| 17 | agents start/restart | `get_repo_token_with_refresh` | → Repo.bot_token（R20.7.15） | 已覆盖 |
| 18 | settings `repair_host` | `get_repo_token_with_refresh` | → Repo.bot_token（R20.7.14） | 已覆盖 |
| 19 | settings verify token | `_req` / `_github_req` → `GET /user` | → provider.verify_token | 已覆盖 |
| 20 | settings update_credentials | 用户 token 绑定/验证 | 独立链路 | 无问题 |
| 21 | events redispatch | `Repo.bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 22 | events complete | `Repo.bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 23 | scheduler autopilot | `get_github_token` / `get_gitcode_token` | → Repo.bot_token（R20.7.9） | 已覆盖 |
| 24 | onboarding | `Repo.bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 25 | project_info builder | `Repo.bot_token` | → TokenResolver（R20.11） | 已覆盖 |
| 26 | templates read | `get_global_*_bot_token` | 独立（只读全局） | 无问题 |
| 27 | vcs/api_client `create_client` | `get_gitcode_token` | → Repo.bot_token（R20.7.10） | 已覆盖 |
| 28 | vcs/providers bot_token | `get_gitcode_token` / `get_github_token` | → Repo.bot_token（R20.7.11） | 已覆盖 |
| 29 | vcs/providers user_token | `get_user_token_with_refresh` | 不替代（R18.4.4） | 无问题 |
| 30 | 飞书 Chat handler_async direct | `user_token=""`（始终空） | → `get_user_token_with_refresh`（R20.28.1） | **遗漏**（user_token 始终为空） |
| 31 | 飞书 VOD handler queue 模式 | `enqueue_task` 未传 user_token | → `enqueue_task` 传 `user_gitcode_token`（R20.28.4） | **遗漏**（queue 模式缺 user_token） |
| 32 | Chat agent 执行后 401 检测 | 无 `detect_and_mark_expired` | → 执行后 401 检测（R20.8.1 执行后时机） | **遗漏**（仅启动前检测） |
| 33 | 飞书 Chat agent 执行后 401 检测 | 无 `detect_and_mark_expired` | → 执行后 401 检测（R20.8.5） | **遗漏** |
| 38 | 飞书 VOD handler direct 执行后 401 标记 | 无 `detect_and_mark_expired` | → direct 模式 401 标记（R20.9.5） | **遗漏**（direct 不走 fail_task，标记缺失导致死循环） |
| 39 | Chat _execute_chat_task direct 执行后 401 标记 | 无 `detect_and_mark_expired` | → direct 模式 401 标记（R20.8.1 执行后时机） | **遗漏**（旧版异步模式，但仍在使用） |
| 40 | Chat send_stream agent_worker 执行后 401 标记 | 无 `detect_and_mark_expired` | → finally 块 401 标记（R20.8.6） | **遗漏**（SSE 流模式不标记 → 重发死循环） |
| 41 | Chat ChatTaskManager 执行后 401 标记 | 无 `detect_and_mark_expired` | → finally 块 401 标记（R20.8.7） | **遗漏**（新版 Chat 异步任务管理器不标记 → 死循环） |
| 42 | webhook pipeline direct 执行后 401 标记 | 无 `detect_and_mark_expired` | → exec_agent done 后 401 标记（R20.8a.1） | **遗漏**（direct 不走 fail_task → 死循环） |
| 43 | event dialogue direct 执行后 401 标记 | 无 `detect_and_mark_expired` | → finally 块 401 标记（R20.8b.1） | **遗漏**（direct 不走 fail_task → 死循环） |

> scenes experience（`with_oauth_token`）归入 user × obtain，保持独立链路（R20.7.7）。

### 二维矩阵：来源 × 消费方式

同一格的场景共享相同的来源和消费方式，可以统一到同一个方法；跨格的场景不能合并——消费方式不同（obtain 需要有效性+刷新，peek 只需要原始值）。

| 来源 \ 消费 | obtain（保证有效） | peek（原始值比对） |
|------------|------------------|-------------------|
| **bot** | 执行 token：pipeline exec_agent / project_info / onboarding / events redispatch / autopilot / Issues close&comment / list_repo_branches / vcs/api_client / vcs/providers / deps / Repo.bot_token | A2A 鉴权：`get_repo_bot_token_no_global` / 关联仓库信任域比对 |
| **user** | 身份 token：建单（issues_center create_issue）/ Chat（`_resolve_gitcode_token`）/ scenes experience / templates verify | — |
| **config** | — | webhook 验证（`get_webhook_verify_token`）→ **移出 token 体系** |

矩阵使用规则：
- **bot × obtain** 是最大的一格（约 15 个场景），全部统一到 `TokenResolver.get_token()`
- **bot × peek** 只有 A2A 鉴权，用 `TokenResolver.read_stored_token()`
- **user × obtain** 是用户身份场景，用 `get_user_token_with_refresh()`（独立链路）
- **config × peek** 是 webhook 验证，不属于 token 体系

### 不属于 token 体系的 3 个场景

以下场景概念上不属于 token 体系，保留在此说明边界：

1. **webhook 验证**（`get_webhook_verify_token`）：语义是"secret 相等吗"的配置项比对，不是"获取可用 token"。查的是 `webhook_secret` / `webhook_token_field`（webhook 创建时的 password），不在 token 策略链中。放在 config/secret 模块更合适。保留在文档中是因为实现上与 token 存储有交集（`webhook_token_field` 列在 managed_repos 表中），但概念上不属于 token 体系。

2. **settings verify token**（`_req` / `_github_req` → `GET /user`）：语义是"调 VCS API 做验证动作"，是动作不是 token 获取。验证结果决定是否写入 token，但验证本身不是 token 消费。

3. **scenes experience**（`with_oauth_token`）：语义是"用户主动操作时用用户自己的 token"，走 `with_oauth_token` 独立链路（含 OAuth 预刷新 + 401 重试）。与 user × obtain 的其他场景（建单/Chat）语义相同，但实现链路不同（`with_oauth_token` 是封装层，不走 TokenResolver）。

### R20.8 Chat 检测 401 → 友好提示

Chat `send_message` 内部，agent 执行结果包含 401/auth 错误时：

1. 调 `TokenResolver.detect_and_mark_expired(repo_key)` 获取 `error_code`
2. 如果 `error_code` 非空 → token 过期 → 返回友好提示"Token 已过期，请在设置页重新绑定 Token 后重试"
3. 如果 `error_code` 为空（token 验证有效）→ 返回"权限不足或 Token 权限范围不够"
4. token 过期不重试（避免反复 401）
5. 提示在 Web Chat 界面显示（不是 Issue 评论）

### R20.9 飞书 sync 跳过 + 卡片通知 + VOD 不启动

- `sync_repo_feishu_mapping` 调 `TokenResolver.get_token()` 返回空时，跳过 sync 并调飞书 `send_card` 通知群成员"仓库 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"
- VOD handler 调 `TokenResolver.resolve()` 返回空 token 时，VOD agent 不启动，调飞书 `send_card` 通知用户"Token 已过期，请在设置页重新绑定"
- 飞书场景 token 过期通知通过飞书消息卡片（不是 Issue 评论），因为飞书是独立通知渠道
- VOD agent token 过期不重试

### R20.10 创建 task 行

非 webhook agent 执行时在 `agent_tasks` 表创建 task 行，使自循环检测条件 4（`check_running_task`）可查到：

- 飞书 VOD handler 启动 agent 前 `INSERT agent_tasks (repo_key, issue_id, status='running')`，agent 完成后 `UPDATE status='completed'`
- Chat direct 模式同理
- autopilot 同理（P0 修复）
- `check_running_task` 两级查询覆盖 `agent_tasks` 和 `devops_agent_session`：
  - 第一级：`agent_tasks.status = running/dispatched`
  - 第二级（R20.10.5 补充）：`devops_agent_session.last_active_at > now-300s AND status=active`
- Chat worker 模式（`_enqueue_chat`）低风险——Chat agent 通常不调 VCS API，如 skill 有 VCS 操作则走 R20.10.2 创建 task 行

### R20.11 Repo.bot_token → service 层 → TokenResolver 代码示例

Repo 保持数据模型纯净，不持有 resolver 引用。token 获取逻辑放 service 层，避免数据模型 → resolver → 策略类 → Repo 循环依赖：

```python
# src/repo.py — 数据模型，不 import TokenResolver
class Repo:
    """仓库领域对象。只封装仓库元数据（owner/name/provider/api_base/clone_url）。
    不持有 token resolver 引用——避免数据模型 → resolver → 策略类 → Repo 循环依赖。"""
    @property
    def bot_token(self) -> str:
        """委托 service 层获取 token。签名不变（str → str），向后兼容。"""
        from src.token_service import get_repo_bot_token  # service 层方法
        return get_repo_bot_token(self.repo_key)

# src/token_service.py — service 层，桥接 Repo 和 TokenResolver
def get_repo_bot_token(repo_key: str) -> str:
    """获取仓库可用执行 token。供 Repo.bot_token 和其他调用方使用。"""
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    return TokenResolver(provider).get_token(repo_key)
```

调用方无需改动——`Repo.bot_token` 签名不变（str → str），内部从直接查 DB 改为委托 service 层 → TokenResolver。

需要级别信息的调用方（Chat `_resolve_gitcode_token` / 飞书 VOD handler）改为直接调 `TokenResolver.resolve()` 获取 `TokenResult` + Handler（R20.11.4）。

---

## 涉及代码文件

| 文件 | 改造内容 | 对应需求 |
|------|---------|---------|
| `src/repo.py` | `Repo.bot_token` 属性改为委托 service 层 `get_repo_bot_token()`，不直接 import TokenResolver | R20.11.1 / R20.11.5 |
| `src/token_service.py`（**新增**） | service 层方法 `get_repo_bot_token(repo_key)`，桥接 Repo 和 TokenResolver | R20.11.1 / R20.11.5 |
| `src/api/routes/chat.py` | `_resolve_gitcode_token` → `TokenResolver.resolve()` + Handler；agent 401 → `detect_and_mark_expired` → 友好提示；direct 模式创建 task 行 | R20.7.2 / R20.8.1-R20.8.4 / R20.10.2 |
| `src/im/feishu_async.py` | `sync_repo_feishu_mapping` → `TokenResolver.get_token()`；token 过期跳过 sync + 卡片通知 | R20.7.3 / R20.9.1 / R20.9.3 |
| `src/im/vod_handler_async.py` | VOD handler → `TokenResolver.resolve()`；token 过期 VOD 不启动 + 卡片通知；启动前创建 task 行 | R20.7.4 / R20.9.2 / R20.9.4 / R20.10.1 |
| `src/api/routes/events.py` | redispatch / complete → `Repo.bot_token`（内部已委托 TokenResolver） | R20.7.5 / R20.11.2 |
| `src/api/onboarding.py` | onboarding → `Repo.bot_token`（内部已委托 TokenResolver） | R20.7.6 / R20.11.2 |
| `src/api/routes/autopilot.py` | scheduler autopilot → `Repo.bot_token`；创建 task 行 | R20.7.9 / R20.10.3 |
| `src/vcs/api_client.py` | `create_client` → `Repo.bot_token`（内部已委托 TokenResolver） | R20.7.10 |
| `src/vcs/providers/` | bot_token → `Repo.bot_token`（内部已委托 TokenResolver）；user_token 保持 `get_user_token_with_refresh` 不替代 | R20.7.11 |

> 间接覆盖（通过 Repo.bot_token 统一入口自动继承）：Issues close/comment（R20.7.8）、deps.py get_bot_token（R20.7.12）、webhook SRE/PR resolve_pr_issue_id（R20.7.13）、settings repair_host（R20.7.14）、agents start/restart（R20.7.15）。

---

## 验收标准

### R20.7 场景覆盖验收

- [ ] 29 场景全览表中所有"已覆盖"场景的 token 获取方式已改为 TokenResolver / Repo.bot_token / Handler
- [ ] **场景 13**：飞书 sync GitHub 侧也走 TokenResolver（当前遗漏，get_github_token → Repo.bot_token）
- [ ] bot × obtain 格约 15 个场景全部统一到 `TokenResolver.get_token()`（经 Repo.bot_token 或直接调 resolve）
- [ ] bot × peek（A2A 鉴权）使用 `TokenResolver.read_stored_token()`
- [ ] user × obtain 场景（建单/Chat/scenes）走 `get_user_token_with_refresh()` 独立链路
- [ ] 3 个不属于 token 体系的场景（webhook 验证 / settings verify / scenes experience）保持独立，不走 TokenResolver
- [ ] Repo.bot_token 返回空串时调用方有明确处理（git clone 失败有日志，agent 注入空 token 有 warning）
- [ ] 需要级别信息的调用方（Chat / 飞书 VOD）改为调 `TokenResolver.resolve()` 获取 TokenResult + Handler

### R20.8 Chat 过期验收

- [ ] Chat agent 执行返回 401 时调 `TokenResolver.detect_and_mark_expired` 检测 token 过期
- [ ] **启动前检测**：token 获取返回空串时检测（已实现）
- [ ] **执行后检测**：agent 返回 exit_code < 0 且 error 含 401 关键词时检测（**遗漏，需补充**）
- [ ] token 过期 → Web Chat 界面显示"Token 已过期，请在设置页重新绑定 Token 后重试"
- [ ] token 有效但 401 → 显示"权限不足或 Token 权限范围不够"
- [ ] token 过期时不重试
- [ ] **飞书 Chat handler_async 同理**（R20.8.5）：agent 执行后 401 → detect_and_mark_expired → 飞书卡片通知（**遗漏，需补充**）
- [ ] **Chat send_stream SSE 流模式**（R20.8.6）：agent_worker done 事件 exit_code != 0 + errors/stderr 含 401 关键词 → finally 块调 detect_and_mark_expired 标记（**遗漏，需补充**——不标记导致重发死循环）
- [ ] **Chat ChatTaskManager**（R20.8.7）：stream_chat_agent done 事件 exit_code != 0 + errors/stderr 含 401 关键词 → finally 块调 detect_and_mark_expired 标记（**遗漏，需补充**）
- [ ] **webhook pipeline direct**（R20.8a.1）：exec_agent done 事件 exit_code != 0 + errors/stderr 含 401 关键词 → 调 detect_and_mark_expired 标记（**遗漏，需补充**——direct 不走 fail_task）
- [ ] **event dialogue direct**（R20.8b.1）：stream_devops_agent done 事件 exit_code != 0 + errors/stderr 含 401 关键词 → finally 块调 detect_and_mark_expired 标记（**遗漏，需补充**）

### R20.9 飞书过期验收

- [ ] `sync_repo_feishu_mapping` token 过期时跳过 sync + 飞书群发通知卡片
- [ ] VOD handler token 过期时 VOD agent 不启动 + 飞书发卡片通知用户
- [ ] 飞书通知通过消息卡片（不是 Issue 评论）
- [ ] VOD agent token 过期不重试
- [ ] **VOD handler direct 模式 agent 执行后 401 标记**（R20.9.5）：exit_code != 0 + final_text 含 401 关键词 → 调 `detect_and_mark_expired` 标记（**遗漏，需补充**——不标记会导致 token 过期死循环）

### R20.10 自循环检测验收

- [ ] 飞书 VOD agent 执行时在 agent_tasks 表创建 task 行（status=running），完成后更新为 completed
- [ ] Chat direct 模式执行时创建 task 行
- [ ] scheduler autopilot 执行时创建 task 行（P0）
- [ ] `check_running_task` 两级查询覆盖 agent_tasks 和 devops_agent_session
- [ ] 非 webhook agent 完成后 task status=completed → 用户后续评论可正常触发

### R20.11 统一入口验收

- [ ] `Repo.bot_token` 改为调 `src/token_service.py` 的 `get_repo_bot_token()`
- [ ] `Repo.bot_token` 签名不变（str → str），所有调用方无需改动
- [ ] `src/repo.py` 不 import TokenResolver（避免循环依赖）
- [ ] `src/token_service.py` service 层方法桥接 Repo 和 TokenResolver
- [ ] 所有调 Repo.bot_token 的场景自动继承短路/过期检测

---

## 依赖场景

**依赖 04-token-resolver**（策略模式 + TokenActionHandler 职责链 + VCSProvider 接口 + 二维矩阵）。

本文档的所有改造方案都建立在 04-token-resolver 定义的基础设施之上：

- `TokenResolver.get_token()` / `resolve()` / `read_stored_token()` / `detect_and_mark_expired()` — 所有场景改造的统一入口
- `TokenActionHandler` 职责链 — Chat / 飞书 VOD 等 resolve() 场景使用的 Handler（`get_comment_token` 等）
- `VCSProvider` 接口 — service 层 `create_provider()` 依赖的 provider 工厂
- 二维矩阵分类模型 — 本文 29 场景全览表和 bot/user/config 分类的基础

下游依赖本文档的模块：
- **05-path-b-self-loop**：自循环检测依赖 R20.10 创建的 task 行 + `Handler.is_self_loop_actor`
- **07-token-injection**：token 注入链路依赖 task 行 token + `env_extra`，R20.10.5 的 session 兜底查询
- **08-token-security**：关联仓库 token 独立获取依赖 `Repo.bot_token` 统一入口
