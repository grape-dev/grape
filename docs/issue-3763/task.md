# 任务拆解：Issue #3763

| # | 任务 | 状态 |
|---|------|------|
| 1 | `db_setup.py` 新增 runtime 域 `autopilot_executions` 建表迁移（幂等补列/索引） | ✅ |
| 2 | `scheduler.py` 表存在性自检（启动 + 每 tick）+ `_create_execution` fail-fast | ✅ |
| 3 | 新增「缺表自检失败」用例 | ✅ |
| 4 | 新增「三库拓扑执行链路只落 owner 域」用例 | ✅ |
| 5 | 运行既有相关测试 + 新增用例通过 | ✅ |
| 6 | 自审查 + 开源前置增量检查 | ✅ |
| 7 | 提交/推送/建 PR、Issue 评论、A2A 通知 QA | ✅ |
