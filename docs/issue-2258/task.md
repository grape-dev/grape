# Issue #2258 任务拆解

1. [x] 需求/方案文档落库。
2. [x] `tests/mgmt/test_org_manual_bind.py` 先写失败测试（成功绑定/审计/403/404/409/替换保护/回归）。
3. [x] `org_db.py` 补组织仓库绑定、旧标记清理、运行中 dispatch 查询。
4. [x] `audit.py` 注册 `org_provision_manual_bind`。
5. [x] `org_provision.py` 实现 `manual_bind_org_repo`（校验/替换保护/场景补齐/审计）。
6. [x] `routes/orgs.py` 新增 PUT 路由与 owner/admin 鉴权。
7. [x] 全量 mgmt 相关测试通过，自审查、建 PR、通知 QA。