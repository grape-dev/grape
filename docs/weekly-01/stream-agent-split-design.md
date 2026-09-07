# stream_agent 拆分：stream_chat_agent / stream_devops_agent 隔离方案

> 版本：v1.0 ｜ 日期：2026-09-04
> 背景：`src/agent/executor.py` 的 `stream_agent` 同时承载 chat 与 devops 事件两类语义，
> 靠 `event_type` 参数分流 token/凭据注入，存在判定误用、语义混杂、冲突扩散的隐患。
> 目标：拆分为 `stream_chat_agent` + `stream_devops_agent`，从调用方传参即语义，彻底隔离两者。

---

## 一、现状问题

### 1.1 单函数双语义，靠参数分流

`stream_agent`（executor.py:244）用 `event_type == "chat"` 决定 token 注入分支：

```
stream_agent(prompt, cwd, session_id, event_type, backend_name, account,
             user_id, env_extra, bot_token, user_token, agent_name, agent_label, max_exec_seconds)
    ├─ if event_type == "chat":   # chat 语义：只注入 USER_GITCODE_TOKEN
    │      （DB 兜底用户 token；无则 log.error，不以 bot_token 冒充用户身份）
    └─ elif bot_token:            # devops 语义：只注入 BOT_GITCODE_TOKEN
           env["BOT_GITCODE_TOKEN"] = bot_token
    └─ if user_id > 0:            # 凭据注入：两者共用，不可区分意图
           get_credentials(user_id) → 华为云/codearts/用户自定义凭据
```

### 1.2 由此导致的隐患

| 隐患 | 说明 |
|---|---|
| **event_type 判定误用** | 调用方漏传 / 误传 event_type，会导致 chat 任务走入 devops 分支（不注入 USER，401）或事件任务误判 chat |
| **凭据注入语义混杂** | `if user_id > 0` 对「chat 用户身份」与「queue 事件仓库归属用户」都注入用户云凭据——**无法在传 user_id 时区分"该不该注入用户凭据"** |
| **token 泄漏面** | chat 与 devops 共用函数签名时，若调用方误传 bot_token 给 chat，历史上出现过「以 bot token 冒充用户」的兜底（已修，但函数层面仍易再犯） |
| **改一处动全局** | token 策略调整会同时影响 chat/事件两条链路，风险扩散 |

### 1.3 现状调用方

| 调用方 | 类型 | 传参特征 |
|---|---|---|
| `chat.py:520/757/1362/1755` | chat | `user_token=gitcode_token`，不传 bot_token（已补 `event_type="chat"`） |
| `event_pipeline.py:1853`（direct 事件） | devops 事件 | `bot_token=repo_gitcode_token` |
| `events.py:1120`（事件对话 event-dialogue） | devops 事件 | `bot_token=get_gitcode_token(repo)`，hermes 引擎，`build_conversation_prompt` |
| `im/handler_async.py` `consume_stream_async` | chat（队列） | user_token 注入 |
| `im/vod_handler_async.py` `consume_stream_async` | devops 事件 | `bot_token=repo_token` |
| worker `agent.py`（chat/事件队列） | 两者 | 按 `task.agent_type`/`event_type` 已分派 user_token/bot_token |

> 调用方全景（`events.py:1120` 补充）：
> ```
> 业务层                   转发/封装                 核心
> ─────────────────────────────────────────────────────────────
> chat.py (4 处, chat)  ─┐
> events.py (事件对话)    ├→ stream_agent ─────────────→ _run_once (opencode)
> event_pipeline.py(事件) │
> handler_async (chat)   ├→ consume_stream_async ─────→ 线程池 → stream_agent
> vod_handler (事件)     ┘
> worker agent.py ─────────────────────────→ stream_runtime → stream_agent
> ```
> - `stream_agent` 是唯一执行核心；`stream_runtime` 是别名转发（M0 术语兼容）；
>   `consume_stream_async` 是 async 线程池封装。
> - 业务直接调用方 **6 处**：chat.py×4、events.py×1、event_pipeline.py×1。

---

## 二、拆分设计

### 2.1 目标函数签名

```
# ===== chat 专用：用户身份 =====
def stream_chat_agent(prompt: str, cwd: str, session_id: str = "",
                      account: str = "", user_id: int = 0,
                      user_token: str = "",
                      env_extra: dict | None = None,
                      agent_name: str = "",
                      agent_label: str = "",
                      backend_name: str = "",
                      max_exec_seconds: int = 600) -> Generator:
    # 恒 chat 语义：
    #   - 只注入 USER_GITCODE_TOKEN（DB 兜底；无则 log.error，不以 bot_token 冒充用户身份）
    #   - 注入用户云凭据（用户身份）
    #   - 不接收 bot_token → 从签名层面杜绝"以 bot 冒充用户"

# ===== devops 事件专用：仓库身份 =====
def stream_devops_agent(prompt: str, cwd: str, session_id: str = "",
                        event_type: str = "",
                        account: str = "", repo_user_id: int = 0,
                        bot_token: str = "",
                        env_extra: dict | None = None,
                        agent_name: str = "",
                        agent_label: str = "",
                        backend_name: str = "",
                        max_exec_seconds: int = 600) -> Generator:
    # 恒 devops 语义：
    #   - 只注入 BOT_GITCODE_TOKEN
    #   - user_id 语义 = 仓库归属用户（仅用于取云凭据），与 chat 用户 token 严格分离
    #   - 不接收 user_token → 事件任务无法以用户身份写 GitCode
```

### 2.2 结构拆解（共用核心，策略分离）

```
stream_chat_agent / stream_devops_agent
    ├─ 各自组装 env（token + 凭据，策略不同）
    ├─ 共用：replace_skill_dir_placeholders / 脱敏 / agent_name / env_extra
    ├─ 共用：获取 backend / hermes ACP 分派 / CLI 查找 / _run_once 执行
    └─ 分别分发到：
          _stream_opencode_common(env, ...)      # opencode/codearts 执行核心
          _stream_hermes_acp(...)                # hermes 执行核心（拆分 token 注入）
```

> env 组装差异收口到「chat 版 env builder」/「devops 版 env builder」两个私有 helper，
> 底层进程执行（Popen/pre_exec/超时/看门狗）完全复用同一段，避免重复。

### 2.3 向后兼容

`stream_agent` 保留为**薄分发器**（不再包含任何 token/凭据注入逻辑）：

```
def stream_agent(prompt, cwd, session_id="", event_type="", ...):
    if event_type == "chat":
        yield from stream_chat_agent(...)
    else:
        yield from stream_devops_agent(...)
```

- 外部既有引用（`runtime/executor.stream_runtime`、测试等）不破坏。
- 内部不再有 if/else 缠结，二义性降到薄分发器一层。
- 可选：后续清理后移除 `stream_agent` 本体，仅留分发。

---

## 三、调用方改造清单

| 调用方 | 改用 | 说明 |
|---|---|---|
| `chat.py:520/757/1362` | `stream_chat_agent(...)` | 去掉 `event_type="chat"`（语义已内建）；不再需要传 bot_token |
| `event_pipeline.py:1853`（direct 事件） | `stream_devops_agent(...)` | `repo_user_id=...`？→ 见 §5 待确认 |
| `events.py:1120`（事件对话 event-dialogue） | `stream_devops_agent(...)` | `bot_token=get_gitcode_token(repo)`、hermes；事件对话无 user_token |
| `im/handler_async.py` `consume_stream_async`（chat） | 拆为 consume_chat / 或内部走 `stream_chat_agent` | |
| `im/vod_handler_async.py` `consume_stream_async`（事件） | 拆为 consume_devops / 或内部走 `stream_devops_agent` | |
| worker `agent.py`（chat/事件队列） | 按 task 类型分派调用对应函数 | `task.event_type=="chat"` → chat 版；否则 devops 版 |
| `chat.py:1755`（_execute_chat_task 死代码） | 同步或移除 | 无调用方，可顺手清理 |

---

## 四、测试

### 4.1 保留与新增

| 用例 | 断言 |
|---|---|
| `test_chat_agent_injects_only_user_token` | `stream_chat_agent` 注入 USER_GITCODE_TOKEN；**无 BOT_GITCODE_TOKEN**；即使误传 bot_token 亦不注入 |
| `test_chat_agent_no_user_token_errors` | chat 无用户 token 且传 bot_token → 不在 env 里冒充 USER；log.error |
| `test_devops_agent_injects_only_bot_token` | `stream_devops_agent` 注入 BOT_GITCODE_TOKEN；**无 USER_GITCODE_TOKEN**；即使误传 user_token 亦不注入 |
| `test_devops_agent_user_credentials` | queue 事件（repo_user_id>0）注入用户云凭据（华为云/codearts），但无 USER_GITCODE_TOKEN |
| `test_stream_agent_dispatch` | 老 `stream_agent`：event_type=chat → chat 语义；否则 devops 语义 |
| 现有 `test_executor_token_env.py` / `test_runtime_agent.py` | 适配新函数名后全绿 |

### 4.2 关键回归

- chat direct / chat 队列：正常会话、回帖 200（user token 生效）
- devops 事件（direct + queue）：建 Issue/评论走 bot token、事件任务云凭据可用
- hermes ACP（chat + 事件）两路径各自 token 正确

---

## 五、待确认项（会影响实现细节）

1. **devops 事件凭据注入**：queue 事件任务现在 `user_id=_issue_scene_uid`（仓库归属用户）触发 `get_credentials` 注入用户云凭据。
   - 拆分后 `stream_devops_agent` 是否保留此行为（`repo_user_id` 仍注入用户云凭据）？
   - 还是事件任务**不再注入用户凭据**（改上层「凭据注入」与「执行身份」分离）？
   - → 这决定 `stream_devops_agent` 是否还需要 `repo_user_id` 及其语义。
2. **`stream_agent` 去向**：保留薄分发器 / 或直接替换调用方后删除。
3. **`consume_stream_async`**：是拆两个 async 函数，还是保留并内部分发到 chat/devops 执行核心。

---

## 六、影响面

| 文件 | 改动 |
|---|---|
| `src/agent/executor.py` | 拆 `stream_chat_agent` / `stream_devops_agent`；`stream_agent` 降为分发器；`_stream_hermes_acp` 拆分 token 注入 |
| `src/runtime/executor.py` | `consume_stream_async` 适配（chat/devops 分发） |
| `src/api/routes/chat.py`（4 处） | 改用 `stream_chat_agent` |
| `src/api/event_pipeline.py`（direct） | 改用 `stream_devops_agent` |
| `src/api/routes/events.py`（事件对话） | 改用 `stream_devops_agent` |
| `src/im/handler_async.py` | chat consume 适配 |
| `src/im/vod_handler_async.py` | devops consume 适配 |
| `src/runtime/agent.py` | worker 按 task 类型分派 |
| 测试 | 适配 + 新增 6 项隔离/强制校验 |

---

*方案待评审；关键分叉点见 §五，确认后实施。*