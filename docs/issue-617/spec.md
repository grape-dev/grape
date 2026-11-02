# Issue #617 Spec — 场景 qa 节点技能名不一致致 materialize 降级

## 背景
grape-skill-dev 场景 qa 节点 skill 字段配置为 `skill-batch-validator`，官方技能库实际技能名为 `grape-skill-validator`（id=2789）。按名物化必然失败，Agent 需 `grape skills list` + grep + `grape skills detail` 三步手工定位，产生降级。

## 目标
1. 配置对齐：grape-skill-dev 场景 qa 节点 skill 字段改为 `grape-skill-validator`。
2. 平台兜底：materialize 按名未命中时按 agent_type（如 qa）回退官方检查器，失败/回退提示中给出 `grape skills detail --skill_id <id>` 获取路径。

## 现状与代码落点
- 场景配置权威源在 grape-skills 仓库（scene_sync 30 分钟同步），本仓 `scenes/grape-skill-dev/scene.json` qa 节点已是 `grape-skill-validator`（cc77775b5 引入即正确）。线上/库内旧数据未对齐部分由本 Spec 第 2 点的 agent_type 兜底消化，并需在场景源仓库同步修正（见 plan）。
- 按名解析/物化路径：
  - direct：`src/api/event_pipeline.py::_materialize_direct_workdir`（skill 按名 resolve_official_skill_path）。
  - worker：`src/api/routes/runtime.py::_attach_skill_obs`（claim 现签 zip URL，按名 resolve_skill_id）→ `src/runtime/agent.py`（zip/skill_files 物化 + `_fallback_official_skill` 官方包回退）。
  - 共用：`src/runtime/skill_materialize.py::materialize_skill_with_fallback`（零 DB 依赖）。

## 实现要点
1. `src/user/db.py` 新增 `resolve_official_checker_skill(agent_type)`：skill 表取 is_active=1 且 scope='official' 的该 agent_type 官方技能，优先名含 validator/check/review/lint/inspect 的检查器技能，返回含 id 的 dict。
2. `materialize_skill_with_fallback` 增加可选 `agent_type` + `agent_type_fallback` resolver 注入（保持零 DB 依赖）：按名官方回退失败后按 agent_type 回退官方检查器目录物化，detail 带 `grape skills detail --skill_id <id>`。
3. direct 路径 `_materialize_direct_workdir` 传入 agent_type 与 DB resolver；降级/回退提示含 `--skill_id`。
4. worker 路径：claim 端 `_attach_skill_obs` 按名未命中时按 agent_type 解析官方检查器并签其 zip URL，同时下发 `skill_fallback`（skill_name/skill_id）；`agent.py` zip 目标校验与 `_fallback_official_skill` 认可 skill_fallback，提示带 `--skill_id`。

## 验收标准
- [ ] materialize 按名未命中时按 agent_type 回退官方检查器（qa → 检查器技能）。
- [ ] 回退/失败提示含 `grape skills detail --skill_id <id>` 获取路径。
- [ ] 配置对齐由场景源仓库/平台数据修正（本仓 scene.json 已正确，随附说明）。

优先级 P2；分支 feat/issue-617，base weekly-08-mys（GRAPE_WORK_BRANCH 注入值）。
