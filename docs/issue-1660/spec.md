# Spec: Issue #1660 — Agent 任务 env 注入：REPO_OWNER/REPO_NAME 由 REPO_FULL_NAME 统一派生

> 目标分支：weekly-mys（工作基线）｜来源：用户反馈 2874 / Issue #1660

## Objective

修复服务端 Agent 任务环境变量注入：`REPO_OWNER`/`REPO_NAME` 必须由 `REPO_FULL_NAME` 派生，
禁止以触发人账号（`ACCOUNT`）充当 `REPO_OWNER`、禁止 `REPO_NAME` 为空。技能按 runbook 使用
`$REPO_OWNER/$REPO_NAME` 拼接仓库路径，错误取值会导致 404/越权请求。

## 现象

实际运行 `REPO_FULL_NAME=acme-org/demo-portal`，但 `REPO_NAME` 为空、`REPO_OWNER=alice`（触发人账号）。

## 根因

`src/runtime/direct_executor.py` 的 `_execute_agent_inner`（direct 分支）此前以
`exec_account = evt["account"] or repository` 拆分 `REPO_OWNER`/`REPO_NAME`：
触发人账号无 `/` 时 `split_owner_repo` 返回 `(<触发人>, "")`，正好产生反馈形态。
入队路径虽以 `repository` 派生，但 `REPO_FULL_NAME` 缺失时回退带前缀 `repo_key` 且 `REPO_NAME` 为空；
chat/IM/VOD 等路径则完全未注入仓库身份变量。

## Scope

### In Scope
- 新增单一派生入口 `derive_repo_identity(repository, repo_key)`：以 `REPO_FULL_NAME` 为事实源，
  缺失/无法拆分时回退 `repo_key` 并告警。
- 全部 Agent 任务 env 组装路径统一走派生入口：事件入队、失败快照、事件重派（`build_task_env`）、
  direct 执行、autopilot、VOD，以及 chat/IM/Web 对话。
- `chat_repo_env(repo_key)` 供 chat 系列路径注入 `REPO_FULL_NAME/REPO_OWNER/REPO_NAME`，伪仓库（`chat:`）不注入。
- 单测覆盖「REPO_FULL_NAME 与触发人不一致」「REPO_NAME 为空」「回退 repo_key」等场景。

### Out of Scope
- 技能侧 runbook 一致性校验兜底（属 grape-skills 仓技能资产，另行建单跟进）。
- `ACCOUNT`、`GRAPE_WORK_BRANCH` 等其它变量语义调整。

## Acceptance Criteria
- [x] `REPO_FULL_NAME=Owner/Repo` 且触发人 ≠ owner 时，`REPO_OWNER=Owner`、`REPO_NAME=Repo`。
- [x] 事件入队、失败快照、事件重派、direct、autopilot、VOD、chat 全部注入路径口径一致。
- [x] `REPO_FULL_NAME` 缺失时回退 `repo_key` 拆分并告警，不使用触发人账号。
- [x] 新增测试覆盖上述场景，相关既有测试通过。
- [x] 技能 runbook 兜底校验转 grape-skills 仓单独建单跟进（在交付说明注明）。