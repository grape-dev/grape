# Issue #617 Plan — materialize 按名未命中按 agent_type 回退官方检查器

## 改动清单（原子提交序列）
1. `src/user/db.py`：新增 `resolve_official_checker_skill(agent_type)`（DB 查询，检查器优先）。
2. `src/runtime/skill_materialize.py`：`materialize_skill_with_fallback` 增加 `agent_type` / `agent_type_fallback` 参数与回退分支（零 DB 依赖，resolver 由服务端注入）。
3. `src/api/event_pipeline.py`：`_materialize_direct_workdir` 接线（传 agent_type + DB resolver；提示含 `--skill_id`）。
4. `src/api/routes/runtime.py`：`_attach_skill_obs` 按名未命中 → agent_type 检查器 zip URL + `task["skill_fallback"]`。
5. `src/runtime/agent.py`：zip 目标校验认可 `skill_fallback`；`_fallback_official_skill` 增加 skill_fallback 分支，提示含 `--skill_id`。
6. `tests/test_skill_materialize.py`：新增 agent_type 回退（fallback 成功 / resolver 未命中仍 degraded / direct 端接线）测试。

## 设计要点
- 兜底选择规则：`scope='official' AND is_active=1 AND agent_type=?`，技能名匹配 `valid|check|review|lint|inspect`（不区分大小写）者优先，其次 id 最小者；qa 场景即命中 `grape-skill-validator`。
- worker 无库：agent_type→检查器解析一律在服务端（claim/direct）完成，worker 只消费 `skill_fallback`（名字 + id），保持 `skill_materialize.py` 零 DB。
- 不修改 skill 表/场景数据本身（配置对齐在场景源仓库与平台数据侧完成，本仓 scene.json 已为正确值）。

## 测试
- 单测：`python3 -m pytest tests/test_skill_materialize.py -q`（旧用例全绿 + 新增 4 例）。
- 回归：`python3 -m pytest tests/test_skill_materialize.py tests/test_transient_retry.py -q`（冒烟相关模块导入）。
