# Issue #338 任务拆解（task）

- [x] 定位设备列表端点与过滤链路（settings.py 两端点 + _build_runtime_devices + _visible_credential）
- [x] 根因确认（org_id 隔离 vs user scope 归属豁免缺失；对照 dashboard 语义）
- [x] 修复 get_agent_runtimes 引擎循环个人组织分支
- [x] 修复 get_agent_runtimes 设备视图 cred_map 过滤
- [x] 修复 get_runtime_devices cred_map 过滤
- [x] 新增 tests/test_issue_338_worker_visibility.py（6 用例，全部通过）
- [x] 回归：tests/test_api_routes.py::TestSettings 17 通过；test_issue_277 / test_skills_org_visibility 通过；
      test_runtime_protocol.py 失败项经 stash 对照确认为分支既有环境/陈旧覆盖问题（与本改动无关）
- [x] 提交并创建 PR（base: weekly-ljt）
