# Issue #658 任务拆解（task）

| # | 任务 | 状态 |
|---|------|------|
| 1 | db_setup：agent_runtimes 增加 role_tags 列（DDL + 增量 ALTER） | done |
| 2 | runtime.py register：RuntimeInfo 接受 role_tags，INSERT/UPDATE 落库 | done |
| 3 | engine_select：默认角色映射 + engine_role_tags 解析（双轨） | done |
| 4 | engine_select：pick_engine（含 SRE 路径）角色硬过滤 + 无候选显式报错 | done |
| 5 | engine_select：get_scene_pool_engine_ids 角色硬过滤 + 无候选返回 None | done |
| 6 | engine_select：_candidate_reject_reasons 补 role_mismatch 取证 | done |
| 7 | service.py：claim_by_tag 认领侧角色守卫（跳过不匹配任务，不阻塞队列） | done |
| 8 | 回归测试：#90 反例 + 正向用例 + 显式 role_tags + 未知类型兼容 | done |
| 9 | 全量相关测试回归（test_engine_select*.py 等） | done |
