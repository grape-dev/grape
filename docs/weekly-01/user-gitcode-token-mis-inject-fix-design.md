# USER_GITCODE_TOKEN 误注入 devops 事件任务 — 分析与修复方案

> 版本：v1.0 ｜ 日期：2026-09-04
> 现象：生产日志大量 `WARNING [Agent:vod|repo:...|engine:opencode-worker-123249] USER_GITCODE_TOKEN empty, fallback to repo bot token (len=24)`
> 结论：**语义缺陷**——devops 事件任务被 executor 的「chat 化兜底」误拉入 user-token 链路，做了不该做的 DB 查询、注入到错误语义的环境变量、并打出误导性 WARNING。

---

## 一、现象

```
WARNING [Agent:vod|repo:gitcode:Jason7602/hello-blog-12|engine:opencode-worker-123249]
        USER_GITCODE_TOKEN empty, fallback to repo bot token (len=24)
```

- 来源：`src/agent/executor.py:283`
- 触发：**每个 devops 事件任务**（vod/issue/coding/sre，含引导）经远端 worker 广播执行时都打一条
- 仓库：任意模板引导/事件仓库（`hello-blog-12` 仅是观察样本）

---

## 二、为什么是错误的（语义缺陷）

### 2.1 devops 事件 prompt 只认 `BOT_GITCODE_TOKEN`

| 场景 | prompt 安全规则 | 实际使用的 token |
|---|---|---|
| devops 事件（vod/issue/coding/sre） | `prompt_builder.py:164`：*"Use ONLY ... (**BOT_GITCODE_TOKEN**)"*；`:483` A2A 交接 `$BOT_GITCODE_TOKEN` | **仓库 bot token** |
| chat 事件 | `prompt_builder.py:285`：*"Use ONLY ... (**USER_GITCODE_TOKEN**)"* | **用户本人 token** |

→ 技能里建 Issue/评论等 GitCode API 写操作一律读 `$BOT_GITCODE_TOKEN`（§十 写操作隔离设计）。

### 2.2 现状：executor 把 bot token 塞进 `USER_GITCODE_TOKEN`

```
事件任务 → worker agent.py:1120-1126（仅 chat 解析 user_token → 事件任务 user_token=""）
executor.py:266 else 分支（user_token 空）:
  ├─ :273-278  user_id>0 → get_user_gitcode_token(user_id)   ← 事件任务根本不该查（worker 无 DB 必空）
  └─ :281-283  else bot_token → USER_GITCODE_TOKEN=bot_token + WARNING
```

- 功能上无害（devops prompt 不看 `USER_GITCODE_TOKEN`）
- 语义上误导：每次事件执行都暗示"丢了用户 token"，与「事件任务本就 user-less」矛盾
- 冗余：`get_user_gitcode_token(user_id)` 对事件任务是死查询

### 2.3 根因

**`executor.py` 的 token 兜底链路是照 chat 场景写的**，未按 `event_type` 分流：
- 假设每个任务都期望 `USER_GITCODE_TOKEN`
- 于是事件任务：查了不该查的用户 token → 注入到错误语义的变量 → 打误导 WARNING

---

## 三、修复方案（方案 B 增强版：按 event_type 强制分流）

### 3.1 executor.py（核心改动）

```
改后逻辑（stream_agent 主路径 + hermes ACP，token **互斥**注入）:
    # 判定严格以 event_type 为准（不依赖 user_token 兜底，防调用方误传导致误判 chat）
    if event_type == "chat":
        # chat（用户身份）：只注入 USER_GITCODE_TOKEN，不注入 BOT_GITCODE_TOKEN
        if not user_token and user_id > 0:
            user_token = get_user_gitcode_token(user_id)   # DB 兜底
        if user_token:
            env["USER_GITCODE_TOKEN"] = user_token
        else:
            # 配置异常：chat 无用户 token 时不以 bot token 冒充用户身份 → log.error，仅提示会 401
            log.error(...)
    elif bot_token:
        # devops 事件（仓库身份）：只注入 BOT_GITCODE_TOKEN
        env["BOT_GITCODE_TOKEN"] = bot_token
    # BOT_GITCODE_TOKEN 与 USER_GITCODE_TOKEN 互斥，二者不同时注入
    # （避免 chat 用户对话上下文暴露仓库 bot token）
```

### 3.2 日志级别（配套降噪）

```
event_type == "chat" 且兜底失败（user_token/bot_token 双空）→ WARNING（真异常：chat 会 401）
其余 fallback 到 bot_token → info（事件任务正常语义）
```

### 3.3 hermes ACP 路径（`_stream_hermes_acp`，executor.py:156-162）

当前行为一致但无日志；对齐主路径：chat 才兜底 user-token，事件任务只发 bot token。

### 3.4 worker 源头（agent.py:1120-1126）— 保持不变

- 已正确：仅 chat 解析 `get_user_gitcode_token`
- 事件任务 `user_token=""` 传入，符合"事件任务 user-less"语义
- **无需改动**（executor 是唯一误分流点）

---

## 四、测试影响与新增

### 4.1 现有测试（`tests/test_executor_token_env.py`）

5 个用例断言**环境变量注入行为**（`USER_GITCODE_TOKEN == "BOT1"` 等），不断言日志级别：

| 用例 | 现断言 | 改后（事件/chat 分流） |
|---|---|---|
| `test_user_token_wins_over_bot_token`（event_type=chat） | USER/GIT=BOT | 不变 ✅ |
| `test_bot_token_fallback_when_user_token_empty`（user_id=0） | USER=BOT1 | 需按 chat/事件复核 |
| `test_db_user_token_fallback`（user_id=3，DB 有） | USER=DBUSR | event_type=chat → 仍查 DB ✅ |
| `test_db_empty_then_bot_fallback`（user_id=3，DB 空） | USER=BOT1 | chat + DB 空 → 仍兜底 bot ✅ |
| `test_no_token_no_injection` | 不注入 | 双空仍不注入 ✅ |

- 风险：`test_bot_token_fallback_when_user_token_empty`（user_id=0，event_type=chat）在改后语义下应仍兜底 bot（chat 用户无 token 但仓库有 bot）→ 需确认保留。
- **新增用例**：`event_type != "chat"` 的事件任务 → 断言 `USER_GITCODE_TOKEN` **不被注入**、`BOT_GITCODE_TOKEN` 正常、不触发任何 DB 兜底查询。

### 4.2 调用方 event_type 核对

grep 所有 `stream_agent` / `stream_runtime` 调用方，确认：
- chat 路径 event_type 稳定传 `chat`（chat.py 直传、worker 用 task.event_type）
- 事件路径 event_type 为 issue_opened 等（非 chat）
- 无第三类"非 chat 但期望用户 token"的调用方（否则会被误跳过）

---

## 五、影响面

| 文件 | 改动 |
|---|---|
| `src/agent/executor.py` | token 兜底按 event_type 分流；日志级别修正 |
| `tests/test_executor_token_env.py` | 复核现用例 + 新增「事件任务不注入 USER_GITCODE_TOKEN」用例 |

**行为正确性**：`BOT_GITCODE_TOKEN` 不变；devops 事件不再注入 `USER_GITCODE_TOKEN`、不再误查用户 token、不再刷 WARNING；chat 语义不变。

---

## 六、验证

1. `pytest tests/test_executor_token_env.py` + `test_runtime_agent.py`（chat user_token 相关）全绿
2. 生产观测：事件任务日志不再出现 `USER_GITCODE_TOKEN empty, fallback` WARNING；chat 正常回帖
3. 引导/事件 E2E 走通（worker 广播执行不 401）

---

*方案待实施；先分析、后落地。*