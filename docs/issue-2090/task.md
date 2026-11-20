# Task Checklist: Issue #2090

- [x] Task 1: 数据库与 DAL
  - Acceptance: organization 与 managed_repos 新列存在，状态与归属可读写。
  - Verify: `python3 -m pytest tests/mgmt/test_org_provisioning.py -q`
  - Files: `src/api/db_setup.py`, `src/domains/mgmt/user/db.py`, `src/domains/mgmt/user/org_db.py`

- [x] Task 2: 组织场景模板
  - Acceptance: 场景 ID 为 `custom-org-<uuid>`，`work_branch=main`，节点为 intake -> dispatch -> aggregate。
  - Verify: provisioning 测试读取场景表与 `get_pipeline_config`。
  - Files: `src/api/pipeline_config.py`

- [x] Task 3: Provisioning 状态机
  - Acceptance: 团队组织 pending -> ready；失败 failed 可 retry；重复执行幂等。
  - Verify: `python3 -m pytest tests/mgmt/test_org_provisioning.py -q`
  - Files: `src/domains/mgmt/org_provision.py`

- [x] Task 4: Provisioning 路由
  - Acceptance: 创建组织触发后台任务，查询与 retry 完成鉴权和状态契约。
  - Verify: `python3 -m pytest tests/mgmt/test_org_provisioning.py -q`
  - Files: `src/domains/mgmt/routes/orgs.py`

- [x] Task 5: 组织仓库绑定
  - Acceptance: 关联/解绑/迁移、单归属、组织仓库保护、配额与审计符合契约。
  - Verify: `python3 -m pytest tests/mgmt/test_org_repo_binding.py -q`
  - Files: `src/domains/mgmt/routes/org_repos.py`, `src/domains/mgmt/auth/audit.py`

- [x] Task 6: 路由注册与回归
  - Acceptance: 新路由可访问，现有 mgmt 测试通过。
  - Verify: `python3 -m pytest tests/mgmt -q`
  - Files: `src/api/main.py`, `src/api/grape_server.py`

- [x] Task 7: 审查反馈修复
  - Acceptance: 同组织关联幂等、个人组织按未归属、建仓后崩溃可认领、运行中派发拒绝解绑、多实例 provisioning 使用 DB 租约。
  - Verify: `python3 -m pytest tests/mgmt/test_org_provisioning.py tests/mgmt/test_org_repo_binding.py -q`
  - Files: `src/domains/mgmt/org_provision.py`, `src/domains/mgmt/routes/org_repos.py`, `src/domains/mgmt/routes/orgs.py`, `src/domains/mgmt/user/org_db.py`

- [x] Task 8: 统一组织绑定保护与派发租约
  - Acceptance: `/api/repos/migrate` 复用 transfer DAL 并写审计；provisioning 落组织归属复用 bind DAL 校验配额；解绑与任务入队共享仓库级分布式租约。
  - Verify: `python3 -m pytest tests/mgmt/test_org_provisioning.py tests/mgmt/test_org_repo_binding.py tests/mgmt/test_repo_migrate.py tests/mgmt/test_repo_migrate_orgs.py -q`
  - Files: `src/domains/mgmt/user/org_db.py`, `src/domains/mgmt/org_provision.py`, `src/domains/mgmt/routes/repos.py`, `src/domains/runtime/engine/service.py`, `src/domains/runtime/pipeline/event_pipeline.py`, `src/kernel/lock.py`
