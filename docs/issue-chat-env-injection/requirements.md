# Chat（Web 直连 / 飞书直连）未注入 REPO_* / ISSUE_NUMBER / GRAPE_SERVER_URL — 需求 + 实现规格

- 目标分支：`weekly-09-IM`（本单跑在「马岩松的专属开发流水线-2」，work_branch=weekly-09-IM）
- 参考基线：`origin/weekly-09-IM` @ `e3214174e`（chat 引擎代码在本分支最全；`src/agent/executor.py::_assemble_env` 与 `src/api/chat_session_env.py` 在 weekly-mys 上逐字相同，已交叉核对）
- 行号仅参考，**一律以函数名/字段名锚定**

## 背景（已只读核实，不是推测）

`grape-skills` 仓的 chat 技能（`skills/chat/im/chat/SKILL.md` §环境变量）写明这些变量「服务端已注入，直接使用」：
`GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` / `REPO_FULL_NAME` / `REPO_OWNER` / `REPO_NAME` / `ACCOUNT` / `ISSUE_NUMBER` / `PR_NUMBER` / `AGENT_NAME` / `DEVOPS_EVENT_TYPE`。

但平台侧 chat 执行链实际只注入：

1. `src/api/chat_session_env.py::chat_session_env_extra(user_id, conv_id, repo_key)` → 恒 `USER_ID`、`CONVERSATION_ID`；
   真仓库时再 `GRAPE_SESSION_TOKEN`（tk_）+ `GRAPE_CLI_MODE=WORKER`。**没有**任何 REPO_* / ISSUE_NUMBER / GRAPE_SERVER_URL。
2. `src/agent/executor.py::_assemble_env(kind="chat")` 只补 `VCS_API_BASE` / `VCS_NAME` + 用户云凭据；
   对比 devops 路径（`src/api/event_pipeline.py::build_task_env`）明确注入了 `REPO_FULL_NAME/REPO_OWNER/REPO_NAME/ACCOUNT/GRAPE_SERVER_URL` 与 `ISSUE_NUMBER/PR_NUMBER`。
3. `src/api/routes/chat.py` 共 8 处 `env_extra`：3 处走 helper（传参只有 user_id/conv_id/repo_key），
   **5 处直接写裸 dict** `{"USER_ID": ..., "CONVERSATION_ID": ...}`（`send` / `stream` / `_enqueue_chat` 等 enqueue 路径）。
4. `src/im/handler_async.py` 2 处（enqueue 1799 / direct 1840）走 helper，同样缺参。

后果：agent 按技能写 `grape git-repo issues get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER"` 时，
三个变量为空 → 请求路径出现空段 → 评论/查询接口 404；`GRAPE_SERVER_URL` 缺失 → CLI 回退 `config.json` / `localhost:8000` 基址 → 同样 404。
（用户环境里 `GRAPE_SESSION_TOKEN` 是有的，所以失败点是"路径段为空"和"基址错"，不是鉴权。）

## 已定口径（主人已确认）

1. 修「chat 直连缺注入」这一个根因：helper 统一注入，且所有 chat 调用点收敛到 helper
2. `GRAPE_SERVER_URL` 的**取值**复用 `src/prompt_builder.py::_resolve_server_url(runtime_id)`（与 devops 任务同源），
   **本单不处理**「URL 指向 runtime 端口」的历史语义问题（#1522 / #1645 / #1690 / #1726 / #1731 家族另单追踪）
3. 一般对话（无仓库 / `chat:{id}` 伪仓库）只注入 `USER_ID` / `CONVERSATION_ID` / `GRAPE_SERVER_URL`，
   **不注入** REPO_* / ISSUE_NUMBER（键不出现，而不是空串）
4. 真仓库用 `src/repo_key.py::split_owner_repo` 剥 provider 前缀，口径与 `build_task_env` 完全一致
5. `ISSUE_NUMBER` 只在任务会话（`conv_type == "task"`）且值为纯数字时注入（沿用 Issue #528 口径：非数字/占位值不注入）
6. `ACCOUNT` / `PR_NUMBER` 不在本单范围（chat 流程未使用；`ACCOUNT` 在 chat 语义下应等于用户本人，需单独确认）

## 期望结果

- Web 直连 chat / 飞书直连 chat / 远端 worker 队列 chat 三条路径下，真仓库任务会话都能拿到
  `REPO_FULL_NAME` / `REPO_OWNER` / `REPO_NAME` / `ISSUE_NUMBER`（数字时）/ `GRAPE_SERVER_URL`，
  技能里写死的命令可直接跑通（不再 404）
- 一般对话行为不变（除多一个 `GRAPE_SERVER_URL`）
- 以后新增 chat 入口忘记注入时，`_assemble_env(kind="chat")` 有兜底补齐，不会再出现"技能说有、实际没有"

---

## 改动范围

严格 3 组共 11 项；多改一处即打回。

### A. helper 扩展（`src/api/chat_session_env.py`）

- A1. `chat_session_env_extra(user_id, conv_id, repo_key, *, runtime_id: int = 0, issue_number: str = "")`：
  签名保持向后兼容（现有三参数调用不报错）。
- A2. 真仓库（`repo_key` 非空且不以 `chat:` 开头）时注入 `REPO_FULL_NAME` / `REPO_OWNER` / `REPO_NAME`：
  用 `from src.repo_key import split_owner_repo`，`owner, name = split_owner_repo(repo_key)`；
  `REPO_FULL_NAME = f"{owner}/{name}"`（**无 provider 前缀**），与 `build_task_env` 一致；解析异常只 warning、不抛。
- A3. 恒注入 `GRAPE_SERVER_URL`（无论真仓库/伪仓库）：`from src.prompt_builder import _resolve_server_url`，
  值 = `_resolve_server_url(runtime_id)`；异常时 warning 并跳过该键（不抛，聊天不因 URL 解析失败中断）。
- A4. `ISSUE_NUMBER`：`issue_number` 入参为空且 `conv_id` 非空时，用 `get_conversation(conv_id)` 兜底取
  `conv["issue_number"]`（**仅** `conv_type == "task"` 时）；`str(x).lstrip("#")` 后必须 `isdigit()` 才注入，否则不注入该键。
- A5. 伪仓库/空 repo：不得注入 `REPO_*` / `ISSUE_NUMBER` 任一键（不含空串），`USER_ID` / `CONVERSATION_ID` / `GRAPE_SERVER_URL` 保留；
  tk_ / `GRAPE_CLI_MODE=WORKER` 行为一字不改。

### B. 调用点收敛（`src/api/routes/chat.py`、`src/im/handler_async.py`）

- B1. `chat.py` 中 5 处裸 `env_extra={"USER_ID": ..., "CONVERSATION_ID": ...}`（enqueue 路径）全部改为
  `chat_session_env_extra(user["id"], conv_id, repo_key, runtime_id=<该处已解析的 runtime_id>, issue_number=<conv 的 issue_number，若有>)`；
  改完后 `grep -n 'env_extra={"USER_ID"' src/api/routes/chat.py` 必须 0 命中。
- B2. `chat.py` 中已有 3 处 helper 调用补齐 `runtime_id` / `issue_number` 两个实参（值取各自作用域内已存在的 `rid`/`cfg.runtime_id` 与 `conv`）。
- B3. `src/im/handler_async.py` 2 处 helper 调用（enqueue + direct）补 `runtime_id` / `issue_number`，`IM_CHAT_ID` 追加逻辑保持不变。
- B4. helper 之外不得再出现任何 chat env 组装代码（含把名值写进 prompt 让 agent 自己 grep 的做法）。

### C. 执行器兜底（`src/agent/executor.py`）

- C1. `_assemble_env` 的 `kind == "chat"` 分支末尾追加兜底（**仅在键缺失时**，`env_extra` 显式值优先，禁止覆盖）：
  - 真仓库且 `REPO_OWNER` 不在 env → 用 `split_owner_repo` 补 `REPO_FULL_NAME/REPO_OWNER/REPO_NAME`
  - `GRAPE_SERVER_URL` 不在 env → 补 `f"http://127.0.0.1:{_local_port()}"`（直连语义，`_local_port` 取 `src/prompt_builder._local_port`）
- C2. 兜底只作用于 `kind == "chat"`；devops/issue/qa/sre 分支（`elif bot_token` 及其后）一行不动。

### D. 测试（新增/扩展现有文件）

- D1. 扩展 `tests/test_chat_session_env.py`：真仓库四变量、github 前缀剥离、伪仓库不注入、task 会话数字 ISSUE_NUMBER、非数字不注入、`_resolve_server_url` 抛异常降级、tk_ 行为不回归。
- D2. 新增 `tests/test_chat_env_injection_alignment.py`：静态断言 `chat.py` 无裸 `env_extra` 组装（0 命中）+ `_assemble_env` 兜底补齐/不覆盖显式值 + devops 分支不变。

---

## 明确禁止

1. 不改 `_resolve_server_url` / `_local_port` 的端口与地址语义（`#1731`/`#1522`/`#1645`/`#1690`/`#1726` 家族另单处理；本单只调用它）
2. 不改 `event_pipeline.build_task_env` / `_enqueue_pipeline_task` / `_inject_scene_env` 等 devops 侧 env 组装
3. 不改 `grape-skills` 仓的 chat `SKILL.md`（跨仓，本单不动；平台对齐技能声明即可）
4. 不注入 `ACCOUNT` / `PR_NUMBER` / `BOT_*` / `USER_*` token（P2 token 边界与范围外变量）
5. 不改 tk_ 会话 token 的签发逻辑（`session_key=conv:{conv_id}`、`agent_type=chat`、作用域一字不改）
6. 不改 chat prompt 构建（`build_issue_chat_prompt` / `build_general_chat_prompt`）、不改 chat 前端、不改 DB schema
7. 不把变量写进 `os.environ`（并发串号）；一律经 `env_extra` 传递
8. 不顺手重构 `chat.py` 其它部分、不改日志级别、不新增依赖

## 反诱人错解（别这么做）

- ❌ 只修 Web chat 三处 helper 调用（飞书 IM 同 helper；5 处裸 dict 还在 → 依然漏）
- ❌ 在 prompt 里追加「仓库是 owner/repo」让 agent 自己解析（技能按 env 取，prompt 里写不算注入）
- ❌ 注入 `REPO_OWNER=""` 空串（`${REPO_OWNER:-...}` 兜底失效，且照样拼出空路径段）
- ❌ 顺手把 `GRAPE_SERVER_URL` 改成 `RUNTIME_SERVER_URL` 或公网域名（超范围，且与 5 个历史单耦合）
- ❌ 兜底写成无条件覆盖（会打掉远端 worker 显式传入的地址）

---

## 验收标准（可执行，逐条断言）

1. **基线**：`git log --oneline -1` 记 SHA；`grep -n 'def chat_session_env_extra' src/api/chat_session_env.py` 命中新签名（含 `runtime_id`、`issue_number`）。
2. **真仓库注入**：`chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo", runtime_id=0)` →
   `REPO_OWNER=="owner"`、`REPO_NAME=="repo"`、`REPO_FULL_NAME=="owner/repo"`（无 `gitcode:` 前缀）、`GRAPE_SERVER_URL` 非空。
3. **github 前缀**：`"github:o/r"` → `REPO_FULL_NAME=="o/r"`、`REPO_OWNER=="o"`。
4. **伪仓库/空**：`""`、`None`、`"chat:1"` → 三个 REPO_* 与 `ISSUE_NUMBER` 均 **不在** dict（`not in`），`GRAPE_SERVER_URL` 在，`USER_ID`/`CONVERSATION_ID` 不变。
5. **ISSUE_NUMBER**：任务会话 `issue_number=1735` → `"1735"`；`issue_number="#1735"` → `"1735"`；`issue_number="3f40dd97-ab9a-..."`（非数字）→ 键不存在；`conv_type != "task"` → 键不存在。
6. **降级**：`patch("src.prompt_builder._resolve_server_url", side_effect=Exception("x"))` → helper 不抛，REPO_* 等其他键仍在，缺 `GRAPE_SERVER_URL`。
7. **兜底**：模拟 `env_extra` 无 REPO_OWNER → `_assemble_env(kind="chat", repo_key="gitcode:o/r")` 后三键齐全；
   `env_extra={"REPO_OWNER": "explicit"}` → 保持 `"explicit"` 不被覆盖；`kind="devops"` 调用结果与改动前逐键一致（可 dump 对比）。
8. **命令可用性**（不连真实仓库）：用注入后的 env 渲染
   `grape git-repo issues get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER"` → 断言命令串中不含 `--owner ""` / `--repo ""` / 路径出现 `//`；
   并断言 `GRAPE_SERVER_URL` 非空（含 `http` 前缀）。
9. **静态收敛**：`grep -n 'env_extra={"USER_ID"' src/api/routes/chat.py` → 0 命中；`grep -c 'chat_session_env_extra' src/api/routes/chat.py` ≥ 8。
10. **回归**：`tests/test_chat_session_env.py`、`tests/test_chat_direct_session_token.py`、`tests/test_feishu_task_chat_session_token.py`、`tests/test_chat_skill.py` 全绿；`tests/test_issue_528_pr_sub_env_injection.py`、`tests/test_issue_573_bot_agent_env_injection.py` 全绿（devops 侧零回归）。
11. **声明对齐**：输出键集合 ⊇ chat SKILL 声明的 `{REPO_FULL_NAME, REPO_OWNER, REPO_NAME, ISSUE_NUMBER, GRAPE_SERVER_URL}`（断言写在测试里；技能文档本身不改）。
12. **线上观察（只写步骤，不执行）**：Web chat 打开一个任务会话 → 发「查一下 #N 状态」→ 日志中该任务 env 含五个变量 →
   agent 输出的 grape 命令无空参数 → 评论/查询 200；再用一般对话（无仓库）复测无 REPO_*。

## 参考资料

- 关键锚点（函数名优先）：`src/api/chat_session_env.py::chat_session_env_extra`；`src/api/routes/chat.py` 的 8 处 `env_extra`（3 处 helper + 5 处裸 dict：`send` / `stream` / `_enqueue_chat` / 反馈入队 / worker 轮询入队）；`src/im/handler_async.py` 两处 helper；`src/agent/executor.py::_assemble_env` / `stream_chat_agent` / `stream_agent`；`src/api/event_pipeline.py::build_task_env`（对照口径，不改）；`src/prompt_builder.py::_resolve_server_url` / `_local_port`（调用，不改）；`src/repo_key.py::split_owner_repo`
- 不可破坏的现存保护：tk_ 会话 token 作用域（`verify_session_token_for_repo` 正/反例）；`GRAPE_CLI_MODE=WORKER` 仅真仓库；P2 边界（平台 VCS token 不进 agent env）；Issue #528 非数字 issue_id 不注入
- 同族历史单（**只做交叉引用，不要在本单顺手修**）：#1522、#1645、#1690、#1726、#1731（均为 `GRAPE_SERVER_URL` 值指向 runtime 端口 / 缺业务端点的语义问题）
- 测试模板：`tests/test_chat_session_env.py`（helper 单测）、`tests/test_issue_573_bot_agent_env_injection.py`（注入 + 不覆盖断言）、`tests/js/test_issue_461_scene_deploy_url.js`（静态断言写法可参考）
- 技能声明来源（只读）：`grape-skills` 仓 `skills/chat/im/chat/SKILL.md` §环境变量
- 数据/环境口径：单测用 sqlite 临时库 + 打桩，不压现网、不连远端 MySQL、不跑真实 grape CLI
- 规模预估：`chat_session_env.py` ≈ +45 行；`chat.py` 8 处 ≈ +15 行；`handler_async.py` ≈ +4 行；`executor._assemble_env` ≈ +20 行；测试 ≈ +200 行