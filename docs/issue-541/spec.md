# Spec: Issue #541 — 模板建仓将模板场景名误写为仓库默认场景 ID

> 继承 Issue 评论中 issue agent 发布的 Spec Document（评级 ready），本文件为 coding 侧落地副本。

## Objective
修复模板建仓场景 ID 传递断裂：`apply_market_scene_by_name` 返回仓内副本真实 scene_id，模板创建流程将该值（而非模板场景名如 `hello-world`）写入 `managed_repos.dev_scene`；派发侧对 scene_id 做仓内存在性校验并区分「场景未绑定引擎」与「引擎全部离线」报错；幂等扫描修复 `dev_scene` 与 `issue.scene_id` 存量错误绑定。

## Scope (In)
- `src/api/pipeline_config.py`：`apply_market_scene_by_name` 返回真实 scene_id（`str | None`，None 表失败，truthiness 兼容 `session_store.py` 布尔用法）；新增 `scene_exists_in_repo`
- `src/api/routes/templates.py`：模板覆盖逻辑写真实 scene_id，失败保留默认场景并告警（抽 `_apply_template_scene` 便于测试）
- `src/user/db.py`：`set_repo_scene` 仓内存在性校验（告警不阻断）
- 派发侧（`src/runtime/engine_select.py` + `src/api/event_pipeline.py` + `webhook.py` + `routes/events.py`）：仓内未命中告警且 SRE 不回退全局模板行；报错区分「场景未绑定引擎」/「引擎全部离线」/「场景仓内不存在」
- 存量修复 `src/api/scene_binding_repair.py`：幂等扫描 `managed_repos.dev_scene` 与 `issue.scene_id`，按 market_scene_id 匹配仓内副本改绑；`main.py` lifespan 启动挂载
- `tests/test_issue_541_template_scene_id.py`：模板写入真实 ID / 校验 / 报错区分 / 存量改绑四类回归

## Out of Scope
场景市场 UI、引擎健康自愈、onboarding Issue 生成策略、weekly-09 独立 ID 语义变更（沿用）。

## Acceptance Criteria
- 带 scene_id 模板新建仓，onboarding Issue scene_id 为仓内副本真实 ID，SRE 派发用场景内引擎
- 派发失败信息区分「场景未绑定引擎」与「引擎全部离线」
- 存量修复后受影响 Issue（gcw_3FZApeLT/hello-html-2 #1）重派可成功
- 现有测试全过 + 新增测试覆盖
