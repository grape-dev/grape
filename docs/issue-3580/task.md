# Issue #3580 — 任务拆解（Task）

## P1 卡体系多实例
- [x] T1 `PlatformCard.ca_bundle` + provider 名/host/URL/CA 校验
- [x] T2 `configs/platforms/gitlab.json` 增 `ca_bundle`、`webhook_dialect`
- [x] T3 `SCHEMA.md` 补 CA 与实例卡说明
- [x] T4 卡校验测试（R1–R4）

## P2 传输安全
- [x] T5 `repogit._run_git` env 注入，移除 argv token
- [x] T6 调用点传 repo_key/CA
- [x] T7 argv/env 断言测试（R9）

## P3 事件持久队列
- [x] T8 `grape_t_webhook_inbox` DDL + 迁移注册
- [x] T9 `webhook_inbox` enqueue/claim/renew/finish
- [x] T10 通用 webhook 验签后 host 校验 + 入队
- [x] T11 Event 进程 worker 消费 + 测试（R10）

## P4 托管全流程 + PAT + 前端
- [x] T12 `GitLabProvider` 多实例 + 分页 + Maintainer（R5）
- [x] T13 `gitlab_v4` webhook 家族 + 托管全流程声明（R6–R8）
- [x] T14 `/api/repos/{provider}/connection` PAT（R11）
- [x] T15 前端实例选择/托管入口（R12 登录渠道不变）