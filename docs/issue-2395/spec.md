# Issue #2395 — 统一各引擎运行时四变量注入契约

## 目标
让 `ACCOUNT`、`GRAPE_ISSUE_ID`、`DEVOPS_EVENT_TYPE`、`GRAPE_WORK_BRANCH` 在
codex/opencode/dsh/hermes/codearts/aishell-dsh/hwcloud/atomcode（含 ACP 路径）下
按同一口径注入，消除「文档声明注入、实测为空」的路径差异。

## 变量语义（契约）
| 变量 | 语义 | 权威来源 |
|------|------|---------|
| `DEVOPS_EVENT_TYPE` | 本次任务真实事件类型 | `executor.stream_agent(event_type)` |
| `ACCOUNT` | 触发人账号（不是仓库名/仓库全名） | 事件 actor / env_extra |
| `GRAPE_ISSUE_ID` | 平台内部需求卡片 id（issue 表主键） | `resolve_issue_card_id(repo_key, ISSUE_NUMBER)` |
| `GRAPE_WORK_BRANCH` | 场景开发基线分支 | 场景快照 / `_read_scene_work_branch` |

> `GRAPE_ISSUE_ID` ≠ `ISSUE_NUMBER`：前者是 issue 表主键（如 13465），
> 后者是仓库 Issue 编号（如 2395）。技能侧按仓库编号使用时必须用 `ISSUE_NUMBER`。

## 验收
- 各引擎 `get_env()` 不再预置 `DEVOPS_EVENT_TYPE=""`。
- 任务级 `env_extra` 合并后由统一装配函数以真实 event_type 覆盖并补齐其余变量。
- 缺失时告警含变量名与引擎标识、不含敏感值，任务继续执行。
- 单测覆盖各适配层与 ACP/CLI 路径。
