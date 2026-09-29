# 4. Agent 执行环境变量

> Grape 将任务调度给不同类型的 Agent（VOD / Issue / Coding / QA / SRE / Chat）时，
> 会在 Agent 的**执行环境**中注入一组环境变量（`env`），供 Skill 脚本 / Agent 读取。
> 本页汇总这些变量的**来源、语义、适用 Agent 类型**，方便开发者编写 Skill 时正确使用。

## 一、注入链路概览

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

## 二、核心注入变量总表

| 环境变量 | 语义 | 适用 Agent | 注入来源 |
|----------|------|-----------|----------|
| `GRAPE_SERVER_URL` | Grape 服务端地址（skill 调 API 用） | 全部 | `_execute_agent` / agent.py 本机修正 |
| `GRAPE_SESSION_TOKEN` | 会话级临时 token（`tk_`，task 生命周期内有效，终态失效） | 全部 | `service.py` `issue_session_token` |
| `GRAPE_CLI_MODE` | CLI 命令过滤模式：`WORKER` = 只显示会话 token（`tk_`）可用的命令（dual + session_only），隐藏 user_only（agent 持 `tk_` 打了也 403） | 全部（注入 session token 的任务） | 随 `GRAPE_SESSION_TOKEN` 注入（`service.py` / `event_pipeline.py`） |
| `REPO_FULL_NAME` | `owner/repo` 仓库全名（服务端注入，原始语义） | 全部（devops 事件） | `_execute_agent` |
| `ACCOUNT` | **仓库属主（owner，如 `Jason7602`）**；从 repository 拆分，始终不含 `/repo` | 全部（devops 事件） | `_execute_agent`（`split_owner_repo`） |
| `REPO_NAME` | 仓库名（repo，如 `grape`，即 `${REPO_FULL_NAME#*/}`） | 全部（devops 事件） | `_execute_agent`（`split_owner_repo`） |
| `ISSUE_NUMBER` | Issue 编号（去 # 前缀的数字） | 事件任务（有 Issue） | `_execute_agent` |
| `PR_NUMBER` | PR 编号 | 事件任务（有 PR） | `_execute_agent` |
| `BOT_USERNAME` | 全局 bot 用户名（grape_bot） | Coding 等（skill 用） | `_execute_agent`（coding 注入） |
| `GRAPE_WORK_BRANCH` | 场景工作基线分支 | Coding/QA/SRE（git 类） | `_read_scene_work_branch` → env_extra |
| `GRAPE_SCENE_ID` | 当前 Issue 绑定的场景标识（issue-219：统一 env 注入；未选定=空串，不回落 default） | 全部（devops 事件，含注入场景的任务） | `_inject_scene_env` → env_extra |
| `GRAPE_CF_1..N` | 场景高级选项 custom_fields 按序号注入的值 | 配置了 custom_fields 的场景 | `_inject_scene_env` |
| `GRAPE_CF_MAP` | `{"GRAPE_CF_1":"key名",...}` 键名映射 | 同上 | 同上 |
| `BOT_GITCODE_TOKEN` | 仓库 bot token（仓库身份，事件任务） | **非 chat**（devops 事件） | `_assemble_env`（互斥） |
| `BOT_GITHUB_TOKEN` | GitHub 仓库 bot token | 非 chat（GitHub 仓库） | `_assemble_env`（互斥） |
| `USER_GITCODE_TOKEN` | 当前用户 GitCode token（用户身份） | **Chat 专用** | `_assemble_env`（互斥） |
| `USER_GITHUB_TOKEN` | 当前用户 GitHub token | Chat（GitHub 关联） | `_assemble_env`（互斥） |
| `DEVOPS_SKILLS_DIR` | 技能目录 | 全部 | 各引擎 get_env |
| `DEVOPS_EVENT_TYPE` | **触发事件类型**（`issue_opened` / `issue_comment` / `push` / `pr_merge` / `vod_signal` 等）——「在响应什么事件」 | 全部 | 引擎 get_env 默认空，`stream_agent` 覆写为 event_type |
| `AGENT_NAME` | **Agent 角色显示名**（`Issue Agent` / `QA Agent` / `Coding Agent` / `VOD Agent`…）——「我是谁」，skill 评论前缀 `[$AGENT_NAME]` 用 | 全部 | `stream_agent` 前 `get_agent_display_name(agent_type)` |
| `GIT_ASKPASS` | git 凭据辅助脚本（BOT_GITCODE_TOKEN） | Coding/QA/SRE（git workflow） | `repogit.askpass_path` |

> 注：`HUAWEICLOUD_SDK_AK/SK`、`CODEARTS_CLI_AK/SK`、`GITHUB_PUBLISH_REPO`、`GITCODE_PUBLISH_REPO`、
> `CLAWHUB_OWNER_HANDLE` **已不再经环境变量注入**（#219 按需获取）——技能需要时通过自定义场景变量（`GRAPE_CF_*`）
> 或按需 API 获取。

> **`AGENT_NAME` 与 `DEVOPS_EVENT_TYPE` 的区别**：两者是正交的两个维度——
> `AGENT_NAME` = **身份**（谁在执行，按 agent_type 的角色显示名，如 `QA Agent`，用于 `[$AGENT_NAME]` 评论前缀）；
> `DEVOPS_EVENT_TYPE` = **时机**（在响应什么触发事件，如 `issue_comment`/`push`，技能据此分派分支）。
> 例：同一 `QA Agent`（名字恒为 `QA Agent`）可以在 `pr_opened`、`issue_comment` 等不同事件类型下被触发。

## 三、GitCode Token 互斥注入（关键）

`_assemble_env`（`agent/executor.py`）按执行类型**互斥注入** token：

```
Chat 任务  → 只注入 USER_GITCODE_TOKEN（用户身份；不用 bot token 冒充用户）
事件任务  → 只注入 BOT_GITCODE_TOKEN（仓库身份；不注入用户 token）
```

- 二者**不同时注入**（防用户对话上下文暴露仓库 bot token）。
- GitHub 仓库额外注入 `BOT_GITHUB_TOKEN` / `USER_GITHUB_TOKEN`。
- 自定义 provider 凭据（openai 等，按 `get_credentials`）与 GITCODE token 无关，chat 与 devops **都按 user_id 注入**。

## 四、按 Agent 类型的可用性

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
> 技能需要时走自定义场景变量（`GRAPE_CF_*`）或按需 API。

## 五、Skill 开发建议

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
   `custom_fields`，映射为 `GRAPE_CF_*`）读取，或在技能 SKILL.md 中声明按需获取方式。
5. **场景读取**：`printenv GRAPE_SCENE_ID` 确认当前 Issue 绑定的场景（空串=尚未判定）；
   自定义字段读 `GRAPE_CF_MAP` 按 key 名取 `GRAPE_CF_N` 值。
6. **git 操作**：用 `GRAPE_WORK_BRANCH` 作工作基线；`GIT_ASKPASS` 已注入 `$BOT_GITCODE_TOKEN`。
7. **token 安全**：`BOT_GITCODE_TOKEN`/`USER_GITCODE_TOKEN` 是敏感变量——**不要 printenv 打印/写入日志**；
   平台已有脱敏（[REDACTED]）。token 仅在进程内有效、删除即失效（GRAPE_SESSION_TOKEN 生命周期=任务）。

### 五.1 技能如何使用自定义场景变量（GRAPE_CF_*）

场景高级选项 `custom_fields`（`[{key, value}, ...]`）注入为 `GRAPE_CF_1..N`（值）+ `GRAPE_CF_MAP`
（键名→变量序号映射）。技能**不硬编码序号**，按 key 名动态取值：

```bash
# 1) 发现：当前场景配了哪些自定义字段
printenv GRAPE_CF_MAP        # {"GRAPE_CF_1":"HUAWEI_AK","GRAPE_CF_2":"HUAWEI_SK","GRAPE_CF_3":"DEPLOY_ENV"}

# 2) 按 key 名映射为业务变量（随场景自动绑定，序号无关）
eval "$(printenv GRAPE_CF_MAP | python3 -c '
import json, os, sys
m = json.load(sys.stdin)
for var, key in m.items():
    print(f"export {key}={os.environ.get(var, \"\")!r}")')"

# 3) 使用
echo "AK=$HUAWEI_AK SK=${HUAWEI_SK:+<set>} ENV=$DEPLOY_ENV"
```

对齐场景（例：把原 `HUAWEICLOUD_SDK_AK/SK` 改为场景自定义字段）：
```json
{"custom_fields": [
  {"key": "HUAWEI_AK", "value": "LTAxxx"},
  {"key": "HUAWEI_SK", "value": "xxxyyy"},
  {"key": "DEPLOY_ENV", "value": "prod"}
]}
```

注意：值只进 env、不进 prompt；技能 PRINTENV 时**不要回显敏感值到日志/评论**。

## 六、关键代码位置

| 环节 | 位置 |
|------|------|
| env_extra 组装 | `src/api/event_pipeline.py` `_execute_agent`（~L1653 起） |
| 场景字段注入 | `_inject_scene_env`（`src/api/event_pipeline.py`） |
| 会话级 token | `src/api/auth_token.py` `issue_session_token`（TTL 6h） |
| token 互斥注入 | `src/agent/executor.py` `_assemble_env`（L212-254） |
| env 落库 | `src/runtime/service.py` `enqueue_task` env_extra（L574-591） |
| Worker 侧加载 | `src/runtime/agent.py` `_execute_task`（~L950） |
| 引擎 get_env | `src/runtime/opencode.py` / `codearts.py` / `hermes.py` / `codex.py` |