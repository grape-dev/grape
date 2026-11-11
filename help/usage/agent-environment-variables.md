# 4. Agent 执行环境变量

> Grape 将任务调度给不同类型的 Agent（VOD / Issue / Coding / QA / SRE / Chat）时，
> 会在 Agent 的**执行环境**中注入一组环境变量（`env`），供 Skill 脚本 / Agent 读取。
> 本页汇总这些变量的**来源、语义、适用 Agent 类型**，方便开发者编写 Skill 时正确使用。

## 注入链路概览

```
GitCode 事件（Issue/PR/评论）
   → webhook → event_pipeline（_execute_agent）组装 env_extra
   → enqueue_task 落 agent_tasks.env_extra（JSON 列）
   → Worker claim 任务 → stream_agent/_stream_runtime_async
   → 引擎 get_env（opencode/codearts/hermes 基础环境）
   → env_extra.update() / _assemble_env()（token 互斥注入）
   → Skill 进程内可 printenv 读取
```

- **env_extra**：任务级注入（主控端组装，见 `event_pipeline.py` / `service.py`）
- **引擎 get_env**：引擎侧基础环境（`opencode.py` / `codearts.py` / `hermes.py` / `codex.py`）
- **_assemble_env**：GitCode/GitHub token 互斥注入（`agent/executor.py`）

## 核心注入变量总表

| 环境变量 | 语义 | 适用 Agent | 注入来源 |
|----------|------|-----------|----------|
| `GRAPE_SERVER_URL` | **主控业务 API 基址**（issues / git-repo 等全量端点，skill 调业务 API / `grape` 业务命令用；**不是** worker runtime 地址）。来源：`task_business_api_url` 显式配置 > 本机 runtime 回退 `127.0.0.1:{port}` > `server_base_url`（本机 runtime 未显式配置时走回环，远程 runtime 用 `server_base_url`） | 全部 | `event_pipeline.py`（`_enqueue_pipeline_task` / `build_task_env`）/ 对话入队（`routes/chat.py`、`im/handler_async.py`、`im/vod_handler_async.py`、`runtime/scheduler.py`，均经 `resolve_task_server_env`）/ `agent.py` 本机或单地址部署覆盖 |
| `RUNTIME_SERVER_URL` | **worker runtime / A2A 回连基址**（任务 server_url 同语义；`grape` 的 runtime 绑定命令用）。来源：`worker_connect_url`（未配置回退 `server_base_url`） | 全部 | `event_pipeline.py`（`_enqueue_pipeline_task` / `build_task_env`）/ 对话入队（`routes/chat.py`、`im/handler_async.py`、`im/vod_handler_async.py`、`runtime/scheduler.py`，均经 `resolve_task_server_env`）/ `agent.py` 本机或内网 worker 覆盖 |
| `GRAPE_SESSION_TOKEN` | 会话级临时 token（`tk_`，task 生命周期内有效，终态失效） | 全部 | `service.py` `issue_session_token` |
| `GRAPE_CLI_MODE` | CLI 命令过滤模式：`WORKER` = 只显示会话 token（`tk_`）可用的命令（dual + session_only），隐藏 user_only（agent 持 `tk_` 打了也 403） | 全部（注入 session token 的任务） | 随 `GRAPE_SESSION_TOKEN` 注入（`service.py` / `event_pipeline.py`） |
| `REPO_FULL_NAME` | `owner/repo` 仓库全名（服务端注入，原始语义）。**格式契约（#721）**：形如 `GTDTeam/CodeArts`——**无 host 前缀、无 provider 前缀（`gitcode:`/`github:`）、无 `.git` 后缀**；注入侧统一 `from_repo_key` 归一（幂等），runbook 拼接 clone URL 前仍应做一次归一兜底（见下「runbook clone URL 模板」） | 全部（devops 事件） | `_execute_agent` |
| `REPO_OWNER` | **仓库属主（owner，组织或个人的泛化概念）**；从 repository 拆分，始终不含 `/repo` | 全部（devops 事件） | `_execute_agent`（`split_owner_repo`） |
| `REPO_NAME` | 仓库名（repo，如 `grape`，即 `${REPO_FULL_NAME#*/}`） | 全部（devops 事件） | `_execute_agent`（`split_owner_repo`） |
| `ACCOUNT` | **个人账号（webhook 触发人 / A2A 触发人，如 `Jason7602`）**；与仓库属主解耦，**解析为空时不会回退为仓库属主** | 全部（devops 事件） | `_execute_agent` / `_enqueue_pipeline_task`（`resolve_actor_account`：a2a_event 为 `payload.account` → `actor` → Issue `created_by`） |
| `ISSUE_NUMBER` | Issue 编号（去 # 前缀的数字） | 事件任务（有 Issue） | `_execute_agent` |
| `GRAPE_ISSUE_ID` | **需求卡片 id（issue 表主键，整数**；awaiting-input / pending-review / review-pass 的 `--issue_id` 用此值，**非 ISSUE_NUMBER**） | 事件任务（有 Issue） | `_execute_agent`（按 `(repo_key, issue_number)` 查卡片） |
| `PR_NUMBER` | PR 编号 | 事件任务（有真实 PR） | `_execute_agent`（`_inject_issue_pr_env` → `_extract_pr_number`）：`payload.pr_number` → `evt._pr_sub_pr_number` 为显式来源、始终可信；仅 **PR/MR 实体事件**才开放对象结构来源（`payload.pull_request.{number,iid}` / `payload.merge_request.{number,iid}` / `payload.object_attributes.iid`），均为去 `#` 后纯数字，Issue #595。**note_hook_pr**：payload 优先（`noteable_iid` / `merge_request` / `pull_request`），缺失时从结构化 URL（`object_attributes.url` / `merge_request`、`pull_request` 的 `web_url`，形如 .../merge_requests/<N>）兜底，不从事件标题猜号，Issue #1579。**Issue #1802 口径**：Issue 实体事件的 `object_attributes.iid` 是 Issue 自身编号，**永不作 PR 号来源**；无真实关联 PR 时**静默不注入** `PR_NUMBER`（不写占位值、不用 `ISSUE_NUMBER` 顶替）；仅 PR/MR 实体解析失败才注入 `PR_NUMBER_PARSE_FAILED=1` 告警。**URL 兜底取到的号会先做一次存在性校验**（`_pr_number_exists`，60s 缓存；明确 404 才丢弃，查询异常按存在处理） |
| `BOT_USERNAME` | 全局 bot 用户名（grape_bot）——**作者过滤/幂等查重必须用它**，缺失时不得退化为「按评论形态判定」 | 全部 devops 事件 + 定时调度（autopilot） | 四条执行路径统一 `_inject_bot_agent_env`（`event_pipeline` ×2 / `direct_executor` / `runtime/scheduler`）；`_resolve_bot_username` 解析为空时**打 warning 而非静默降级** |
| `GRAPE_WORK_BRANCH` | 场景工作基线分支 | Coding/QA/SRE（git 类） | `_read_scene_work_branch` → env_extra |
| `GRAPE_SCENE_ID` | 当前 Issue 绑定的场景标识（issue-219：统一 env 注入；未选定=空串，不回落 default）。**#1745**：跨场景转交后以 Issue 当前归属场景为真值；重派/执行前会按 `issues detail.scene_id` 校正陈旧快照（PR 订阅任务保留订阅场景）。未绑定场景时本机执行与远端 worker **同源回退仓库默认场景 `dev_scene`**（不再一边给 dev_scene、一边清空）；绑定的场景已被删除时按「未绑定」处理，不注入幽灵 scene_id | 全部（devops 事件，含注入场景的任务） | `_inject_scene_env` → env_extra；执行前 `reconcile_task_scene_env` |
| `GRAPE_SCENE_SCOPE_ON_ERROR` | 场景技能 scope 判定降级开关（服务端读，非注入给 agent）：`deny`（默认）= 查询异常 fail-closed；`allow` = 查询异常按放行并标 source=db_error，仅在 DB 抖动阻断派发时临时打开 | 平台进程 | `scene_skill_scope._env_error_policy` |
| 场景自定义变量（`custom_fields`） | 场景高级选项配置的自定义环境变量，**以变量名直接注入**（如 `MY_API_KEY`、`HUAWEI_AK`），技能 `os.environ.get('VAR_NAME')` 直接读取；支持 `is_secret`（页面掩码）与 `scope`（按 agent 过滤，默认 all） | 配置了 custom_fields 的场景（按 scope 匹配的 agent） | `_inject_scene_env` → env_extra |
| `BOT_GITCODE_TOKEN` | 仓库 bot token（仓库身份，事件任务） | **非 chat**（devops 事件） | `_assemble_env`（互斥） |
| `BOT_GITHUB_TOKEN` | GitHub 仓库 bot token | 非 chat（GitHub 仓库） | `_assemble_env`（互斥） |
| `USER_GITCODE_TOKEN` | 当前用户 GitCode token（用户身份） | **Chat 专用** | `_assemble_env`（互斥） |
| `USER_GITHUB_TOKEN` | 当前用户 GitHub token | Chat（GitHub 关联） | `_assemble_env`（互斥） |
| `DEVOPS_SKILLS_DIR` | 技能目录 | 全部 | 各引擎 get_env |
| `DEVOPS_EVENT_TYPE` | **触发事件类型**（`issue_opened` / `issue_comment` / `push` / `pr_merge` / `vod_signal` 等）——「在响应什么事件」 | 全部 | 引擎 get_env 默认空，`stream_agent` 覆写为 event_type |
| `AGENT_NAME` | **Agent 角色显示名**（`Issue Agent` / `QA Agent` / `Coding Agent` / `VOD Agent`…）——「我是谁」，skill 评论前缀 `[$AGENT_NAME]` 用；**它不是账号，禁止当评论作者过滤依据**（作者过滤用 `BOT_USERNAME`） | 全部 | `_inject_bot_agent_env` → `get_agent_display_name(agent_type)` |
| `GIT_ASKPASS` | git 凭据辅助脚本（BOT_GITCODE_TOKEN） | Coding/QA/SRE（git workflow） | `repogit.askpass_path` |

> 注：`HUAWEICLOUD_SDK_AK/SK`、`CODEARTS_CLI_AK/SK`、`GITHUB_PUBLISH_REPO`、`GITCODE_PUBLISH_REPO`、
> `CLAWHUB_OWNER_HANDLE` **已不再经环境变量注入**（#219 按需获取）——技能需要时通过自定义场景变量（场景高级选项 custom_fields，变量名直接注入）
> 或按需 API 获取。

> **`ACCOUNT` 取值优先级（Issue #1668）**：
> 1. `a2a_event`：`payload.account`（A2A 交办方显式携带的触发人）→ 事件 `actor`；
>    - `/api/a2a` 入口的事件 `actor` 取**已认证调用方登录名**（JWT / API Key 可提供；会话 token 无登录名时为空）；
>    - 内部派生的 a2a_event（订阅 / PR 订阅 / VOD 重派）继承来源事件的 `actor`；
> 2. 其余事件：事件 `actor`（webhook 触发人）→ `payload.account`；
> 3. 仅 `a2a_event`：上述为空时按 `issue_number` 查 `issue.created_by`（Issue 创建人）；
> 4. 仍为空 → 注入空串，并在服务端事件日志输出结构化告警 `ACCOUNT_MISSING`（含 `event_type` / `issue_number` / `repository` / 实际回退链 `fallback`）。
>
> **禁止回退为仓库属主（`REPO_OWNER`）**——个人账号与仓库属主解耦；A2A payload 的 `account` 与事件记录 `actor` 同步使用该解析结果。
> 订阅 / PR 订阅 / VOD 重派等派生的 a2a_event 会把解析结果同时写入事件 `actor` 与落库 `payload.account`，保证下游任务与事件查询一致。
> `issue_opened` / PR 等非 A2A 事件的 `ACCOUNT` 仍取 webhook 触发人 `actor`，行为不变。

> **`AGENT_NAME` 与 `DEVOPS_EVENT_TYPE` 的区别**：两者是正交的两个维度——
> `AGENT_NAME` = **身份**（谁在执行，按 agent_type 的角色显示名，如 `QA Agent`，用于 `[$AGENT_NAME]` 评论前缀）；
> `DEVOPS_EVENT_TYPE` = **时机**（在响应什么触发事件，如 `issue_comment`/`push`，技能据此分派分支）。
> 例：同一 `QA Agent`（名字恒为 `QA Agent`）可以在 `pr_opened`、`issue_comment` 等不同事件类型下被触发。

## GitCode Token 互斥注入（关键）

`_assemble_env`（`agent/executor.py`）按执行类型**互斥注入** token：

```
Chat 任务  → 只注入 USER_GITCODE_TOKEN（用户身份；不用 bot token 冒充用户）
事件任务  → 只注入 BOT_GITCODE_TOKEN（仓库身份；不注入用户 token）
```

- 二者**不同时注入**（防用户对话上下文暴露仓库 bot token）。
- GitHub 仓库额外注入 `BOT_GITHUB_TOKEN` / `USER_GITHUB_TOKEN`。
- 自定义 provider 凭据（openai 等，按 `get_credentials`）与 GITCODE token 无关，chat 与 devops **都按 user_id 注入**。

## 按 Agent 类型的可用性

| Agent | 仓库身份 token | 用户身份 token | 场景字段 |
|-------|----------------|----------------|----------|
| VOD | BOT_GITCODE_TOKEN | — | ✓ |
| Issue | BOT_GITCODE_TOKEN | — | ✓ |
| Coding | BOT_GITCODE_TOKEN | — | ✓（工作分支） |
| QA | BOT_GITCODE_TOKEN | — | ✓ |
| SRE | BOT_GITCODE_TOKEN | — | ✓ |
| Chat | — | USER_GITCODE_TOKEN | — |

> Chat 用当前登录用户身份；事件任务用仓库 bot 身份。
> 华为云/CodeArts AK-SK 与发布目标（gitcode/github publish repo、ClawHub handle）**不再 env 注入**（#219），
> 技能需要时走**自定义场景变量**（场景高级选项 custom_fields，变量名直接注入）或按需 API。

## Skill 开发建议

1. **读 GitCode 仓库信息**：
   ```bash
   echo "repo=$REPO_FULL_NAME issue=$ISSUE_NUMBER"
   curl -sH "Authorization: Bearer $BOT_GITCODE_TOKEN" \
     "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME"
   ```
2. **调 Grape 内部 API**：用 `GRAPE_SERVER_URL` + `GRAPE_SESSION_TOKEN`（鉴权 Bearer）。
3. **CLI 命令范围**：环境已注入 `GRAPE_CLI_MODE=WORKER`——`grape --help` / `grape commands` 只会列出
   **会话 token（`tk_`）可用的命令**（dual + session_only，含 git-repo / issues 读 / a2a / tasks 等）；
   user_only（skills 管理 / settings / repos 平台管理 等）已被过滤——agent 不要尝试调用它们（服务端仍会 403）。
4. **华为云/CodeArts 凭据**：不再经 env 注入——需要云能力的 SRE/skill 通过**自定义场景变量**（场景高级选项
   `custom_fields`，变量名直接注入）读取，或在技能 SKILL.md 中声明按需获取方式。
5. **场景读取**：`printenv GRAPE_SCENE_ID` 确认当前 Issue 绑定的场景（空串=尚未判定）；
   自定义字段以**变量名直接注入**——`printenv <NAME>` / `os.environ.get('<NAME>')` 直接读值。
   **跨场景转交/重入时以 `issues detail` 返回的 `scene_id` 为权威**，`GRAPE_SCENE_ID` 作兜底：
   两者不一致时按真实值执行 `scenes detail --scene_id <真实值>` 重取节点后重试，
   仅当真实场景确实不含目标节点时才进入待确认（Issue #1745）。
6. **git 操作**：用 `GRAPE_WORK_BRANCH` 作工作基线；`GIT_ASKPASS` 已注入 `$BOT_GITCODE_TOKEN`。
7. **token 安全**：`BOT_GITCODE_TOKEN`/`USER_GITCODE_TOKEN` 是敏感变量——**不要 printenv 打印/写入日志**；
   平台已有脱敏（[REDACTED]）。token 仅在进程内有效、删除即失效（GRAPE_SESSION_TOKEN 生命周期=任务）。

### 五.1 runbook clone URL 模板（规范写法，#721）

runbook 需要本地 clone 仓库时，**先归一再拼接**，禁止把 `$REPO_FULL_NAME` 直通进 URL
（直通在注入值带 provider 前缀/为空时会拼出错误地址，见用户反馈 488 / Issue #721）：

```bash
# 1) 归一 + 校验（owner/repo 两段、无前缀、无 .git）
REPO_URL_PATH="$(printf '%s' "${REPO_FULL_NAME:-}" | sed 's#^[a-z]*:##; s#\.git$##')"
case "$REPO_URL_PATH" in */*) : ;; *) echo "REPO_FULL_NAME 非法（应为 owner/repo）: ${REPO_URL_PATH:-<空>}" >&2; exit 1 ;; esac

# 2) 拼接（host 按仓库 provider 选择；gitcode 仓库固定如下）
CLONE_URL="https://gitcode.com/${REPO_URL_PATH}.git"

# 3) clone（不要 2>/dev/null 吞错；也不要 clone 失败后盲目 || git pull 掩盖根因）
grape git clone "$CLONE_URL" "$repo_dir" || { echo "clone 失败: $CLONE_URL" >&2; exit 1; }
```

### 五.2 clone 失败排查指引（#721）

**第一步：校验变量值**（只回显非敏感仓库变量）：

```bash
echo "REPO_FULL_NAME=[${REPO_FULL_NAME:-}] REPO_OWNER=[${REPO_OWNER:-}] REPO_NAME=[${REPO_NAME:-}]"
test -n "$REPO_FULL_NAME" && test -n "$REPO_OWNER" && test -n "$REPO_NAME" || echo "存在空值：漏注入（同族 #584 BOT_USERNAME / #595 PR_NUMBER），如实报错勿猜"
```

**常见错误模式对照**：

| 症状 | 典型错误 URL | 根因 | 处理 |
|------|--------------|------|------|
| clone 404 / repository not found | `https://gitcode.com/gitcode:owner/repo.git` | `$REPO_FULL_NAME` 带 provider 前缀直通拼接 | 按五.1 归一后再拼；上报注入侧异常 |
| clone 404 / repository not found | `https://gitcode.com/.git` | `$REPO_FULL_NAME` 为空（漏注入） | 排查任务 env_extra；勿猜仓库名 |
| 404 / repository not found | `https://gitcode.com/owner/repo`（缺 `.git`） | 模板漏 `.git` 后缀 | 模板补 `.git`（部分 provider 必需） |
| Could not resolve host | `https://gitcode.com/owner/repo.git` 用于 GitHub 仓库 | host 与 provider 不匹配 | GitHub 仓用 `https://github.com/owner/repo.git` |
| Authentication failed | URL 正确 | 凭据/权限（401 token 失效；403 需维护者权限） | 401 说明凭据失效；403 需管理员配置，不换 token |
| 拼接正确仍 clone 失败 | — | 网络/远端临时故障 | 重试 1 次，仍失败如实报错（附手工命令） |

**同族一致性核对结论（#584/#595/#721）**：三者的根因形态一致——「runbook 依赖的环境变量与实际注入值/格式不匹配」。
`BOT_USERNAME`（#584，`_resolve_bot_username`）、`PR_NUMBER`（#595，`_inject_issue_pr_env`）、
`REPO_FULL_NAME`（#721，`from_repo_key` 归一）均已纳入注入侧统一口径与保留名清单
（`src/scene_custom_env.py` `_RESERVED_ENV_NAMES`，场景自定义变量不可覆盖）；
测试见 `tests/test_issue_573_bot_agent_env_injection.py`、`tests/test_issue_528_pr_sub_env_injection.py`、
`tests/test_issue_721_repo_full_name_env.py`。

### 五.3 技能如何使用自定义场景变量

场景高级选项 `custom_fields`（`[{name, value, is_secret?, scope?}, ...]`）**以变量名直接注入**——`name` 即环境变量名，技能直接读取，无序号/映射。

```bash
# 直接读取（printenv / os.environ.get）
printenv HUAWEI_AK
# 或
python3 -c 'import os; print(os.environ.get("HUAWEI_AK",""))'
```

对齐场景（例：把原 `HUAWEICLOUD_SDK_AK/SK` 改为场景自定义字段）：
```json
{"custom_fields": [
  {"name": "HUAWEI_AK", "value": "LTAxxx"},
  {"name": "HUAWEI_SK", "value": "xxxyyy", "is_secret": true},
  {"name": "DEPLOY_ENV", "value": "prod", "scope": ["all"]}
]}
```

注意：值只进 env、不进 prompt；技能 PRINTENV 时**不要回显敏感值到日志/评论**。

## 关键代码位置

| 环节 | 位置 |
|------|------|
| env_extra 组装 | `src/api/event_pipeline.py` `_execute_agent`（~L1653 起） |
| 场景字段注入 | `_inject_scene_env`（`src/api/event_pipeline.py`） |
| 会话级 token | `src/api/auth_token.py` `issue_session_token`（TTL 6h） |
| token 互斥注入 | `src/agent/executor.py` `_assemble_env`（L212-254） |
| env 落库 | `src/runtime/service.py` `enqueue_task` env_extra（L574-591） |
| Worker 侧加载 | `src/runtime/agent.py` `_execute_task`（~L950） |
| 引擎 get_env | `src/runtime/opencode.py` / `codearts.py` / `hermes.py` / `codex.py` |
## AtomCode 引擎配置

| 变量 | 行为 |
|------|------|
| `GRAPE_ATOMCODE_PROVIDER` | 与 `GRAPE_ATOMCODE_MODEL` 同时配置，传入 CLI `--provider` |
| `GRAPE_ATOMCODE_MODEL` | 与 provider 同时配置，传入 CLI `--model`；仅设置其中一项时任务明确失败 |
| `ATOMCODE_HOME` | 官方 CLI 用户目录，默认 `~/.atomcode`；技能目录为其 `skills` 子目录 |

provider/model 的优先级为任务执行环境 > worker 环境；均未配置时使用 CLI 自身默认值。
登录在实际执行机器上运行 `atomcode login`，不通过 Grape 保存或修改 CLI 登录凭据。
首版使用独立任务模式，不原生 resume，支持 macOS/Linux，Windows 暂不支持。
安装与排障步骤参见 [README](../../README.zh.md#atomcode-引擎macos--linux)。
