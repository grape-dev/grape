# 方案：飞书任务会话 direct 模式注入 GRAPE_SESSION_TOKEN（Issue #6 澄清回写失败根因修复）

> 状态：**✅ 已实施**（2026-10-18；代码 `src/api/chat_session_env.py` + `src/im/handler_async.py` + `src/api/routes/chat.py`；
> 测试 `tests/test_chat_session_env.py` + `tests/test_feishu_task_chat_session_token.py` 通过，相关回归 36 passed）｜
> 目标分支：weekly-07-lyz（生产部署分支）｜关联：Issue-45 M2 会话 token 机制、weekly-06 chat-direct-session-token-injection-design（Web 链路已修）
>
> 动机：飞书任务会话（`conv_type=task`，绑定真实仓库）里 Chat Agent 在 **direct（本机直连）执行模式**下回复
> 「当前运行环境缺少会话凭据 `GRAPE_SESSION_TOKEN`，`grape` CLI 无法认证执行评论写入」，导致澄清结论无法回写到
> Issue #6。根因是 **飞书 IM 的 direct 执行链路漏掉了 `GRAPE_SESSION_TOKEN` 注入**（queue/远端 worker、pipeline 任务、
> Web chat direct 三处均正常）。本方案为 weekly-06 同名修复的**飞书侧补齐**。

---

## 一、问题现象（生产现场）

- **对话**：`chat_conversations.id=<conv-id>`，`conv_type=task`，
  `repo_key=gitcode:<user>/hello-calculator`，issue #6，飞书群 `oc_<REDACTED>`。
- **时序**（`chat_messages`，UTC）：
  1. `2026-10-18 11:02:34`（飞书 19:02:34 CST）用户：`1.  sqrt；2. 新增一元函数；`
  2. `2026-10-18 11:03:58` agent `chat_messages.id=1833`：
     > 不过回帖到 [Issue #6](https://gitcode.com/<user>/hello-calculator/issues/6) 时失败：当前运行环境缺少会话凭据
     > `GRAPE_SESSION_TOKEN`，`grape` CLI 无法认证执行评论写入。按安全规范我不会去读配置/日志寻找或尝试其它 token。
- **服务端日志**（`journalctl -u grape`）佐证执行路径：
  ```
  Oct 18 19:02:05 [task-open] cmd=/task-open … conv_id=<conv-id>… rk=gitcode:…/hello-<app> issue=6
  Oct 18 19:02:35 [Agent:chat|repo:hello-<app>] opencode cwd: …/chat/2/<conv-id>…
  ```
  `[Agent:chat|repo:…]` 是 `src/im/handler_async.py` 直达分支的 `agent_label` 特征（Web `chat.py` 为
  `[Agent:chat|user:…]`）→ **确认走的是飞书 IM 的 direct（本机直连）分支**，不是 Web `/chat/send*`。

> 注：该问题为**长期缺口**——`chat_messages.id=1698`（2026-10-11）同文案再次出现，说明 weekly-06 的修复只覆盖了
> Web 三条路由，未覆盖飞书 IM 链路。

---

## 二、根因分析

### 2.1 注入点现状（矩阵，对齐 weekly-06 口径）

`GRAPE_SESSION_TOKEN`（`tk_`，`auth_token.py`，绑定 `(repo_key, agent_type[, user_id])`）的签发注入点共 4 处：

| 执行路径 | 位置 | 注入 `GRAPE_SESSION_TOKEN` |
|----------|------|---------------------------|
| 队列入队 `enqueue_task` | `src/runtime/service.py:802-814` | ✅（含 chat，用户/远端 worker） |
| devops 事件 direct `_execute_agent_inner` | `src/api/event_pipeline.py:3212-3223` | ✅（事件任务 agent） |
| **Web chat direct** `stream_chat_agent` | `src/api/routes/chat.py` `/chat/send`、`/chat/send/stream`、`/chat/submit`（`_chat_session_env_extra`） | ✅（weekly-06 已修） |
| **飞书 IM direct** `consume_stream_async` | `src/im/handler_async.py`（`_process_chat_inner_async` 直达分支） | ❌ **缺口：未传 `env_extra`** |

### 2.2 飞书直达分支代码现状（部署中 HEAD 已核实）

```python
# src/im/handler_async.py  _process_chat_inner_async  direct 分支
async for event_type, data in consume_stream_async(prompt, wsd, oc_sid, "chat",
                    backend_name=backend, account=exec_account,
                    user_token=user_token, user_id=user_id,
                    agent_name="Chat Agent",
                    agent_label=f"[Agent:chat|repo:{repository}]"):   # ← 无 env_extra
```

- `consume_stream_async` 签名**支持** `env_extra`（`src/agent/executor.py:1146`），且会 `env.update(env_extra)`（`executor.py:582-583`）——
  只差调用点没传。
- 现有实际注入的 `env_extra` 只是独立 `{"IM_CHAT_ID": chat_id}`（飞书 **remote/queue 分支** `enqueue_task` 时传入，
  `handler_async.py:1771`）；**direct 分支连 `IM_CHAT_ID` 都没有传**。
- 因此飞书 direct 的 agent 进程 env 缺失：`GRAPE_SESSION_TOKEN`、`GRAPE_CLI_MODE`、`USER_ID`、`CONVERSATION_ID`。
  `grape issues comment`（GitCode 回写 Issue）因无 `tk_` 会话凭据 → CLI/服务端 `verify_session_token_for_repo`
  （`auth_token.py`）拒绝 → agent 按安全规范如实上报「未注入」，不再自寻其它 token。

### 2.3 为什么 weekly-06 的修复没覆盖到飞书

weekly-06 `chat-direct-session-token-injection-design.md` 把修复范围限定在 `src/api/routes/chat.py` 的三个 Web direct
`stream_chat_agent` 调用点；**飞书 IM 用的是 `consume_stream_async`（另一执行器入口、另一模块），不在其改动清单内**。
本次为对称补齐。

### 2.4 为什么「重新触发任务」无法自愈（与 weekly-06 同结论）

飞书任务会话聊天再次触发仍走同一直达分支 → 依旧无 `GRAPE_SESSION_TOKEN`；仅当命中 devops 事件 / queue 队列执行才有。

---

## 三、方案设计

### 3.1 复用一个公共 helper（消除与 Web 侧逻辑漂移）

weekly-06 在 `chat.py` 内实现的是私有 `_chat_session_env_extra`。本轮**将其上移/暴露为公共 helper**，飞书 IM 与
Web 共用同一套语义，避免两组判断漂移：

**方案 A（推荐）：新建独立公共模块 `src/api/chat_session_env.py`**

```python
# 新建 src/api/chat_session_env.py
def chat_session_env_extra(user_id: int, conv_id: str, repo_key: str) -> dict:
    """任务会话（真实仓库）chat 会话凭据注入：与 queue 语义对齐。

    仅真实仓库注入 tk_ 会话 token；一般对话（repo_key 空 / `chat:` 伪仓库）不注入，行为不变。
    """
    env = {"USER_ID": str(user_id), "CONVERSATION_ID": conv_id}
    if repo_key and not repo_key.startswith("chat:"):
        from src.api.auth_token import issue_session_token
        env["GRAPE_SESSION_TOKEN"] = issue_session_token(
            session_key=f"conv:{conv_id}",
            repo_key=repo_key, agent_type="chat", user_id=user_id)
        env["GRAPE_CLI_MODE"] = "WORKER"   # 与 queue 注入一致，CLI 只暴露 dual+session_only 命令
    return env
```

> 落地时在 `src/api/chat_session_env.py` 新增该函数，并从 `chat.py` 迁移 `_chat_session_env_extra` 函数体（删除私有
> 实现），`chat.py` 三处改为 `from src.api.chat_session_env import chat_session_env_extra` 调用（行为不变，回归防漂移）。

### 3.2 飞书 direct 分支：`consume_stream_async` 追加 `env_extra`

```python
# src/im/handler_async.py  direct 分支
env_extra = chat_session_env_extra(user_id, conv_id or "", repo_key or "")
env_extra["IM_CHAT_ID"] = chat_id          # 保留既有语义
async for event_type, data in consume_stream_async(
        prompt, wsd, oc_sid, "chat",
        backend_name=backend, account=exec_account,
        user_token=user_token, user_id=user_id,
        agent_name="Chat Agent",
        agent_label=f"[Agent:chat|repo:{repository}]",
        env_extra=env_extra):               # ← 追加
    ...
```

- `user_id` / `conv_id` / `repo_key` 在 `_process_chat_inner_async` 中已解析（`handler_async.py:1632-1644`），直接用。
- 一般对话（repo_key 空或 `chat:` 伪仓库）→ helper 只回 `USER_ID`/`CONVERSATION_ID`，不注入 token，行为与现状一致。

### 3.3 飞书 remote/queue 分支：env_extra 并入会话上下文

```python
# src/im/handler_async.py  remote 分支 enqueue_task
env_extra = chat_session_env_extra(user_id, conv_id or "", repo_key or "")
env_extra["IM_CHAT_ID"] = chat_id
task = await enqueue_task(..., env_extra=env_extra, ...)
```

`enqueue_task` 内已有 `GRAPE_SESSION_TOKEN` 签发（`service.py:802`），重复传同 session_key 也无害（同 conv 幂等）。
风险极低，属一致性收口（remote 分支现状已能注入，本项仅为 ISE 对齐）。

### 3.4 关键决策

| 决策 | 依据 |
|------|------|
| session_key = `conv:{conv_id}` | 与 Web 侧一致；按会话定位/失效，不误伤同用户并发其它会话 |
| agent_type = "chat"，user_id = 用户 id | token 绑定调用者身份；与 queue / Web chat 注入一致 |
| 仅真实仓库注入 | 一般对话 repo_key=`chat:{id}` 伪仓库注入无意义；避免 `GRAPE_CLI_MODE=WORKER` 隐藏 user_only 命令而退化普通对话 |
| 注入 `GRAPE_CLI_MODE=WORKER` | 与 queue / devops direct / Web 注入一致；CLI 只暴露 dual+session_only 命令 |
| 不新增撤销钩子 | chat direct 无任务终态；`tk_` TTL 6h（auth_token.py）+ `purge_expired_session_tokens` 兜底清理 |
| 异常兜底 | `chat_session_env_extra` 内 `issue_session_token` 抛异常 → 仅缺 token 字段，chat 仍可正常回复（告警日志），不让 agent 整体失败 |
| 与云凭据互斥无关 | helper 不含任何平台 token；服务端 token 存储/使用不动（对齐 vcs-env-token-boundary-audit：agent env 不再下发 BOT_*/USER_* 平台 token） |

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/im/handler_async.py` | direct 分支 `consume_stream_async` 追加 `env_extra=`；remote 分支 `enqueue_task` 的 `env_extra` 并入会话上下文（`IM_CHAT_ID` 保留） |
| `src/api/routes/chat.py` | 删除私有 `_chat_session_env_extra`，三处调用改为 `from src.api.chat_session_env import chat_session_env_extra`（行为不变，防漂移） |
| `src/api/chat_session_env.py` | **新建**：公共 `chat_session_env_extra(user_id, conv_id, repo_key)`（从 chat.py 迁移函数体，语义不变） |
| `tests/` | **新建** `tests/test_chat_session_env.py`（helper 单元：真仓库/伪仓库/空/异常兜底/幂等）+ `tests/test_feishu_chat_session_token.py`（飞书 direct/remote 透传、token 作用域正/反例、生命周期），详见 §六 |

**未改动**：`enqueue_task`、`exec_devops_agent`（event_pipeline）、`consume_stream_async/stream_chat_agent` 签名、
CLI 过滤逻辑、auth_token.py。

---

## 五、影响面 / 安全边界

| 项 | 说明 |
|----|------|
| 行为增量 | 仅飞书 direct + 真实仓库的任务会话 chat 增加 env 注入，与 Web/queue/事件任务对齐；一般对话、其它链路零变化 |
| 作用域 | `tk_` 绑定该会话的 `repo_key`；`verify_session_token_for_repo` 仅放行同仓库内部协议端点（git-repo/issues 等）。飞书任务会话绑定真实仓库（`/task-open` 时建立），回写 Issue 即为同仓库 |
| 凭据安全 | `tk_` 明文仅注入进程 env，sha256 落库；TTL 6h 自动过期；日志 `_tokens` 脱敏走既有机制；不新增任何平台 token 注入 |
| 与生产事故因果 | 修复后飞书任务会话 agent 拿到会话凭据 → `grape issues comment` 回写成功；无 token 的一般对话不受影响 |
| 并发/竞态 | 不同会话独立 session_key，互不干扰；同会话并发消息复用同一 token 绑定（与 Web 侧一致） |

---

## 六、验证

### 6.1 helper 单元测试（`tests/test_chat_session_env.py`）

| 用例 | 断言 |
|------|------|
| `chat_session_env_extra(uid, conv, "gitcode:owner/repo")` | 含 `GRAPE_SESSION_TOKEN`（`tk_` 前缀）+ `GRAPE_CLI_MODE=WORKER` + `USER_ID`/`CONVERSATION_ID`；`verify_session_token` 解出 repo_key=`gitcode:owner/repo`、agent_type=`chat`、user_id=uid |
| `chat_session_env_extra(uid, conv, "chat:1")` | 无 `GRAPE_SESSION_TOKEN`（一般对话，行为不变） |
| `chat_session_env_extra(uid, conv, "")` | 无 `GRAPE_SESSION_TOKEN`（无仓库，行为不变） |
| `chat_session_env_extra(uid, conv, None)` | 无 `GRAPE_SESSION_TOKEN`，不抛异常 |
| 幂等 | 同 `(uid, conv, repo)` 两次调用返回同一 `session_key` 前缀 `conv:`（同会话共享绑定，便于清理） |
| 异常兜底 | `issue_session_token` 抛异常（mock）→ helper **不抛**，仍返回 `USER_ID`/`CONVERSATION_ID`，仅缺 token 字段，`log.warning` 一次 |

### 6.2 飞书 direct 分支透传（`tests/test_feishu_chat_session_token.py`，mock 引擎 / patch `consume_stream_async`）

| 用例 | 断言 |
|------|------|
| `conv_type=task` 真实仓库 | `consume_stream_async` 收到的 `env_extra` 含 `GRAPE_SESSION_TOKEN`（`tk_`）+ `GRAPE_CLI_MODE=WORKER` + `USER_ID`/`CONVERSATION_ID` + `IM_CHAT_ID` |
| 一般对话（repo_key 空 / `chat:` 伪仓库） | `env_extra` 无 `GRAPE_SESSION_TOKEN`；仍含 `USER_ID`/`CONVERSATION_ID`/`IM_CHAT_ID` |
| token 与 task 会话仓库匹配 | `verify_session_token_for_repo(token, repo_key)` 通过（同仓库） |
| token 跨仓库拒绝 | 用该 token 调另一仓库端点 → `verify_session_token_for_repo` 拒绝 / 403（作用域反例） |
| 会话复用 | 同 conv 多次消息 → 同一 session_key（`conv:{conv_id}`），不重复堆积绑定 |

### 6.3 飞书 remote/queue 分支

| 用例 | 断言 |
|------|------|
| `enqueue_task` 的 `env_extra` | 含 `IM_CHAT_ID` + `USER_ID`/`CONVERSATION_ID`；含 `GRAPE_SESSION_TOKEN`（helper 签发）或 `enqueue_task` 自行签发（service.py:802），二选一均可、无 401 缺口 |
| 同会话幂等 | remote 重复触发不产生多份会话 token 冲突（依赖 enqueue_task 幂等，回归 `test_executor_token_env.py`） |

### 6.4 token 生命周期

| 用例 | 断言 |
|------|------|
| 删除后失效 | 删除 `session_api_tokens` 对应行 → `verify_session_token` 返回 None（agent 401，符合安全兜底） |
| 6h TTL | `purge_expired_session_tokens` 清理过期 `conv:*` 令牌（回归 auth_token 既有用例） |

### 6.5 回归

| 项 | 断言 |
|----|------|
| `tests/test_chat_*`、`tests/test_feishu_*`、`tests/test_web_chat_tooluse.py`、`tests/test_executor_token_env.py` | 全绿（Web 迁移 helper 后行为不变） |
| 全量套件 | 新增失败 = 0（除既有已知失败 `test_events_empty` 过期断言外无新红） |

---

## 七、相关文档 / 提交约定

- weekly-06 同源修复：`docs/weekly-06/chat-direct-session-token-injection-design.md`（Web 三条路由）。
- 会话 token 机制：`issue-45-api-token-design.md`、`auth_token.py` / `chat_session_env` 头注释。
- CLI 命令按 token 作用域过滤：`grape-cli-mode-filter-design.md`。
- 现存相关 WIP：`docs/weekly-07/feishu-chat-stream-unified-design.md`（未提交，改动了 `src/im/handler_async.py` 与
  `src/api/chat_task_manager.py`）。本方案与其**正交**（注入 env_extra vs 事件流统一），实施时在同一文件上叠加、
  合并提交时注意双改动共存；若先合并 WIP，本方案在合并后的 handler 上施加同一注入点。
- 提交（Conventional Commits，`git commit -s`）：`fix(im): inject session token for feishu direct task-chat agent`。