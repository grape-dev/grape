# Plan: Issue #657 技能 scope 校验（apply 拦截 + 派发 fail-fast）

## 定位（代码勘察结论）
- 技能 scope：`skill` 表 `scope ∈ {official, repo, user}`（`src/api/db_setup.py:96`）。
- 场景 scope：`devops_pipeline_scene` 无 scope 列，方案 B 后仅 repo 级（repo_key 非空）。
- apply 路径：`src/api/routes/scenes.py apply_scene`（Pass1 只读解析 → Pass2 落库），目前 `resolve_skill_id` 失败仅 skill_id=0 不报错。
- 保存路径：`create_scene` / `update_scene`（同文件）。
- 派发路径：`src/api/event_pipeline.py run_pipeline`（issue 场景覆盖 2319-2332 / PR 订阅 2292-2308 / 回退 2340-2345）；A2A：`src/api/routes/webhook.py` 1156-1168。`resolve_skill_id`（`src/user/db.py:1268`）返回 0 时现状是带着 skill_id=0 继续派发 → 1800s 超时。
- 失败事件模板：`_fail_event`（Issue #418 模式）与 webhook A2A 失败留痕事件。

## 改动
1. `src/vcs/event_types.py`：新增 `SKILL_RESOLVE_FAILED = "skill_resolve_failed"` 错误事件类型。
2. `src/user/db.py`：新增 `get_skill_scope_info(skill_name, repo_key)` —— 按 repo → official → user 顺序查 skill 表，返回 `{found, matched, scope, skill_id, owner_id}`（user-only 即 scope 不匹配）。
3. `src/api/routes/scenes.py`：
   - `apply_scene` Pass1：节点技能存在但 scope=user（无 repo/official 同名可用）→ 400「技能 X scope=user 与场景 scope=repo 不匹配，无法引用」，落库前拒绝；
   - `create_scene` / `update_scene`：同规则校验（保存即拦截）。
4. `src/api/event_pipeline.py`：新增 `_fail_skill_resolve_event()` —— 产生 `skill_resolve_failed` 错误事件（status=failed，result 带 `[skill_resolve_failed]` 前缀文案，含场景/节点/技能名/原因），接入三个派发前解析点（issue 场景覆盖 / PR 订阅 / 回退解析），解析失败立即返回不再派发。
5. `src/api/routes/webhook.py`：A2A issue 场景技能覆盖解析失败（`_new_sid==0`）→ 同错误事件 + 返回 failed 响应。

## 兼容性
- 校验仅在「技能确以 user scope 存在（且无 repo/official 同名）」或「派发前解析不到 skill」时触发；技能不在 skill 表的存量场景在 apply 阶段维持原行为（与 test_issue_524 等存量用例兼容），由派发兜底接管。
- fail-fast 仅在解析失败分支触发，其余派发路径不动。

## 测试（TDD）
`tests/test_issue_657_skill_scope_check.py`：apply 拦截（user/repo/official 三态）、保存拦截、派发兜底（event_pipeline 三点 + webhook A2A）、smoke 回归（user 技能 + repo 场景全链：apply 拒绝 + 强行落库后派发 fail-fast）。
