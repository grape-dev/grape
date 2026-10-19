# Spec: Issue #239 — 场景名称显示一致性

> Owner: Coding Agent | Baseline: weekly-04-lyz | 2026-09-24

## 目标
统一项目场景页面与需求中心（创建/复制需求下拉框）、任务列表、Issue pipeline 详情中的场景名称显示来源，全部优先使用 `display_name`，缺失时回退 `name`，与 `dashboard.js` 保持一致。

## 根因
- 项目场景页面（`dashboard.js`）使用 `s.display_name || s.name` 显示（正确）。
- 需求中心创建/复制需求下拉框（`issues.js`）使用 `s.name` 显示（错误）。
- 任务列表（`issues_center.py`）与 Issue pipeline 详情（`issues.py`）通过 `get_pipeline_config` 取 `_cfg.get("name")`，而 `get_pipeline_config` 返回值不含 `display_name`（错误）。

## 修改清单

### 1. `src/api/pipeline_config.py` — `get_pipeline_config` 返回增加 `display_name`
- DB 分支（`_query_scene_by_id` 返回的 scene 已含 `display_name`）：返回值新增 `display_name = scene.get("display_name") or scene.get("name", scene_id)`。
- scene_market 官方模板分支（`_load_official_scene_from_market` 返回的 scene 已含 `display_name`）：同上。
- pipelines.json 文件回退分支：无 display_name 字段，`display_name = file_scene.get("name", scene_id)`（与 name 一致，保证不空）。
- 保留 `name` 字段不变（向后兼容）。

### 2. `src/api/issues_center.py` ~1298 行 — 任务列表 `scene_name` 改用 `display_name` 优先
- `_scene_name = _cfg.get("display_name") or _cfg.get("name", _scene_id)`

### 3. `src/api/routes/issues.py` ~716 行 — Issue pipeline 详情 `scene_name` 改用 `display_name` 优先
- `"scene_name": (pipe_cfg or {}).get("display_name") or (pipe_cfg or {}).get("name", _scene_id)`
- 同步 ~81 行 `_get_scene_name` 缓存：`_scene_name_cache[sid] = (cfg or {}).get("display_name") or (cfg or {}).get("name", sid)`

### 4. `src/api/static/js/pages/issues.js`
- ~1541 行 创建需求下拉框：`opt.textContent = (s.display_name || s.name || s.id) + (scopeTag ? ' ' + scopeTag : '');`
- ~1654 行 复制需求下拉框：同上

## 验收标准
- [x] 需求中心创建需求下拉框场景名与项目场景页面一致
- [x] 复制需求下拉框场景名与项目场景页面一致
- [x] 任务列表场景名与项目场景页面一致
- [x] Issue pipeline 详情场景名与项目场景页面一致
- [x] display_name 修改后各处同步更新
- [x] 无 display_name 时回退 name 不报错
- [x] 现有测试通过
- [x] 新增测试覆盖 display_name 优先逻辑

## 风险
- `get_pipeline_config` 新增字段仅追加，不修改已有字段，向后兼容（Low）。
- 官方场景模板无 display_name 时回退 name，与当前行为一致（Low）。
