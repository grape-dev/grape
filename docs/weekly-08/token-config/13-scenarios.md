# 全部场景用例（1-31）

> 本文档从设计文档中提取全部 31 个场景用例，供测试参考。
> 每个场景包含初始状态和具体用例步骤。

## 场景索引

| 场景 | 分类 | 关键需求项 |
|------|------|-----------|
| 1 | 纯路径 B | R16, R20.1 |
| 2 | 路径 A | R15, R16, R20.1 |
| 3 | OAuth | R16, R18 |
| 4 | 健康检查 | R17, R21.3 |
| 5 | PAT 提示 | R17.1 |
| 6 | 集群 | R17.2.9, R20.27 |
| 7 | fail_task 协调 | R16.6, R20.19 |
| 8 | 旧 bot 移除 | R15.6 |
| 9 | 解绑清理 | R19.3 |
| 10 | GitHub PAT | R19.4 |
| 11 | 日志脱敏 | R19.1, R20.25 |
| 12 | 自循环 | R20.1 |
| 13 | fail_task 回帖 | R20.2 |
| 14 | 执行期间过期 | R20.3 |
| 15 | auto_upgrade | R20.4 |
| 16 | webhook 权限 | R20.5 |
| 17 | bot_usernames | R20.1 |
| 18 | 核心矛盾 | R20.1 |
| 19 | 用户触发 | R20.1 |
| 20 | 时序竞态 | R20.6.4 |
| 21 | 飞书 VOD | R20.9, R20.10 |
| 22 | Chat | R20.8 |
| 23 | 非 pipeline | R20.7, R20.11 |
| 24 | autopilot | R20.10 |
| 25 | Issues | R20.7.8 |
| 26 | webhook 验证 | R20.12, R20.13 |
| 27 | 建单 | R20.14 |
| 28 | error 检测 | R20.19 |
| 29 | GIT_ASKPASS | R20.24 |
| 30 | token 泄露 | R20.25 |
| 31 | 关联仓库 | R20.26 |
| 32 | task 排队期间 token 过期 | R21.12 |
| 33 | 飞书 sync GitHub 侧 TokenResolver | R20.7.3 |
| 34 | 飞书 Chat direct user_token | R20.28.1 |
| 35 | 飞书 VOD/Chat queue user_token | R20.28.4 |
| 36 | Chat agent 执行后 401 检测 | R20.8.1 |
| 37 | 飞书 Chat agent 执行后 401 检测 | R20.8.5 |
| 38 | 飞书 VOD handler direct 执行后 401 标记 | R20.9.5 |
| 39 | Chat _execute_chat_task direct 执行后 401 标记 | R20.8.1 |
| 40 | Chat send_stream agent_worker 执行后 401 标记 | R20.8.6 |
| 41 | Chat ChatTaskManager 执行后 401 标记 | R20.8.7 |
| 42 | webhook pipeline direct 执行后 401 标记 | R20.8a.1 |
| 43 | event dialogue direct 执行后 401 标记 | R20.8b.1 |

---

详细场景用例内容请参考原始设计文档 `docs/weekly-08/repo-token-config-design.md` 第十章，包含每个场景的初始状态、用例编号、逐步骤描述。

### 场景 32 摘要：task 排队期间 token 过期

**初始状态**：路径 A 仓库（全局 bot token 有效），task 队列有 1 个排队中的 task。

#### 用例 32.1：排队期间全局 bot token 过期

1. T0: task 创建 → bot token 写入 task 行
2. T1: task 排队（status=queued）
3. T2: 健康检查标记 meta.global_*_bot_token_status=expired
4. T3: worker claim → 新鲜度检查（R21.12.1）→ _is_expired=True → 重新获取 → 短路返回空
5. task 行注入空串 → fail_task NO_TOKEN_AVAILABLE（R20.19.1）
6. 管理员重新配置 → 清除标记 → 下次 task 正常

#### 用例 32.2：排队期间仓库级 bot token 过期

1. T0: task 创建 → task 行 repo_bot_token_X
2. T2: 健康检查标记 managed_repos.bot_token_status=expired
3. T3: claim → 新鲜度检查 → _is_expired=True → 重新获取 → RepoBotToken 短路 → GlobalBotToken 有效 → 返回全局 bot token
4. task 行注入全局 bot token（R21.12.3）→ agent 正常（降级）

#### 用例 32.3：排队期间 token 未过期（正常路径）

1. claim → 新鲜度检查 _is_expired=False → 不重新获取 → 用原 token → 正常
2. 只查 DB 过期标记，不调 VCS API → 无延迟（R21.12.6）

#### 用例 32.4：路径 B 仓库排队期间用户 token 过期

1. task 行为空（路径 B 防过期落库）→ claim 时现有逻辑实时获取
2. 不受 R21.12 影响（R21.12.4）

本文档作为场景索引，供测试时快速定位场景与需求项的对应关系。实现时按各子文档（01-12）中的验收标准执行测试。

---

### 场景 33：飞书 sync GitHub 侧 TokenResolver 遗漏（R20.7.3）

**初始状态**：GitHub 仓库（`github:owner/repo`），全局 GitHub bot token 已配置（路径 A），飞书群已绑定仓库。

#### 用例 33.1：GitHub 仓库 sync mapping token 过期

1. 全局 GitHub bot token 被标记过期（`global_github_bot_token_status=expired`）
2. 飞书群成员变更 → 触发 `sync_repo_feishu_mapping`
3. 当前代码调 `get_github_token(repo_key=repo_key)` → 未走 TokenResolver → 不检查过期标记 → 返回过期 token
4. 用过期 token 调 GitHub API 拉协作者列表 → 401
5. sync 失败，无飞书卡片通知（因为 `get_github_token` 不走 TokenResolver 短路）
6. **期望行为**：应走 `Repo.bot_token`（内部委托 TokenResolver）→ 短路返回空 → 跳过 sync + 飞书卡片通知

#### 用例 33.2：GitHub 仓库 sync mapping 正常路径

1. 全局 GitHub bot token 有效
2. 飞书群成员变更 → 触发 `sync_repo_feishu_mapping`
3. 调 `Repo(repo_key).bot_token` → TokenResolver 返回有效 token
4. 拉取协作者列表成功 → 写 unified_user_mapping

### 场景 34：飞书 Chat handler_async direct 模式 user_token 遗漏（R20.28.1）

**初始状态**：路径 B 仓库（host_user_id > 0，用户 OAuth token），飞书用户通过飞书 Chat 发消息触发 agent direct 模式。

#### 用例 34.1：飞书 Chat direct 模式路径 B 仓库 git clone

1. 飞书用户发消息 → handler_async direct 模式启动
2. 当前代码 `user_token=""` → consume_stream_async 的 `user_token` 参数为空
3. env_extra 注入 `USER_GITCODE_TOKEN=""`
4. agent 执行 `git clone` → GIT_ASKPASS 读 `BOT_GITCODE_TOKEN`（路径 B 为空）→ fallback `USER_GITCODE_TOKEN`（也为空）→ fallback `BOT_TOKEN`（全局 bot token，路径 B 无权限）→ **clone 失败**
5. **期望行为**：启动前调 `get_user_token_with_refresh(username, user_id)` 获取用户 token → 注入 `user_token` 参数 → env 注入 `USER_GITCODE_TOKEN` 非空 → GIT_ASKPASS fallback 成功 → clone 成功

#### 用例 34.2：飞书 Chat direct 模式用户 token 过期

1. 用户 OAuth token 已过期（status=4）
2. `get_user_token_with_refresh` 返回空（或 best-effort 返回过期 token）
3. `user_token` 为空 → agent 无用户身份
4. agent 调 VCS API 403 → 返回友好提示"Token 已过期，请重新绑定"
5. 飞书发卡片通知用户

### 场景 35：飞书 VOD/Chat queue 模式 user_token 遗漏（R20.28.4）

**初始状态**：路径 B 仓库，飞书用户触发 VOD agent 或 Chat agent，引擎为远端 worker（queue 模式）。

#### 用例 35.1：飞书 VOD queue 模式路径 B 仓库

1. VOD handler 调 `resolve_repo_token(repo_key)` → 返回 TokenResult(level="user", token=用户 token)
2. 当前 enqueue_task 调用：`repo_gitcode_token=repo_token`，**未传 `user_gitcode_token`**
3. worker 收到 task → `repo_gitcode_token` 被注入为 `BOT_GITCODE_TOKEN`（但语义上是 user token）
4. GIT_ASKPASS 读 `BOT_GITCODE_TOKEN`（非空，是 user token 值）→ clone 成功（值正确但语义错误）
5. **问题**：agent 侧无法区分 bot token 和 user token——如果 skill 代码检查 `BOT_*_TOKEN` 做权限判断，会用 user token 的权限范围（可能不正确）
6. **期望行为**：路径 B 仓库时 enqueue_task 传 `user_gitcode_token=用户 token`，`repo_gitcode_token=空`（与 pipeline 一致）

#### 用例 35.2：飞书 Chat queue 模式路径 B 仓库

1. Chat handler_async queue 模式 enqueue_task
2. 当前未传 `user_gitcode_token`
3. 同 35.1——语义混乱

### 场景 36：Chat agent 执行后 401 检测遗漏（R20.8.1 执行后时机）

**初始状态**：路径 B 仓库，Chat agent 通过 Web Chat send_message 启动，用户 token 在启动时有效。

#### 用例 36.1：Chat agent 执行中 token 过期

1. T0: send_message → `_resolve_gitcode_token` 获取用户 token（有效）→ agent 启动
2. T1: agent 执行中（耗时 5 分钟），用户 token 在 T0+3min 过期
3. T2: agent 调 VCS API → 401 → agent 返回 exit_code < 0，error 含 "401 unauthorized"
4. 当前代码：send_message 返回 agent error 文本，**未调 `detect_and_mark_expired`** → 用户只看到 agent 报错，不知道是 token 过期
5. **期望行为**：agent 返回后检测 exit_code < 0 + error 含 401 关键词 → 调 `detect_and_mark_expired` → 返回 error_code → Chat 界面显示"Token 已过期，请重新绑定"

#### 用例 36.2：Chat agent 执行后 401 但 token 有效

1. agent 返回 401 → 调 `detect_and_mark_expired` → token 验证有效（401 是权限不足）
2. 返回"权限不足或 Token 权限范围不够"

### 场景 37：飞书 Chat agent 执行后 401 检测遗漏（R20.8.5）

**初始状态**：路径 B 仓库，飞书用户通过飞书 Chat 触发 agent（direct 或 queue 模式）。

#### 用例 37.1：飞书 Chat direct 模式 agent 执行中 token 过期

1. T0: 飞书用户发消息 → handler_async direct 模式启动 agent
2. T1: agent 执行中 token 过期 → 401 → exit_code < 0
3. 当前代码：handler_async 返回 agent error 文本，**未调 `detect_and_mark_expired`** → 飞书用户只看到 agent 报错
4. **期望行为**：agent 返回后检测 exit_code < 0 + error 含 401 关键词 → 调 `detect_and_mark_expired` → 飞书发卡片通知"Token 已过期，请重新绑定"

#### 用例 37.2：飞书 Chat queue 模式 agent 执行后 401

1. queue 模式：task 完成 → `wait_task_terminal` 返回 exit_code < 0
2. 同 37.1——检测 401 → detect_and_mark_expired → 飞书卡片通知

### 场景 38：飞书 VOD handler direct 模式执行后 401 标记（R20.9.5）

**初始状态**：路径 B 仓库（host_user_id > 0，用户 OAuth token），飞书用户触发 VOD agent direct 模式，token 在启动时有效。

**核心矛盾**：VOD handler direct 模式走 `consume_stream_async`，不经过 `fail_task`。如果 agent 执行中 token 过期（401），不调 `detect_and_mark_expired` 标记 → 下次 VOD agent 启动时 TokenResolver 不短路 → 继续用过期 token → 401 → 不标记 → 死循环，直到健康检查或用户手动重新绑定。

#### 用例 38.1：VOD direct 模式 agent 执行中 token 过期

1. T0: VOD handler 调 `resolve_repo_token(repo_key)` → 返回有效 token → agent 启动
2. T1: agent 执行中 token 过期 → agent 调 VCS API 401 → exit_code != 0，final_text 含 "401 unauthorized"
3. 当前代码：返回 final_text 给调用方 → `reply_card` 展示原始错误 → **未调 `detect_and_mark_expired`**
4. T2: 用户再次发消息 → VOD handler 调 `resolve_repo_token` → TokenResolver 不短路（未标记）→ 返回过期 token → agent 再次 401 → 死循环
5. **期望行为**：agent 返回后检测 exit_code != 0 + final_text 含 401 关键词 → 调 `detect_and_mark_expired` → 标记 token 过期 → 下次 VOD agent 启动时 TokenResolver 短路返回空 → VOD handler 返回"Token 已过期"提示 → 不启动 agent

#### 用例 38.2：VOD direct 模式 agent 401 但 token 有效

1. agent 返回 401 → 调 `detect_and_mark_expired` → token 验证有效（401 是权限不足）
2. 不标记过期 → final_text 原样返回 → 正常行为

### 场景 39：Chat _execute_chat_task direct 模式执行后 401 标记（R20.8.1）

**初始状态**：路径 B 仓库，Chat 通过 `_execute_chat_task`（旧版异步模式）执行 agent direct 模式，token 在启动时有效。

**核心矛盾**：同场景 38——`_execute_chat_task` direct 模式走 `stream_agent`，不经过 `fail_task`。agent 执行中 token 过期不标记 → 死循环。

#### 用例 39.1：Chat _execute_chat_task direct 模式 agent 执行中 token 过期

1. T0: `_execute_chat_task` → `stream_agent` 启动
2. T1: agent 执行中 token 过期 → 401 → error 事件 / final_text 含 "401 unauthorized"
3. 当前代码：`final_text` 写入 `add_message` → **未调 `detect_and_mark_expired`**
4. T2: 用户再次发消息 → TokenResolver 不短路 → 返回过期 token → agent 再次 401 → 死循环
5. **期望行为**：agent 返回后检测 final_text 含 401 关键词 → 调 `detect_and_mark_expired` → 标记 → 下次 Chat 启动时 TokenResolver 短路 → 返回"Token 已过期"提示

#### 用例 39.2：Chat _execute_chat_task queue 模式（标记已覆盖）

1. queue 模式：task failed → worker 侧 `fail_task` 内已有 `detect_and_mark_expired` 调用
2. 标记已覆盖 → 无遗漏

### 场景 40：Chat send_stream agent_worker 执行后 401 标记（R20.8.6）

**初始状态**：路径 B 仓库，用户通过 Web Chat SSE 流模式发送消息，token 在启动时有效。

**核心矛盾**：send_stream 的 agent_worker 在后台线程运行，通过 SSE 队列投递事件。agent 执行中 token 过期（401）时，SSE 流中不会出现 token 原文（流式输出是思考过程和文本），所以不需要提示。但**不标记会导致死循环**：用户看到 SSE error 事件后立即重发 send_stream → 启动前检测不命中（token 未被标记）→ agent 再次 401 → 不标记 → 死循环，直到健康检查或用户用非 stream 模式发消息（send_message 会标记）。

#### 用例 40.1：send_stream agent 执行中 token 过期

1. T0: 用户发消息 → send_stream → agent_worker 启动 → SSE 流开始
2. T1: agent 执行中 token 过期 → agent 调 VCS API 401 → done 事件 exit_code != 0，errors/stderr 含 "401 unauthorized"
3. 当前代码：agent_worker finally 块写入 final_text 到 DB → **未调 `detect_and_mark_expired`**
4. T2: 用户看到 SSE error 事件 → 立即重发 send_stream
5. T3: send_stream 启动前检测 → TokenResolver 不短路（未标记）→ 返回过期 token → agent 再次 401 → 死循环
6. **期望行为**：agent_worker finally 块检测 done 事件的 errors/stderr 含 401 关键词 → 调 `detect_and_mark_expired` → 标记 token 过期 → 用户重发时启动前检测命中 → 返回"Token 已过期"提示 → 不启动 agent

#### 用例 40.2：send_stream agent 401 但 token 有效

1. agent done 事件 exit_code != 0 + errors 含 "401" → 调 `detect_and_mark_expired`
2. token 验证有效（401 是权限不足）→ 不标记 → SSE 流正常结束
3. 用户重发 → 启动前检测不命中（未标记）→ 正常启动 agent

### 场景 41：Chat ChatTaskManager 执行后 401 标记（R20.8.7）

**初始状态**：路径 B 仓库，用户通过 Web Chat 发消息，ChatTaskManager（新版异步任务管理器）执行 agent。

**核心矛盾**：ChatTaskManager 替代旧版 _execute_chat_task，stream_chat_agent 的 finally 块写入 DB，没有 detect_and_mark_expired 调用。不标记 → 死循环（同场景 40 逻辑）。

#### 用例 41.1：ChatTaskManager agent 执行中 token 过期

1. T0: 用户发消息 → ChatTaskManager → stream_chat_agent 启动
2. T1: agent 执行中 token 过期 → 401 → done 事件 exit_code != 0，errors/stderr 含 "401 unauthorized"
3. 当前代码：finally 块写入 final_text 到 DB → **未调 `detect_and_mark_expired`**
4. T2: 用户重发 → 启动前检测不命中 → agent 再次 401 → 死循环
5. **期望行为**：finally 块检测 done 事件的 errors/stderr 含 401 关键词 → 调 `detect_and_mark_expired` → 标记 → 用户重发时启动前检测命中

### 场景 42：webhook pipeline direct 执行后 401 标记（R20.8a.1）

**初始状态**：路径 A 仓库（全局 bot token），webhook 触发 pipeline，coding/sre agent 通过 direct 模式执行 git clone/push。

**核心矛盾**：pipeline direct 模式 exec_agent done 事件 exit_code != 0 时走 `update_task_status`（不经过 `fail_task`），没有 `detect_and_mark_expired` 调用。coding/sre agent 做 git clone/push，token 过期会导致 401。不标记 → 下次 webhook 触发 pipeline 时 TokenResolver 不短路 → 死循环。

#### 用例 42.1：pipeline direct coding agent git push 401

1. T0: webhook 触发 → pipeline → exec_agent → stream_devops_agent → coding agent 启动
2. T1: coding agent git push → 401 → done 事件 exit_code != 0，errors/stderr 含 "fatal: authentication"
3. 当前代码：exec_agent 走 `update_task_status(task_id, "failed")` → **未调 `detect_and_mark_expired`**
4. T2: 下次 webhook 触发 → pipeline → TokenResolver 不短路 → 返回过期 token → agent 再次 401 → 死循环
5. **期望行为**：exec_agent done 事件 exit_code != 0 时，基于 errors/stderr 判断 401 → 调 `detect_and_mark_expired` → 标记 → 下次 pipeline 触发时 TokenResolver 短路

#### 用例 42.2：pipeline direct queue 模式（标记已覆盖）

1. queue 模式：task failed → worker 侧 `fail_task` 内已有 `detect_and_mark_expired` 调用
2. 标记已覆盖 → 无遗漏

### 场景 43：event dialogue direct 执行后 401 标记（R20.8b.1）

**初始状态**：路径 B 仓库，用户在 Issue 上发评论触发 event dialogue，agent 通过 direct 模式执行。

**核心矛盾**：event dialogue direct 模式 stream_devops_agent 的 finally 块写入 DB，没有 `detect_and_mark_expired` 调用。event dialogue agent 可能调 VCS API（读 issue 评论等），token 过期会 401。不标记 → 死循环。

#### 用例 43.1：event dialogue direct agent 执行中 token 过期

1. T0: 用户评论 → event dialogue → stream_devops_agent 启动
2. T1: agent 调 VCS API 读 issue 评论 → 401 → done 事件 exit_code != 0，errors/stderr 含 "401 unauthorized"
3. 当前代码：finally 块写入 _worker_final_text 到 DB → **未调 `detect_and_mark_expired`**
4. T2: 用户再次评论 → event dialogue → TokenResolver 不短路 → agent 再次 401 → 死循环
5. **期望行为**：finally 块检测 done 事件的 errors/stderr 含 401 关键词 → 调 `detect_and_mark_expired` → 标记 → 下次 event dialogue 启动时 TokenResolver 短路

#### 用例 43.2：event dialogue queue 模式（标记已覆盖）

1. queue 模式：task failed → worker 侧 `fail_task` 内已有 `detect_and_mark_expired` 调用
2. 标记已覆盖 → 无遗漏
