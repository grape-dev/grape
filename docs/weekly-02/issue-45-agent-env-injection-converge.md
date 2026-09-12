# Issue-45 设计文档：Agent 环境变量注入链路与敏感凭证收敛（先增后减）

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature（安全收敛）| P1 | 状态：spec-ready
> 分支：`weekly-02`
> 日期：2026-09-08（2026-09-08 调整：改为**先增后减**——先只叠加 `GRAPE_SESSION_TOKEN`，现网验证功能正常后再逐一移除 gitcode token）
> 范围：梳理 agent 任务环境变量注入**现状链路**；策略为**增量式收敛**——① 保持当前注入变量不变，仅在其上新增 `GRAPE_SESSION_TOKEN`；② 后续现网验证功能正常后，再逐一移除 `BOT_GITCODE_TOKEN` env / askpass 等敏感面（对应执行规划 M3.4，随 F-2 技能迁移逐步推进）。

## 关联文档

- 执行规划（M3.4 协作点）：`docs/weekly-02/issue-45-execution-plan.md`
- Git 仓库统一接口与方案③（CLI 本地 git、token 进程内）：`docs/weekly-02/issue-45-git-repos-api-design.md`
- CLI git 透传实现：`docs/weekly-02/issue-45-cli-design.md` / `cli/README.md`
- 认证 token 语义：`docs/weekly-02/issue-45-api-token-design.md`
- 已有 token env 互斥注入设计：`docs/weekly-01/user-gitcode-token-mis-inject-fix-design.md` / `docs/new-version/gitcode-token-injection-refactor.md`

---

## 一、现状：环境变量注入链路（已核实）

### 1.1 服务端打包（任务入队，`enqueue_task`）

`src/runtime/service.py` `enqueue_task` 组装 `env`，**落库快照** `agent_tasks.env_extra`（JSON）：

| 来源 | 变量 |
|------|------|
| 事件上下文 `env_extra`（`event_pipeline._enqueue_pipeline_task`/`_execute_agent`） | `REPO_FULL_NAME`、`ACCOUNT`、`GRAPE_SERVER_URL`、云 AK/SK、`GRAPE_WORK_BRANCH`、事件 `_env_extra`、`BOT_USERNAME`（coding）、`ISSUE_NUMBER`、`PR_NUMBER` |
| `_collect_server_env`（`service.py:193`） | 用户云凭据 → `HUAWEICLOUD_SDK_AK/SK`、`CODEARTS_CLI_AK/SK`、自定义 `{K}=v` |
| **`GRAPE_SESSION_TOKEN`**（M2） | 会话级临时 token（`tk_`），**注入点见下方**，供 agent 调对外 API |

### 1.2 执行侧注入进程（两条路径殊途同归）

```
本地 direct：_execute_agent → stream_devops_agent → _assemble_env(env)
远端 worker：claim 任务 → env = json.loads(agent_tasks.env_extra) → stream_runtime → _assemble_env(env)
        │
        ▼
env = os.environ.copy() + 后端默认(DEVOPS_SKILLS_DIR/NO_COLOR/TERM/HERMES_YOLO_MODE)
   + GitCode token（chat→USER_GITCODE_TOKEN；devops→BOT_GITCODE_TOKEN）
   + env_extra.update(...)   ← 覆盖上表变量（含 GRAPE_SESSION_TOKEN）
        │
        ▼
HermesACPBackend.stream_execute(env=env) → subprocess.Popen(..., env=proc_env)
```

**`GRAPE_SESSION_TOKEN` 注入点（就两处，均已上线 M2）**：

| 注入点 | 代码 | 覆盖路径 | 时机 |
|--------|------|----------|------|
| **① 入队打包（主路径）** | `src/runtime/service.py:422`（`enqueue_task` 内 `env["GRAPE_SESSION_TOKEN"] = issue_session_token(...)`） | 所有经 `enqueue_task` 的任务（pipeline / chat / 远端 worker）——签发后写入 `agent_tasks.env_extra` 落库 | 任务入队时 |
| **② direct 本地直执行** | `src/api/event_pipeline.py:1875`（`_execute_agent_inner` 内 `env_extra["GRAPE_SESSION_TOKEN"] = ...`） | 本机 direct（`_execute_agent` 流式）路径，不走 enqueue 也注入 | 执行开始时 |

> chat 任务同样走 **①**（chat.py 的 `_enqueue_chat` → `enqueue_task` → service.py L422 统一签发）；env 组装为 `USER_ID`+`CONVERSATION_ID`+`GRAPE_SESSION_TOKEN` 后随任务下发（见 1.3）。

- 组装点：`src/agent/executor.py` `_assemble_env`（L209+）
- 最终注入：`src/agent/hermes_acp.py:99-124` `proc_env = dict(env)` → `subprocess.Popen(env=proc_env)`
- worker 侧：`src/runtime/agent.py:947-949` `env = json.loads(task["env_extra"])`，`L1167 env_extra=env`

### 1.3 chat agent 注入流程（`stream_chat_agent`）

chat 与 devops 事件共用底层 `stream_agent`，但**签名/语义严格隔离**（`executor.py`）：

```
chat 触发（chat.py：/send /send/stream /submit …）
   │  _enqueue_chat → enqueue_task(
   │      repo_key="chat:{user_id}", agent_type="chat", user_id=user_id,
   │      env_extra={"USER_ID":…, "CONVERSATION_ID":…})   ← GRAPE_SESSION_TOKEN 由 service.py:422 统一签发（注入点①）
   ▼
执行侧：stream_chat_agent(user_token=…, user_id=…, env_extra=…)
   │  （签名**不含 bot_token**——从调用方杜绝「以仓库 bot token 冒充用户身份」）
   ▼
stream_agent(event_type="chat", bot_token="", user_token=user_token)
   ▼
_assemble_env(env, kind="chat"):
   │  ✓ 只注入 USER_GITCODE_TOKEN（无则 DB 兜底查用户 token；**不注入 BOT_GITCODE_TOKEN**）
   │  ✓ user_id>0 → 注入用户云凭据（HUAWEICLOUD_SDK_AK/SK、CODEARTS 等）
   │  ✓ env.update(env_extra) → USER_ID / CONVERSATION_ID / GRAPE_SESSION_TOKEN（会话级）
   ▼
Popen(env=proc_env) → agent 可见 $USER_GITCODE_TOKEN、$GRAPE_SESSION_TOKEN、
                         $USER_ID、$CONVERSATION_ID …
```

**chat 与 devops 的关键差异**：

| 维度 | chat（`stream_chat_agent`） | devops 事件（`stream_devops_agent`） |
|------|------------------------------|--------------------------------------|
| 身份语义 | **用户身份**（只注入用户 token） | **仓库身份**（只注入 bot token） |
| GitCode token | `USER_GITCODE_TOKEN` | `BOT_GITCODE_TOKEN`（本次收敛对象） |
| token 指针语法 | 签名不含 `bot_token` | 签名不含 `user_token`（见 `user-gitcode-token-mis-inject-fix-design` §十） |
| 特有 env_extra | `USER_ID`、`CONVERSATION_ID` | `REPO_FULL_NAME`、`ACCOUNT`、`ISSUE_NUMBER`、`PR_NUMBER`… |

> 收敛后：chat 的 `USER_GITCODE_TOKEN` **保留**（用户身份语义，与 bot 互斥）；devops 的 `BOT_GITCODE_TOKEN` 移除（改由 CLI/API 提供）。

### 1.4 核对方法

```bash
# 任务 env 快照
sqlite3 data/sessions.db "SELECT env_extra FROM agent_tasks WHERE id='t_xxx';"
# 运行中 agent 进程 env
ps eww <pid>            # 或 cat /proc/<pid>/environ
```

---

## 二、当前注入变量清单（按敏感度分级）

| 级别 | 变量 | 说明 |
|------|------|------|
| 任务上下文 | `REPO_FULL_NAME` / `ACCOUNT` / `GRAPE_SERVER_URL` / `GRAPE_WORK_BRANCH` / `ISSUE_NUMBER` / `PR_NUMBER` / `BOT_USERNAME` / `DEVOPS_EVENT_TYPE` / `<chat特有>` `USER_ID` `CONVERSATION_ID` | 公开字段，可保留 |
| 授权通道 | `GRAPE_SESSION_TOKEN`（`tk_`） | 短生命周期、随任务/会话失效，**设计内保留**（agent 调对外 API 的唯一凭据） |
| **平台凭证（敏感）** | `BOT_GITCODE_TOKEN` | 仓库 bot token，长期有效；**本次收敛对象** |
| **平台凭证（敏感）** | `USER_GITCODE_TOKEN` | 用户个人 token；chat 语义按用户身份执行——**保留**（chat 专用，与 bot 互斥） |
| **云凭证（敏感）** | `HUAWEICLOUD_SDK_AK/SK`、`CODEARTS_CLI_AK/SK`、自定义 | 用户云 AK/SK，agent 需用它调云 API——**保留**（后续可加白名单/开关） |
| 事件自定义 | `evt["_env_extra"]` 任意键 | 上游注入，评审把关 |

---

## 三、敏感面与风险

| 风险点 | 现状 | 风险 |
|--------|------|------|
| **`BOT_GITCODE_TOKEN` env 注入**（`executor.py:235` devops 分支） | 明文进 agent `os.environ`，技能 `$BOT_GITCODE_TOKEN` 直连 gitcode | ⚠️ 高：agent 随时可读、可被技能泄漏到日志/评论；长期有效 |
| **askpass 脚本落盘**（`runtime/repogit._write_askpass`，`L168`） | 在 agent 工作区写 `.git-askpass.sh`（`echo "$BOT_GITCODE_TOKEN"`） | ⚠️ 高：工作区 `cat` 可读（即使只读 env 引用，env 有值即泄） |
| **git `-c http.extraheader`**（repogit 注入路径） | 平台 token 的 base64 出现在 git 命令行 | ⚠️ 中：`ps` 可见；已被方案③（CLI 进程内）替代 |
| worker 隔离 | bwrap sandbox 仅锁目录/能力，**env 不脱敏** | agent 看到注入的全部凭证 |

---

## 四、收敛路径（先增后减，逐步推进）

> **核心原则**：**先保证现网无感**——现有注入的 env 一个都不动，只在其上**新增** `GRAPE_SESSION_TOKEN`；
> 待现网验证功能正常后，再**逐一**移除 gitcode token，每步独立验证、可回滚。

### 4.1 阶段 1——保持现状 + 仅新增 `GRAPE_SESSION_TOKEN`（已实现，M2）

- **现有注入变量保持不变**：`BOT_GITCODE_TOKEN` / `USER_GITCODE_TOKEN` / 云 AK/SK / 任务上下文等，与现网一致，**零改动**。
- 在其基础上**新增注入** `GRAPE_SESSION_TOKEN`（会话级临时 token，`tk_`）：
  - devops 事件任务：enqueue 签发（session_key=task_id）注入
  - chat 任务：enqueue 签发（session_key=task_id，绑定 user_id）注入
- 实现已上线：`src/runtime/service.py:422`、`src/api/event_pipeline.py:1875`。

> 本阶段对现网流程与技能**不产生任何影响**（env 只增不减，技能对 `$BOT_GITCODE_TOKEN` 的引用照常可用）。

### 4.2 阶段 2——现网验证功能正常后，逐一移除 gitcode token

按依赖顺序**逐个执行记录在案的移除步**，每一步都：技能/流程先走通替代路径 → 现网灰度验证 → 移除 → 观察无回归再进下一步。

| 步 | 移除项 | 前置（必须先完成） | 文件 |
|----|--------|--------------------|------|
| 2.1 | devops 事件任务不再注入 `BOT_GITCODE_TOKEN` env | 技能已切换 `$BOT_GITCODE_TOKEN` 直连 → `{{SERVER_URL}}/api/repos/...` + `$GRAPE_API_TOKEN` / `grape git`，现网 skill 验证通过 | `src/agent/executor.py._assemble_env` devops 分支 |
| 2.2 | 废弃 askpass 脚本（`_write_askpass`/`askpass_path`） | git clone/push 已改走 `grape git`（方案③，CLI 进程内 `/git-credential`） | `src/runtime/repogit.py` |
| 2.3 | （候选）检查 `http.extraheader` 命令行路径不再使用 | 全部 git 操作走 CLI | `src/runtime/repogit.py` |
| 2.4 | （候选）收敛云 AK/SK 注入面 | 引入 `GRAPE_ENV_WHITELIST` 过滤开关 | `service.py/_assemble_env` |

> **保留项**：`USER_GITCODE_TOKEN`（chat 用户身份语义）、`GRAPE_SESSION_TOKEN`（对外 API 授权通道）、任务上下文变量。

---

## 五、env 视图（现状 → 阶段1 → 远期目标态）

```
① 现状（现网）：BOT_GITCODE_TOKEN / USER_GITCODE_TOKEN(+chat) / 云AK-SK / 任务上下文 / ...
    │
② 阶段1（当前目标，已实现）：现状 + GRAPE_SESSION_TOKEN   ← 只增不减，现网无感
    │
③ 远期目标态（全部移除后）：任务上下文 + GRAPE_SESSION_TOKEN + 云AK-SK
    （无 BOT/USER GitCode token；平台身份改由 grape CLI/API 提供）
```

---

## 六、改动清单

### 阶段 1（当前：只新增，已实现）

| # | 模块 | 改动 | 状态 |
|---|------|------|------|
| 1 | `src/runtime/service.py` | enqueue 统一签发 `GRAPE_SESSION_TOKEN` 注入（session_key=task_id） | ✅ 已实现（M2） |
| 2 | `src/api/event_pipeline.py` | direct 模式签发 `GRAPE_SESSION_TOKEN` 注入 | ✅ 已实现（M2） |
| 3 | `tests/test_session_token_lifecycle.py` | 注入/失效用例 | ✅ 已实现（M2） |

### 阶段 2（现网验证后逐项执行，记录在案）

| # | 模块 | 改动 | 优先级 | 状态 |
|---|------|------|--------|------|
| 4 | `src/agent/executor.py` | `_assemble_env` devops 分支移除 `BOT_GITCODE_TOKEN` 注入 | P0（验证后） | ⏳ 待验证 |
| 5 | `src/runtime/repogit.py` | `_write_askpass`/`askpass_path` 废弃 | P0（验证后） | ⏳ 待验证 |
| 6 | 技能包（F-2） | `gitcode-api-reference.md` 与技能引用切换 `api.gitcode.com`→`/api/repos/*`、`$BOT_GITCODE_TOKEN`→`$GRAPE_API_TOKEN` | P0（先行铺垫） | ⏳ 待验证 |
| 7 | `tests/test_executor_token_env.py` | 断言：devops 事件任务 env 无 GitCode 平台 token、有 `GRAPE_SESSION_TOKEN` | P1 | ⏳ 待验证 |
| 8 | 开关 | `GRAPE_CONVERGE_BOT_ENV=1` 灰度（默认关，单仓库可开可回滚） | P1 | ⏳ 待验证 |
| 9 | 文档 | `docs/openapi/10-git-repos.md` 技能迁移说明同步 | P1 | ⏳ 待验证 |

---

## 七、验收标准

### 阶段 1（当前）
- [ ] **现网 agent 注入 env 与之前完全一致**（无任何变量被移除/覆盖变化）
- [ ] 在其之上**新增** `GRAPE_SESSION_TOKEN`（`tk_`）：devops 与 chat 任务均随任务注入、随会话失效
- [ ] 现网技能（含引用 `$BOT_GITCODE_TOKEN` 的旧技能）行为 **0 回归**；`GRAPE_SESSION_TOKEN` 可被 agent 用于调对外 API

### 阶段 2（远期，逐项完成后勾记）
- [ ] devops 事件任务 env 不含 `BOT_GITCODE_TOKEN`；agent 工作区无 askpass 残留；`http.extraheader` 命令行路径不再使用
- [ ] 技能已全量走 `/api/repos/*` + `$GRAPE_API_TOKEN`（grep 无 `\$BOT_GITCODE_TOKEN` 残留）
- [ ] chat 语义不变（`USER_GITCODE_TOKEN` 保留、与 bot 互斥）；`GRAPE_SESSION_TOKEN` 注入不受影响
- [ ] 每步移除均有现网灰度验证记录，`GRAPE_CONVERGE_BOT_ENV` 可回滚

---

## 八、风险与推进顺序

| 风险 | 影响 | 缓解 |
|------|------|------|
| 阶段1 误动了现有 env | 高 | **阶段1 只增不减**：diff 只含 `GRAPE_SESSION_TOKEN` 一行，评审可核对 |
| 阶段2 技能未迁移即移除 env → 执行中断 | 高 | **验证前置**：技能先走 `/api/repos` 替代路径并在现网验证 → 才移除；`GRAPE_CONVERGE_BOT_ENV` 可回滚 |
| askpass 依赖残留在旧 worktree | 中 | askpass 废弃与 2.1 同批；存量 worktree 复用 `grape git` 重建 |
| 云 AK/SK 裸奔 | 中 | 远期 `GRAPE_ENV_WHITELIST` 过滤（阶段2.4 候选） |

**推进顺序**：
```
阶段1（已实现）→ 现网跑技能验证（功能正常）
   → 阶段2 逐项：① skill 迁移 → ② 灰度开关按仓库开（GRAPE_CONVERGE_BOT_ENV）
   → ③ 全量移除 BOT env → ④ askpass 废弃/extraheader 收敛 → ⑤ 白名单收窄
```
> 与执行规划 M3.4 一致（"先改技能引用到 `/api/repos`，再撤 env"），但执行节奏更保守：**先增量、后验证、再逐一减法**。

---

## 附：关键代码位置

- env 打包：`src/runtime/service.py` `enqueue_task`（L311）/ `_collect_server_env`（L193）
- **`GRAPE_SESSION_TOKEN` 注入**：① enqueue 主路径 `src/runtime/service.py:422`；② direct 模式 `src/api/event_pipeline.py:1875`
- env 组装与注入：`src/agent/executor.py` `_assemble_env`（L209，devops 分支 L235）/ `src/agent/hermes_acp.py:124` Popen
- worker 侧：`src/runtime/agent.py:947` env 反序列化、`L1167` 传入
- askpass：`src/runtime/repogit.py:168` `_write_askpass` / `askpass_path`
- CLI 凭证注入（方案③）：`cli/grape_cli/commands/git.py`（GIT_CONFIG env，不进 argv/不落盘）