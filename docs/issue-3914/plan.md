# Issue #3914 方案设计（plan）

## 落点与修法
### B-1 入队回写解析结果（`src/domains/runtime/engine/service.py`）
`enqueue_task` 的 `if skill:` 块已解析 `_sid`（id 直查 / id 失效按名 / 无 id 按名）。
新增 `_resolved_skill_id` 承载解析结果，落库列改为 `skill_id = resolved or 调用方入参 or 0`。
不改 `skill_files` / `__obs_key__` 既有语义。

### B-2 direct evt 重建桥接（`src/domains/runtime/engine/local_claimer.py`）
`_rebuild_evt_from_task` 按任务行回填：
- `_skill_id`：`task["skill_id"]`（>0 时）；
- `_skill_path`：id 命中 → `get_skill_path(id)`；否则回退 `task["skill"]`（按名降级语义不变）；
- `_skill_obs_key`：从 `task["skill_files"]{<skill>: {"__obs_key__": key}}` 提取（存量行 id=0 时的取包桥）。

`__obs_key__` 提取抽为模块级 `_obs_key_from_skill_files(skill_files, skill)`，便于单测。

### C 复用既有 OBS 取包通道（`event_pipeline._skill_primary`）
`_skill_id` 为 0 但 evt 携带 `_skill_obs_key` → 直接 `get_skill_zip_bytes(key)` 走既有通道（与 worker 同源），
不新增通道；`_skill_id` 非 0 分支保持原逻辑（skill 表 `file_path` → OBS zip）。

### A 派发传权威 id（`event_pipeline._dispatch_pr_subscription_extra` / `_dispatch_pr_subscription_redispatch`）
两处在已解析的 `_sub_uid` 之后调用 `resolve_scene_node_skill_id(repo_key, "qa", scene_id, user_id=_sub_uid)`，
`skill_path` 取 `get_skill_path(sid)`（解析不到则空串，保持名字降级），替代硬编码 `0, qa_skill`。

### D 门禁传 skill_fallback（`skill_materialize` + `_materialize_direct_workdir`）
- `materialize_skill_with_fallback` 的回退分支（官方包 / agent_type 检查器）结果补 `skill_name`
  （单点来源，避免调用点用 `basename(skill_dir)` 反推）；
- `_materialize_direct_workdir` 在 `status == "fallback"` 且 `skill_name` 存在时传
  `skill_fallback={"skill_name": ..., "skill_id": ...}`，与 worker 侧 `task["skill_fallback"]` 口径一致；
- `status == "degraded"`（真缺失）**不传** → 既有 fail-fast（`[skill_mismatch]`）保持不变。

### E 场景保存存名必存 id + 回填
- `src/domains/mgmt/pipeline_config.py` 新增 `fill_scene_nodes_skill_id(nodes, repo_key, user_id, resolver=None)`：
  纯内存变换，节点有 `skill` 无 `skill_id` 时按 user→repo→official 解析回填（可注入 resolver 供测试）；
  解析不到保持原样（派发时按名降级 + warning）；
- 挂载点：`create_custom_scene` / `update_custom_scene`（仓库级场景建/改/复制/组织场景统一入口）、
  `scenes.py` 市场场景 create/update 的 `nodes_json` 组装处；
- 存量回填：`scripts/backfill_scene_nodes_skill_id.py`，默认 dry-run 输出对照清单
  （表 / 行 / scene / tag / skill / 解析 id / 是否官方），`--apply` 才写库，幂等。

## 测试 seam
| 切片 | seam | 用例 |
|------|------|------|
| B-1 | `service.enqueue_task` 落库列 | sqlite 内存表断言 `skill_id` 列 = 解析 id |
| B-2 | `local_claimer._rebuild_evt_from_task` | 任务行 → `_skill_id`/`_skill_path`/`_skill_obs_key` |
| C | `_materialize_direct_workdir` | `_skill_obs_key` → `.skills/<name>/SKILL.md` 存在 |
| A | `_dispatch_pr_subscription_extra` 入队入参 | 捕获 `_enqueue_pipeline_task` 的 `skill_id` |
| D | `_materialize_direct_workdir` 写 `_skill_consistency` | 回退态 → `declared_fallback` + ok |
| E | `fill_scene_nodes_skill_id` / `update_custom_scene` | 存名后节点 `skill_id≥1` |

## 实施顺序
B（红→绿）→ A → D → C（随 B 验证）→ E（保存 + 脚本）→ 自审查 → PR → QA。