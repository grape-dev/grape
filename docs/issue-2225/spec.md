# Spec: Issue #2225 — 共享 worker 并发 Agent 会话暂存文件隔离

> 评级：ready | 目标开发分支：arch-weekly-11-zj | 本文件为仓库内实现索引副本（Issue 已发布 Spec 评论 200334095）。

## Objective

共享 worker 上并发 Agent 会话的暂存目录按会话/任务隔离，平台下发会话级临时目录变量
（`TASK_TMPDIR`/`SESSION_DIR`），发布前校验正文与当前 Issue 号一致性，并提供会话结束清理与
`/tmp/opencode` 存量平铺残留治理，消除「把其他 Issue 正文写进线上 Issue/PR」的数据正确性风险。

## Scope

### In Scope

1. worker 会话运行时：为每个任务提供唯一暂存目录（优先平台注入变量；缺失回退 `mktemp -d` 并告警）。
2. 环境变量注入链路：平台下发 → worker 注入 → agent 进程可见可写。
3. 发布前一致性断言：写 Issue/PR 正文前校验 Issue 号与当前任务一致，不一致非零退出且无线上写入。
4. 会话结束清理自身暂存子目录；sweeper 兜底清理 `/tmp/opencode` 存量平铺文件与超 TTL 会话子目录。
5. 本仓文档/说明同步（grape-skills runbook 全量改版属伴随项 grape-skills#3220，不在本任务范围）。

### Out of Scope

- grape-skills 仓的 runbook 全量改版（已拆分 grape-skills#3220）。
- 跨 worker 的分布式暂存治理（单机 `/tmp` 场景已覆盖）。
- 线上 Issue/PR 历史数据修复（尚无线上污染实例）。

## Acceptance Criteria

- [ ] 并发两会话各写同名 body.md，读回内容均属于自身任务（互不污染）。
- [ ] 暂存路径含 session_id/task_id 或由 mktemp 生成。
- [ ] `TASK_TMPDIR`/`SESSION_DIR` 在会话内可见且可写；缺失时回退 `mktemp -d` 且日志有告警。
- [ ] 发布前校验命中不一致：命令非零退出且线上 Issue 无变更。
- [ ] 会话结束后暂存子目录被清理，`/tmp/opencode` 无跨会话长期残留。
- [ ] 所有现有测试通过；新增测试覆盖并发隔离/回退/断言/清理四类场景。