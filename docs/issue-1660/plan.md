# Plan: Issue #1660 — 统一 REPO_OWNER/REPO_NAME 派生

## 方案概述

在 `src/repo_key.py` 新增唯一派生入口 `derive_repo_identity(repository, repo_key)`，返回
`(full_name, owner, name)`：

1. 优先拆 `repository`（`REPO_FULL_NAME`，先剥 provider 前缀）；
2. `repository` 为空/无法拆出 owner+name → 回退拆 `repo_key`，并 `log.warning` 告警；
3. 二者均为空或伪仓库（`chat:`）→ 返回空三元组，由调用方决定不注入。

## 改动点

| 模块 | 改动 |
|------|------|
| `src/repo_key.py` | 新增 `derive_repo_identity` 单一派生入口 |
| `src/api/event_pipeline.py` | 失败快照 / 入队 / `build_task_env` 三处 env 组装改走派生入口 |
| `src/runtime/direct_executor.py` | `_execute_agent_inner` 由 `repository` 派生，移除用 `exec_account` 拆分 |
| `src/runtime/scheduler.py` | autopilot env 补齐 `REPO_OWNER/REPO_NAME` |
| `src/api/chat_session_env.py` | 新增 `chat_repo_env`，`chat_session_env_extra` 合并仓库身份变量 |
| `src/api/routes/chat.py` | 4 处 chat 入队/env 组装注入 `chat_repo_env(repo_key)` |
| `src/im/vod_handler_async.py` | worker 入队与 direct 执行注入仓库身份变量 |
| `tests/test_issue_1660_repo_env_derivation.py` | 新增多路径一致性/异常输入单测 |

## 关键决策

- **单一派生入口**：避免各组装点自行拆分再次漏改旁路。
- **REPO_FULL_NAME 权威**：`ACCOUNT`/`exec_account` 不再参与 owner 推导，消除触发人≠owner 缺陷。
- **伪仓库跳过**：`chat:{id}` 非真实仓库，不注入仓库身份变量，保持一般对话行为不变。

## 风险

| Risk | Mitigation |
|------|-----------|
| 旁路遗漏 | 全局检索 `REPO_OWNER/REPO_NAME` 组装点并清单化，逐点替换 |
| 历史行为依赖 | 真实仓库取值语义与文档一致；伪仓库路径不变 |