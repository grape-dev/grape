# Phase 4 Task（Issue #699）：四进程拆分 — 检查清单

## 编码清单

- [ ] src/api/server_common.py：build_app / CORS / metrics / request_id+log / 异常处理 / ping / health / frontend
- [ ] src/api/server_tasks.py：start_mgmt_tasks / start_event_tasks / start_worker_tasks / start_insight_tasks
- [ ] src/api/main.py：收敛复用 server_common/server_tasks（行为等价）
- [ ] src/api/grape_server.py（Mgmt :38080）
- [ ] src/api/grape_event_server.py（Event :38081）
- [ ] src/api/grape_worker_server.py（Worker :38082）
- [ ] src/api/grape_insight_server.py（Insight :38083）
- [ ] apps/mgmt/main.py、apps/runtime/webhook/main.py、apps/runtime/worker/main.py、apps/insight/main.py
- [ ] cleanup.py：sweep_events_loop + sweep_tasks_loop（保留 sweep_loop/sweep_once）
- [ ] table_maintenance.py：table_maintenance_tasks_loop + table_maintenance_messages_loop
- [ ] deploy/nginx-grape-four-process.conf（4 upstream + 路径分流）
- [ ] deploy/systemd/grape-{server,event-server,worker-server,insight-server}.service
- [ ] tests/test_issue_699_phase4_servers.py（TC-4.1~4.12）

## 验证清单

- [ ] python3 -m pytest tests/test_issue_699_phase4_servers.py 通过
- [ ] 回归批次（arch / phase3 / worker_package / 冒烟 import）通过
- [ ] scripts/check-arch.py RESULT: PASS
- [ ] pre-pr-check：无关文件拦截 0、敏感扫描 0、commit message 全英文
- [ ] PR #836 body 追加 Phase 4 章节并推送
- [ ] Issue #699 评论告知 + A2A QA（context 含 PR #836 与 TC-4.1~4.12）

## 收尾

- [ ] grape tasks complete（actions/conclusion/artifacts/issue_summary 真实填充）