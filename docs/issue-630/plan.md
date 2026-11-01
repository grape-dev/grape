# Plan: Issue #630

## 改动清单（原子提交序列）

1. `src/runtime/service.py`：新增 `verify_skill_materializable()`（技能物化完整性校验，
   OBS 对象 / 本地目录 / 官方内置包三级内容源判定；技能未配置恒通过）。
2. `src/api/event_pipeline.py`：`_enqueue_pipeline_task` 在 `enqueue_task` 前挂门禁，
   失败 `log.warning` + `_fail_event` 阻断（场景技能 override / PR 订阅 / 广播任务
   全部覆盖；技能为空不阻断）。
3. `src/api/routes/skills.py`：`/by-id/{skill_id}` 路径参数 int→str，非数字名称走
   `resolve_skill_id`（user scope 优先）解析为 sid；404 兜底；数字行为不变。
4. `src/user/db.py`：`create_skill()` 注册后校验内容源，空壳行显式 warning（发布链路
   断点审计）；不改激活语义（保持 #287 测试契约）。
5. `tests/test_issue_630_skill_gate.py`：门禁（未配置通过/空壳行阻断/OBS 源通过/
   本地完整目录通过/官方包兜底）+ by-id 名称解析（用户级技能按名 200、未知名 404）。

## 不做（Out of Scope）

- `enqueue_task` 全局抛错（会破坏 #287 autopilot 降级契约，门禁只挂 Pipeline）。
- 历史空壳数据自动修复（重传动作归技能所有者，见 spec 风险表）。
- #602/#607/#610/#617 独立修复。

## 风险

| 风险 | 缓解 |
|------|------|
| 门禁误阻断正常派发 | 技能未配置恒通过；official 内置包兜底；仅"配置了技能但内容源全缺失"才阻断 |
| OBS read_bytes 增加派发延迟 | 技能包 <10MB 上限、通常 <100KB，单次读可接受 |
| by-id 参数 int→str 影响契约 | 数字路径行为不变；openapi 路径参数类型变 string，测试同步校验 |
