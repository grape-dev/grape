# Spec: Issue #573 — BOT_USERNAME/AGENT_NAME 环境变量未注入，Step 4 机器人接管 assignee 无法落地

> 来源：Issue Agent Spec（评论 id=197165041，评级 ready）| 目标基线分支：weekly-08-mys（服务端注入 GRAPE_WORK_BRANCH 权威值）

## 目标

服务端任务派发时补注入 `BOT_USERNAME` / `AGENT_NAME` 环境变量，使 issue agent（issue-runbook Step 4 机器人接管 assignee）及其它 devops agent 可正常读取。

## 根因（代码实勘）

- BOT_USERNAME 注入逻辑已存在于 `src/api/event_pipeline.py`，但**仅在 `agent_type == "coding"` 时注入**，共三处重复块：
  1. 正常入队路径（~L2982）
  2. direct 直连执行路径（~L3955）
  3. 重派路径 `build_task_env()`（~L3282）
- issue agent（issue-runbook Step 4 的执行者）不在门控内 → `BOT_USERNAME` 为空。
- `AGENT_NAME` 仅 direct 模式经 `stream_devops_agent(agent_name=...)` 参数注入；worker 模式 env 来自任务行 `env_extra` 快照，`AGENT_NAME` 从未写入 → worker 模式两变量均空。

## 范围

### In Scope

- `src/api/event_pipeline.py`：
  - 抽公共 helper `_resolve_bot_username(repo_key)`（repo 级 managed_repos.bot_username → 全局 bot（按 provider）→ host_user 平台用户名，即现状三级兜底逻辑）；
  - 抽公共 helper `_inject_bot_agent_env(env_extra, repo_key, agent_type)`：无 agent_type 门控注入 `BOT_USERNAME` + `AGENT_NAME`（源 `get_agent_display_name(agent_type)`）；
  - 三处 coding 门控块统一替换为 helper 调用（含重派路径 build_task_env）。
- `src/scene_custom_env.py`：`AGENT_NAME` 加入 `_RESERVED_ENV_NAMES`（防场景自定义变量同名冲突覆盖）。
- 测试 `tests/test_issue_573_bot_agent_env_injection.py`：注入逻辑（repo 级/全局兜底/空值）、非 coding agent（issue/qa）注入、AGENT_NAME 注入、build_task_env 路径、三处调用点统一走 helper（防第四处旁路回归）。

### Out of Scope

- `issue-runbook.md` Step 4 / `spec-generation.md` 空值降级说明：技能资产不在本仓库（同 #530/#533 先例，资产在外部技能仓），文档侧改动需在技能仓发起；本 PR 在 Issue 评论中说明降级约定：`BOT_USERNAME` 为空时 Step 4 跳过 assignee 变更并输出降级提示。
- 其它环境变量（DEVOPS_SKILLS_DIR，#63/#564）。

## 验收标准

- [x] 非 coding agent（issue 等）任务 env 注入 BOT_USERNAME/AGENT_NAME（测试覆盖）
- [x] repo 级 bot_username 缺省时按 provider 走全局/host_user 兜底（测试覆盖）
- [x] 全部兜底为空时注入空串（与原 coding 行为一致，下游 skill 按空值降级）
- [x] 重派路径 build_task_env 同样注入（测试覆盖）
- [x] 现有测试回归通过
