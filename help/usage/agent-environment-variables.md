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
| `REPO_FULL_NAME` | `owner/repo` 仓库全名 | 全部（devops 事件） | `_execute_agent` |
| `ACCOUNT` | 仓库所属账号 | 全部 | `_execute_agent` |
| `ISSUE_NUMBER` | Issue 编号（去 # 前缀的数字） | 事件任务（有 Issue） | `_execute_agent` |
| `PR_NUMBER` | PR 编号 | 事件任务（有 PR） | `_execute_agent` |
| `BOT_USERNAME` | 全局 bot 用户名（grape_bot） | Coding 等（skill 用） | `_execute_agent`（coding 注入） |
| `GRAPE_WORK_BRANCH` | 场景工作基线分支 | Coding/QA/SRE（git 类） | `_read_scene_work_branch` → env_extra |
| `GRAPE_CF_1..N` | 场景高级选项 custom_fields 按序号注入的值 | 配置了 custom_fields 的场景 | `_inject_custom_fields_env` |
| `GRAPE_CF_MAP` | `{"GRAPE_CF_1":"key名",...}` 键名映射 | 同上 | 同上 |
| `BOT_GITCODE_TOKEN` | 仓库 bot token（仓库身份，事件任务） | **非 chat**（devops 事件） | `_assemble_env`（互斥） |
| `BOT_GITHUB_TOKEN` | GitHub 仓库 bot token | 非 chat（GitHub 仓库） | `_assemble_env`（互斥） |
| `USER_GITCODE_TOKEN` | 当前用户 GitCode token（用户身份） | **Chat 专用** | `_assemble_env`（互斥） |
| `USER_GITHUB_TOKEN` | 当前用户 GitHub token | Chat（GitHub 关联） | `_assemble_env`（互斥） |
| `HUAWEICLOUD_SDK_AK` / `HUAWEICLOUD_SDK_SK` | 华为云 AK/SK（按仓库 owner 注入） | SRE（cloud-deploy）等 | `_inject_cloud_credentials` |
| `CODEARTS_CLI_AK` / `CODEARTS_CLI_SK` | CodeArts CLI 华为云认证 | CodeArts 引擎 | `_collect_server_env` / codearts.get_env |
| `DEVOPS_SKILLS_DIR` | 技能目录 | 全部 | 各引擎 get_env |
| `DEVOPS_EVENT_TYPE` | 事件类型 | 全部 | 各引擎 get_env |
| `AGENT_NAME` | Agent 显示名 | 全部 | `stream_agent` env_extra 前设置 |
| `GIT_ASKPASS` | git 凭据辅助脚本（BOT_GITCODE_TOKEN） | Coding/QA/SRE（git workflow） | `repogit.askpass_path` |
| `GITHUB_PUBLISH_REPO` | 评论指定发布目标 GitHub 仓库 | SRE（skill 发布） | `_apply_comment_repo_override` |
| `GITCODE_PUBLISH_REPO` | 评论指定发布目标 GitCode 仓库 | SRE（skill 发布） | `_apply_comment_repo_override` |
| `CLAWHUB_OWNER_HANDLE` | 评论指定 ClawHub 发布组织 | SRE（skill 发布） | `_apply_comment_repo_override` |

## 三、GitCode Token 互斥注入（关键）

`_assemble_env`（`agent/executor.py`）按执行类型**互斥注入** token：

```
Chat 任务  → 只注入 USER_GITCODE_TOKEN（用户身份；不用 bot token 冒充用户）
事件任务  → 只注入 BOT_GITCODE_TOKEN（仓库身份；不注入用户 token）
```

- 二者**不同时注入**（防用户对话上下文暴露仓库 bot token）。
- GitHub 仓库额外注入 `BOT_GITHUB_TOKEN` / `USER_GITHUB_TOKEN`。
- 用户云凭据（华为云/codearts/自定义）与 GITCODE token 无关，chat 与 devops **都按 user_id 注入**。

## 四、按 Agent 类型的可用性

| Agent | 仓库身份 token | 用户身份 token | 华为云 AK/SK | 场景字段 | 发布目标 |
|-------|----------------|----------------|--------------|----------|----------|
| VOD | BOT_GITCODE_TOKEN | — | 按 owner | ✓ | — |
| Issue | BOT_GITCODE_TOKEN | — | 按 owner | ✓ | — |
| Coding | BOT_GITCODE_TOKEN | — | 按 owner | ✓（工作分支） | — |
| QA | BOT_GITCODE_TOKEN | — | 按 owner | ✓ | — |
| SRE | BOT_GITCODE_TOKEN | — | 按 owner | ✓ | ✓ |
| Chat | — | USER_GITCODE_TOKEN | 按 user_id | — | — |

> 「按 owner」= 仓库归属用户配置的华为云凭据（`_inject_cloud_credentials`）。
> Chat 用当前登录用户身份；事件任务用仓库 bot 身份。

## 五、Skill 开发建议

1. **读 GitCode 仓库信息**：
   ```bash
   echo "repo=$REPO_FULL_NAME issue=$ISSUE_NUMBER"
   curl -sH "Authorization: Bearer $BOT_GITCODE_TOKEN" \
     "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME"
   ```
2. **调 Grape 内部 API**：用 `GRAPE_SERVER_URL` + `GRAPE_SESSION_TOKEN`（鉴权 Bearer）。
3. **cloud-deploy 类 Skill**：读 `HUAWEICLOUD_SDK_AK/SK`（SRE，仓库 owner 配置后注入）。
4. **场景自定义字段**：读 `GRAPE_CF_MAP` 按 key 名取 `GRAPE_CF_N` 值。
5. **git 操作**：用 `GRAPE_WORK_BRANCH` 作工作基线；`GIT_ASKPASS` 已注入 `$BOT_GITCODE_TOKEN`。
6. **token 安全**：`BOT_GITCODE_TOKEN`/`USER_GITCODE_TOKEN` 是敏感变量——**不要 printenv 打印/写入日志**；
   平台已有脱敏（[REDACTED]）。token 仅在进程内有效、删除即失效（GRAPE_SESSION_TOKEN 生命周期=任务）。

## 六、关键代码位置

| 环节 | 位置 |
|------|------|
| env_extra 组装 | `src/api/event_pipeline.py` `_execute_agent`（~L1653 起） |
| 场景字段注入 | `_inject_custom_fields_env` / `_inject_cloud_credentials` / `_apply_comment_repo_override`（L1742-1858） |
| 会话级 token | `src/api/auth_token.py` `issue_session_token`（TTL 6h） |
| token 互斥注入 | `src/agent/executor.py` `_assemble_env`（L212-254） |
| env 落库 | `src/runtime/service.py` `enqueue_task` env_extra（L574-591） |
| Worker 侧加载 | `src/runtime/agent.py` `_execute_task`（~L950） |
| 引擎 get_env | `src/runtime/opencode.py` / `codearts.py` / `hermes.py` / `codex.py` |