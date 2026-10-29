# Task: Issue #512 — agent_runtimes 时间口径统一为 UTC

## 拆解

- [x] T1 文档：spec.md / plan.md / task.md（本目录）并提交
- [x] T2 红：新增 tests/test_issue_512_runtime_utc.py（写入 UTC/迁移幂等/读取归一/转正）→ 失败提交
- [x] T3 绿：save_agent_runtimes 参数化 UTC（UPDATE+INSERT）→ 通过提交
- [x] T4 绿：migrate_agent_runtimes_utc_caliber 幂等迁移 + 挂载刷新钩子
- [x] T5 绿：parse_dt 无时区语义对齐（mysql=CST / sqlite=UTC）
- [x] T6 绿：agent-runtimes 读取出口 normalize_event_time 兜底
- [x] T7 回归：`pytest -m atomic -k online_runtime_last_seen_not_in_future` 通过；
    相关既有测试（test_user_db / test_runtime_protocol / test_local_probe / test_health_scorer 等）不回归
- [ ] T8 自审查（5 轴 + 开源前置增量检查：无关内容拦截 + 敏感信息增量扫描）
- [ ] T9 建 PR（base=local-liufei）+ Issue 评论告知 + A2A QA

## 验收关联

- 验收命令：`pytest -m atomic -k online_runtime_last_seen_not_in_future`
- 验收标准见 spec.md「成功标准」

## 收尾记录

（开发完成后勾选并记录 PR 编号）

> 2026-10-29: T1-T7 完成；回归批次 A/B/C/D 失败集与基线完全一致（0 新增回归）。
