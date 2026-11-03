# Phase 4 Plan（Issue #699）：四进程拆分（Mgmt + Event + Worker + Insight）

## 拆解与顺序

| # | 子任务 | 产出 | 依赖 |
|---|--------|------|------|
| 1 | 需求文档/方案/拆解 | docs/issue-699/phase-4-{spec,plan,task}.md | 设计文档 §5/§8/§9/§10/§11 |
| 2 | 共享脚手架 | src/api/server_common.py + main.py 收敛复用 | 1 |
| 3 | 共享任务启动器 | src/api/server_tasks.py + main.py lifespan 收敛 | 2 |
| 4 | 四进程服务模块 | src/api/grape_{server,event_server,worker_server,insight_server}.py | 2,3 |
| 5 | apps 入口 | apps/mgmt/main.py、apps/runtime/{webhook,worker}/main.py、apps/insight/main.py | 4 |
| 6 | sweep 拆分 | cleanup.py: sweep_events_loop / sweep_tasks_loop | 独立 |
| 7 | table_maintenance 拆分 | table_maintenance.py: tasks/messages 双 loop | 独立 |
| 8 | nginx + systemd | deploy/nginx-grape-four-process.conf + deploy/systemd/*.service | 4,5 |
| 9 | 验收测试 | tests/test_issue_699_phase4_servers.py（TC-4.1~4.12） | 4~8 |
| 10 | 自审查 + 前置检查 + PR/A2A | 更新 PR #836 body、Issue 评论、A2A QA | 9 |

## 提交拆分（原子提交，全英文 message）

1. `docs: #699 add Phase 4 spec, plan and task breakdown` — docs/issue-699/phase-4-*
2. `refactor: #699 extract shared server scaffolding into server_common` — server_common.py + main.py
3. `refactor: #699 move process task starters into server_tasks` — server_tasks.py + main.py（lifespan 收敛）
4. `feat: #699 add four-process split server apps and entrypoints` — grape_*.py + apps/*
5. `feat: #699 split sweep_loop into sweep_events_loop and sweep_tasks_loop` — cleanup.py
6. `feat: #699 split table_maintenance across worker and insight loops` — table_maintenance.py
7. `feat: #699 add four-process nginx config and systemd units` — deploy/*
8. `test: #699 add Phase 4 acceptance tests (TC-4.1~TC-4.12)` — tests/*

## 验证策略

- 单元级：TestClient(不进入 lifespan) 验证各进程路由可达；源码/符号断言验证定时任务分配、
  sweep/table 拆分、nginx/systemd 静态产物。
- 回归级：跑 Phase 3 测试 + arch 测试 + 既有冒烟（main app import、/health）等批次，
  确认 main.py 收敛重构无回归。
- 部署级（TC-4.8/4.9/4.10/4.12 中依赖 systemd/nginx 的运行态链路）留待 Phase 6
  双模式部署验证，本轮以静态校验 + 文档说明覆盖。

## 风险控制

- main.py 的默认行为保持“单进程全量”（四组任务并集 + 全量路由），避免测试/存量部署回归。
- 每个提交只做一件事；提交前过无关文件黑名单、push 前敏感增量扫描。