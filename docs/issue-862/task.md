# Task Breakdown: Issue #862

## M1（P0 止血，本 PR）
- [x] T1: `src/api/routes/scenes.py` — 常量 `EXPERIENCE_TEMPLATE_CREATE_TIMEOUT = 45` + 替换内联 `timeout=30` + 注释更新
- [x] T2: `tests/test_issue_862_experience_timeout.py` — 常量值与路由使用断言
- [x] T3: `docs/issue-862/` — spec / plan / task

## M2（P1 治本，按排期另立 PR）
- [ ] T4: 模板仓瘦身 — `jiuwenswarm-develop` 模板 docs/（52MB）、resources/（16MB）移出模板（模板仓侧变更）
- [ ] T5: 体验改异步 — task_id + expProgress 轮询，消除 504 假死线程（前后端契约改造）

## 验收对照

| Spec 验收项 | 实现 | 验证 |
|------------|------|------|
| 超时 30→45 且常量化 | T1 | test_timeout_constant_value / test_route_uses_constant |
| 45s 在 nginx/前端 300s 覆盖内 | 既有部署参数核对 | 文档记录（plan.md 设计决策） |
| 全新建仓实测无 504 | 部署后回归（M3） | SRE / 验收执行 |
| 其他用户接口延迟无劣化 | 部署后回归（M3） | api_latency_log 对照 |
