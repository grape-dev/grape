# Task: Issue #541

- [x] T1 红：测试——apply_market_scene_by_name 返回仓内副本真实 scene_id；模板覆盖写真实 ID（_apply_template_scene）
- [x] T1 绿：pipeline_config.py 返回值改造 + templates.py 覆盖逻辑（失败保留默认并告警）
- [x] T2 红：测试——set_repo_scene 对仓外 scene_id 告警但照常写入
- [x] T2 绿：user/db.py set_repo_scene 仓内存在性校验
- [x] T3 红：测试——scene_dispatch_failure_reason 区分「未绑定引擎/全部离线/仓内不存在」；get_scene_pool_engine_ids SRE 仓内未命中 → None 不回退全局
- [x] T3 绿：engine_select.py 校验 + event_pipeline/webhook/events 报错文案接入
- [x] T4 红：测试——repair_scene_bindings 存量改绑 dev_scene 与 issue.scene_id（幂等）
- [x] T4 绿：scene_binding_repair.py 新模块 + main.py lifespan 挂载
- [x] 回归：python3 -m pytest 相关测试全绿
- [x] 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base=local-liufei）→ Issue 评论 → A2A QA
