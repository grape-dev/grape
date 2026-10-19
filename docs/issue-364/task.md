# Task: Issue #364 — 任务拆解

- [x] T1 后端：`settings.py` 新增 `GET /agent-runtimes/{rid}` 详情端点（复用列表可见性校验，404 分支）
- [x] T2 文档：`help/api/13-settings.md` 补端点文档块（过 test_api_docs_sync）
- [x] T3 测试：`tests/test_api_routes.py` 新增详情端点测试（通过/拒绝两分支）
- [x] T4 前端：`runtimes.js` 新增 `_rtEngineIndex` 索引 + `renderRuntimes` 后重建
- [x] T5 前端：`openEngineDetail` 多源查找 + 异步回退（loading 态/toast/_partial 补全）
- [x] T6 验证：`python3 -m pytest tests/test_api_routes.py tests/test_api_docs_sync.py tests/test_openapi.py` + 前端 JS 语法检查
- [x] T7 自审查 → 提交 → push → 建 PR（base=功能分支）→ Issue 评论 → A2A QA
