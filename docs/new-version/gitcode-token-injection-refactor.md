# GitCode Token 注入链路重构实施计划

> 状态：待执行
> 日期：2026-08-03
> 关联：修复 issue #6 出现 3 条事件（bot 自循环回声事件被记录）的根本性问题

## 1. 背景与问题

### 1.1 issue #6 现象

`Jason7602/task-management-study` Issue #6 创建后产生了 3 条事件：

| seq | event_type | 触发原因 |
|-----|-----------|----------|
| 244 | `issue_opened` | 用户创建 Issue（正常，应保留）|
| 245 | `issue_updated` | agent 打 need-info 标签的**回声 webhook** |
| 246 | `issue_comment` | agent 发追问评论的**回声 webhook** |

前两条是 agent 自身动作的回声，却被记录为独立事件。

### 1.2 根因

回声 webhook 无法被"bot 自循环过滤"识别，因为：

- **agent 打标签时用了用户 token（`mZ__xQ`）而非 bot token**
  - GitCode 认为操作者是 Jason7602 → 回声 webhook 的 actor=Jason7602
  - 自循环检测（`actor ∈ bot_usernames`）失效
- **token 注入链路混乱**：`GITCODE_API_TOKEN` 环境变量语义不清、bot token 来源分裂、agent 可从历史上下文读到用户 token 并硬编码

### 1.3 token 注入链路现状

| 步骤 | 代码 | 实际行为 |
|------|------|----------|
| bot token 来源（webhook/A2A）| `event_pipeline._execute_agent` | `get_bot_token_by_repo()` = `managed_repos.bot_gitcode_token`，**未读 `devops_agent_session.repo_gitcode_token`** |
| bot token 来源（VOD）| `vod_handler` / `vod_handler_async` | `devops_agent_session.repo_gitcode_token` ✓ |
| 环境变量注入 | `opencode.py:24-25`、`codearts.py:21-22` | `GITCODE_API_TOKEN` = account 配置的 token |
| 环境变量注入 | `executor.py:57-59` | `GITCODE_API_TOKEN` = repo_gitcode_token |
| 环境变量注入 | `chat.py:120/212` | `GITCODE_API_TOKEN` = 用户 token（env_extra）|

**问题**：
1. `devops_agent_session.repo_gitcode_token` 字段存在（前端配置 bot token 的落点），但 webhook 的 `_execute_agent` 从不读取它
2. `GITCODE_API_TOKEN` 环境变量在 webhook 场景（bot token）、chat 场景（用户 token）、`get_env`（account token）三处互相覆盖，agent 无从判断当前是哪个
3. IM（飞书）chat 流程不创建 `chat_conversations`，缺少用户 token 的持久化载体

## 2. 设计决策（已与用户确认）

1. **bot token 首选** `devops_agent_session.repo_gitcode_token`，未配置时 fallback 全局 bot token（`configs/global-bot-token.json`）
2. **删除 `GITCODE_API_TOKEN` 环境变量注入**，改为 **通过 prompt 直接注入完整 token**（接受泄漏风险）
3. **chat agent（Web + IM）用用户 token**（存于 `chat_conversations.user_gitcode_token`）；**其他 agent（issue/pr/qa/vod）用 bot token**
4. skill 文件中 `$GITCODE_API_TOKEN` 统一改名：
   - bot 场景 → `$BOT_GITCODE_TOKEN`
   - chat 场景 → `$USER_GITCODE_TOKEN`
5. **IM（飞书）chat 流程必须创建 `chat_conversations`**——`chat_agent_session` 只存 repo 的 chat 配置，`chat_conversations` 存实际对话，两者不冲突（类比 `devops_agent_session` + `vod_conversations`）

## 3. 改动清单

### 3.1 新增统一 token 函数（`src/user/db.py`）

```python
def get_agent_bot_token(repo_key: str) -> str:
    """bot token：devops_agent_session.repo_gitcode_token → fallback 全局 bot token"""
    # 1. 查 devops_agent_session.repo_gitcode_token（该 repo 任一非空）
    # 2. 空 → configs/global-bot-token.json 的 gitcode_api_token

def get_user_gitcode_token(user_id: int) -> str:
    """用户 token：get_credentials(user_id) 提取 gitcode token"""
```

- 统一 webhook/VOD 场景的 bot token 读取，替代散落的 `get_bot_token_by_repo` + 手动读 global json

### 3.2 删除 GITCODE_API_TOKEN 环境变量注入

| 文件 | 位置 | 改动 |
|------|------|------|
| `src/agent/opencode.py` | :24-25 | 删 `env["GITCODE_API_TOKEN"] = acc["gitcode_api_token"]` |
| `src/agent/codearts.py` | :21-22 | 删 `env["GITCODE_API_TOKEN"] = acc["gitcode_api_token"]`（保留 CODEARTS_CLI_AK/SK）|
| `src/agent/executor.py` | :57-59 | 删 `env["GITCODE_API_TOKEN"] = repo_gitcode_token`；参数 `repo_gitcode_token` → `gitcode_token`（供 prompt 注入，不再写环境变量）|

### 3.3 token 来源统一（bot 场景）

| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` `_execute_agent` | 用 `get_agent_bot_token(repo_key)` 替代 `get_bot_token_by_repo` + 手动全局 fallback |
| `src/im/vod_handler.py` | `repo_gitcode_token` 为空时 fallback `get_agent_bot_token` |
| `src/im/vod_handler_async.py` | 同上 |

### 3.4 prompt 注入 token

| 文件 | 改动 |
|------|------|
| `src/prompt_builder.py` `build_prompt` | 加参数 `gitcode_token: str = ""`，末尾追加 `BOT_GITCODE_TOKEN=<token>` + 一行 curl 用法 |
| `src/prompt_builder.py` `build_chat_prompt` | 加参数 `user_gitcode_token: str = ""`，注入 `USER_GITCODE_TOKEN=<token>` |
| `src/api/event_pipeline.py` | `build_prompt(..., gitcode_token=bot_token)` |
| `src/api/routes/chat.py` | 删 env_extra（:120/:212）；`_build_chat_prompt`/`build_chat_prompt` 注入用户 token 为 `USER_GITCODE_TOKEN` |

### 3.5 IM chat 创建 chat_conversations + 用户 token

| 文件 | 改动 |
|------|------|
| `src/im/handler_async.py` `_process_chat_inner_async` | ① `user_id = get_user_id_by_app_id(ctx.app_id)`；② 按 `(user_id, repo_key, im_channel_id=chat_id)` 查 `chat_conversations`，无则 `create_conversation(user_id, repo_key, im_channel_id=chat_id, user_gitcode_token=get_user_gitcode_token(user_id))`；③ agent 执行前读 `user_gitcode_token` → `build_chat_prompt(..., user_gitcode_token=token)`；不再用 `get_bot_token_by_repo` |
| `src/im/handler.py`（同步版）| 同样处理（保持兼容）|

### 3.6 skill 文件改名（35 处 `$GITCODE_API_TOKEN`）

- **bot 场景** → `$BOT_GITCODE_TOKEN`：
  - `skills/issue/SKILL.md` + `references/*`
  - `skills/pr/SKILL.md` + `references/*`
  - `skills/qa/SKILL.md` + `references/*`
  - `skills/vod/SKILL.md`
  - `skills/implementation/references/*`
  - `skills/skill-pr/SKILL.md` + `references/*`
  - `skills/cloud-deploy/SKILL.md`
- **chat 场景** → `$USER_GITCODE_TOKEN`：
  - `skills/chat/SKILL.md`
- **说明文案**：`gitcode-api-reference.md` 中"Token 从环境变量读取：`$GITCODE_API_TOKEN`" → "Token 已在 prompt 中给出（BOT_GITCODE_TOKEN / USER_GITCODE_TOKEN）"

## 4. 涉及文件汇总

### 源码（7 个）
1. `src/user/db.py` — 新增 `get_agent_bot_token`、`get_user_gitcode_token`
2. `src/agent/opencode.py` — 删 GITCODE_API_TOKEN 注入
3. `src/agent/codearts.py` — 删 GITCODE_API_TOKEN 注入
4. `src/agent/executor.py` — 删 GITCODE_API_TOKEN 注入，参数改名
5. `src/prompt_builder.py` — build_prompt/build_chat_prompt 注入 token
6. `src/api/event_pipeline.py` — 用 get_agent_bot_token + prompt 注入
7. `src/api/routes/chat.py` — 删 env_extra，prompt 注入用户 token

### IM 处理（3 个）
8. `src/im/handler_async.py` — 创建 chat_conversations + 用户 token
9. `src/im/handler.py` — 同上（同步兼容）
10. `src/im/vod_handler.py` / `vod_handler_async.py` — fallback 全局 bot token

### skill 文件（约 15 个）
- 35 处 `$GITCODE_API_TOKEN` → `$BOT_GITCODE_TOKEN` / `$USER_GITCODE_TOKEN`

### 测试（2 个）
- `tests/test_agent_executor.py` — `os.environ["GITCODE_API_TOKEN"]` 引用需更新
- `tests/test_chat_handler.py` — 同上

## 5. 实施顺序

1. **基础设施**：`src/user/db.py` 新增 `get_agent_bot_token` / `get_user_gitcode_token`
2. **环境变量清理**：opencode.py / codearts.py / executor.py
3. **webhook/VOD 场景**：event_pipeline.py / vod_handler*.py 改用新函数 + prompt 注入
4. **chat 场景**：chat.py / prompt_builder build_chat_prompt + handler*.py 创建 chat_conversations
5. **skill 文件**：批量改名
6. **测试更新**：tests/ 两个文件
7. **验证**：语法检查 + 单测 + 部署 + 真实事件验证

## 6. 验证方案

1. **本地单测**：mock 各场景，验证
   - prompt 中含 `BOT_GITCODE_TOKEN=<bot token>`（webhook/VOD）
   - prompt 中含 `USER_GITCODE_TOKEN=<user token>`（chat）
   - 环境变量中无 `GITCODE_API_TOKEN`
2. **重放验证**：用服务器 seq=245/246 真实 payload 重放 run_pipeline，确认回声事件被跳过（echo 过滤已部署，commit `2a62023`）
3. **部署后真实验证**：在 Issue 上创建真实事件，确认
   - bot 打标签/评论的回声 webhook 不再记录为事件
   - bot 动作的操作者身份是 `hwc-dev-bot`（而非用户）

## 7. 风险与注意事项

- **token 泄漏风险（已接受）**：完整 token 直接写进 prompt 会落入 agent 日志/会话历史。当前 issue #6 的 `mZ__xQ` 泄漏正是此途径。已确认接受，通过"prompt 只给当前执行的正确 token + skill 强制用 `$BOT_GITCODE_TOKEN`/`$USER_GITCODE_TOKEN`"降低误用概率。
- **prompt 注入 token 不能进 event 的 result/agent_logs**：`_execute_agent` 记录 `agent_logs` 时需注意 token 不落库（agent_logs 只记录 tool_use 的 command，prompt 本身不进 events 表）。
- **skill 改名需彻底**：遗漏的 `$GITCODE_API_TOKEN` 会导致 agent 用空变量 → API 401。
- **IM 创建 chat_conversations 是新增行为**：需要确保重复消息不重复创建（幂等，按 user_id+im_channel_id 查询）。
