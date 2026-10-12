# 方案：direct 模式 chat 注入 GRAPE_SESSION_TOKEN（Issue #2 澄清回写失败根因修复）

> 状态：待实施（2026-10-11）｜目标分支：weekly-06-lyz（生产部署分支）｜关联：Issue-45 M2 会话 token 机制
> 动机：任务会话的 Chat Agent 在 **direct（本机直连）执行模式**下反映「环境未注入 `GRAPE_SESSION_TOKEN`」，
> 无法调用 GitCode 接口把澄清结论回写 Issue（如 Issue #2）。根因是 chat 的 direct 执行链路漏掉了
> `GRAPE_SESSION_TOKEN` 注入（queue 模式正常）。本文档给出根因与修复方案。

---

## 一、问题现象

- Chat Agent（任务会话）回复：「本会话的运行环境未注入 `GRAPE_SESSION_TOKEN`（会话凭据为空），因此我无法调用 GitCode 接口回写需求到 Issue #2。按安全规范，我不会从其他来源获取或尝试替代 token。」
- 期望行为：任务会话 chat 应拥有与 queue 模式一致的会话级凭据，可把澄清结论回帖到 Issue（`tk_` 会话 token）。

---

## 二、根因分析

### 2.1 注入点现状（矩阵）

`GRAPE_SESSION_TOKEN`（`tk_`，auth_token.py:37，绑定 `(repo_key, agent_type[, user_id])`）仅在两处签发注入：

| 执行路径 | 位置 | chat 是否覆盖 |
|----------|------|---------------|
| 队列入队 `enqueue_task` | `src/runtime/service.py:802-814` | ✅ 包含 chat（注释亦写明「chat 与 devops 任务均适用」） |
| devops 事件 direct `exec_devops_agent` | `src/api/event_pipeline.py:2999-3007` | 不适用（仅事件任务） |
| **chat direct** `stream_chat_agent` | `src/api/routes/chat.py` 三处：`:641` `/chat/send`、`:884` `/chat/send/stream`、`:1514` `/chat/submit` | ❌ **缺口**：`env_extra` 未传，进程 env 无 `GRAPE_SESSION_TOKEN`，也无 `GRAPE_CLI_MODE` |

### 2.2 生产为何命中该缺口（auto → direct 推导链）

1. 生产 `grape.service` systemd 环境未设 `DEVOPS_EXECUTION_MODE`；meta `execution_mode` 无值 → 回落默认 `EXEC_MODE_AUTO`（service.py:240）。
2. `runtime_execution_kind(runtime_id)`（service.py:324-348）auto 语义：**绑定引擎宿主 credential_id == 0（本机）→ `EXEC_MODE_DIRECT`**；未绑定 / 远端失联亦回退 direct。
3. 任务会话 chat 绑定本机引擎 → `/chat/send|send/stream|submit` 走 direct 分支 → `stream_chat_agent(prompt, cwd, ..., repo_key=repo_key)`，**未传 `env_extra`**。
4. agent 调 `grape … issue comment`（GitCode 回写 Issue #2）时无 `tk_` → CLI/服务端 `verify_session_token_for_repo`（auth_token.py:91）拒绝 → agent 按安全规范如实上报「未注入」。

### 2.3 为什么「重新触发任务」无法自愈

queue 模式 chat 经 `enqueue_task` 有 token；direct 模式 chat 是**结构性缺口**——同一任务从任务中心 re-trigger 仍走 chat direct，依旧无 token（仅当重新命中 devops 事件/队列执行才有）。

---

## 三、方案设计

### 3.1 对齐 queue 语义：direct chat 注入会话 token

新增 helper（`src/api/routes/chat.py` 内）：

```python
def _chat_session_env_extra(user_id: int, conv_id: str, repo_key: str) -> dict:
    """direct chat 会话凭据注入（src/api/routes/chat.py 内私有）。

    目标：任务会话（绑定真实仓库）的 chat 对齐 queue 语义，注入 tk_ 会话 token，
    使 Chat Agent 能调用仓库作用域内部端点（如 git-repo issue comment 回写澄清结论）。
    仅真实仓库注入；一般对话（repo_key 为空或 `chat:{id}` 伪仓库）不注入，行为不变。
    """
    env = {"USER_ID": str(user_id), "CONVERSATION_ID": conv_id}
    if repo_key and not repo_key.startswith("chat:"):
        from src.api.auth_token import issue_session_token
        env["GRAPE_SESSION_TOKEN"] = issue_session_token(
            session_key=f"conv:{conv_id}",
            repo_key=repo_key, agent_type="chat", user_id=user_id)
        # 与 queue 注入（service.py:812-814）一致：CLI 按 tk_ 作用域过滤，只暴露 dual+session_only 命令
        env["GRAPE_CLI_MODE"] = "WORKER"
    return env
```

三处 direct `stream_chat_agent` 调用点追加 `env_extra=_chat_session_env_extra(user["id"], conv_id, repo_key)`。

### 3.2 关键决策

| 决策 | 依据 |
|------|------|
| session_key = `conv:{conv_id}` | 按会话定位 / 失效，不误伤同用户并发其它会话（对齐 auth_token.py 注释「task_id / conv_id」语义） |
| agent_type = "chat"，user_id = 用户 id | token 绑定调用者身份；与 queue 模式 chat 注入一致（service.py:809-811） |
| 仅真仓库注入 | 一般对话 repo_key=`chat:{id}` 伪仓库注入无意义；避免 `GRAPE_CLI_MODE=WORKER` 隐藏 user_only 命令而退化普通对话 |
| 注入 `GRAPE_CLI_MODE=WORKER` | 与 queue / devops direct 注入一致；CLI 只暴露 dual+session_only 命令，避免无 token 打 user_only 接口 403 重试 |
| 不新增撤销钩子 | chat direct 无任务终态；`tk_` TTL 6h（auth_token.py:21）+ `purge_expired_session_tokens` 兜底清理 |
| gate 判定 | `repo_key` 非空且非 `chat:` 前缀；task 会话（`conv_type=="task"`）自然满足 |

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/routes/chat.py` | 新增 `_chat_session_env_extra`；三个 direct `stream_chat_agent` 调用点（`:641` `/chat/send`、`:884` `/chat/send/stream`、`:1514` `/chat/submit`）追加 `env_extra=` |
| `tests/` | 新增用例：helper（真仓库→token+WORKER+绑定上下文；伪/空仓库→无 token）；direct chat 路由透传（conv_type=task 时 `stream_chat_agent` 收到含 `GRAPE_SESSION_TOKEN` 的 env_extra）；token 作用域校验正/反例 |

**未改动**：`enqueue_task`、`exec_devops_agent`、`stream_chat_agent/stream_agent` 签名、CLI 过滤逻辑、auth_token.py。

---

## 五、影响面 / 安全边界

| 项 | 说明 |
|----|------|
| 行为增量 | 仅 direct + 真实仓库的任务会话 chat 增加 env 注入；一般对话、queue 模式、devops 事件零变化 |
| 写隔离 | `stream_chat_agent` 签名不含 bot_token → 仍只注入 `USER_GITCODE_TOKEN`（§十 写操作隔离不变）；会话 token 只解锁仓库作用域内部协议端点 |
| 作用域 | `verify_session_token_for_repo` 仅校验 repo_key 匹配，chat 与 devops 共用；注入对象即该任务会话绑定的真实仓库 |
| 凭据安全 | `tk_` 明文仅注入进程 env，sha256 落库；TTL 6h 自动过期；`_tokens` 脱敏走既有机制 |
| 兜底 | 若 `issue_session_token` 抛异常（DB 不可用等），helper 仅缺失 token 字段，chat 仍可正常回复（告警日志），不让 agent 整体失败 |

---

## 六、验证

| 用例 | 断言 |
|------|------|
| `_chat_session_env_extra(user, conv, "gitcode:owner/repo")` | 含 `GRAPE_SESSION_TOKEN`（`tk_` 前缀）+ `GRAPE_CLI_MODE=WORKER` + `USER_ID`/`CONVERSATION_ID`；`verify_session_token` 解出 repo_key/agent_type=chat/user_id |
| `_chat_session_env_extra(user, conv, "chat:1")` / `""` | 无 `GRAPE_SESSION_TOKEN`（一般对话行为不变） |
| direct `/chat/submit`（conv_type=task，mock 引擎） | `stream_chat_agent` 收到的 `env_extra` 含 `GRAPE_SESSION_TOKEN` |
| 作用域反例 | 用该 token 调其它仓库端点 → `verify_session_token_for_repo` 拒绝 |
| 回归 | `tests/test_web_chat_tooluse.py` / `test_chat_handler.py` / `test_executor_token_env.py` 全绿；全量套件 branch-only 失败 = 0 |

---

## 七、相关文档 / 提交约定

- Issue-45 M2 会话 token：`docs/agent-workflow-and-prompt-design.md` §2.2、`auth_token.py` 头注释。
- CLI 命令按 token 作用域过滤：`grape-cli-mode-filter-design.md`。
- 提交（Conventional Commits，`git commit -s`）：`fix(chat): inject session token for direct-mode chat agent`。