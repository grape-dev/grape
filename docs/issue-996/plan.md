# Issue #996 方案设计

## 事件层
1. `src/vcs/event_types.py`
   - `CHANGES_KEY_MAP` 增补 files/deleted_files/changed_files → files；
   - `normalize_files(source, limit)` 把 payload/commits/files/changes 归一为
     `{added,modified,deleted,truncated}`，条目为 `{path, sha?}`；
   - `FILE_EVENT_TYPES = {push, pull_request_merged, pull_request_updated}`；
   - added/modified 单桶限 200，deleted 永不截断。
2. `src/vcs/{gitcode,github,gitlab}.py`
   - `normalize()` 对变更类事件调用 `normalize_files(payload)`，命中非空则写
     `payload.files`；payload 自带 commits/files/changes 时零额外 API。
3. `src/vcs/files_enrich.py`
   - payload 无文件清单时兜底：PR 走 `/pulls/{n}/files` 翻页；push 走
     `/compare/{before}...{after}` 或 `/commits/{after}`；失败 fail-open。
4. `src/api/event_pipeline.py`
   - 归一化后对变更类事件调用 `enrich_event_files(evt, repo_key)`，入库 payload。

## 守卫预检与清理
1. `src/vcs/guard_impact.py`
   - `is_guard_script`：scripts/tests 下 `check-*.sh|.py`；
   - `scan_guard_impact(target_paths, scripts)`：按完整路径 / 父目录引用匹配；
   - `fetch_guard_scripts`：contents API 按目录预筛拉取脚本文本；
   - `list_issue_artifacts`：列 `docs/issue-N` 产物。
2. `src/runtime/cleanup.py::clean_issue_artifacts`
   - 删除前预检；`breaking=true` → 跳过删除、返回 `guard_impact`；
   - 无命中 → 照常 remover，返回值不含 guard_impact。

## 消费与展示
1. `src/prompt_builder.py`：变更类事件注入 `## Changed Files`，列出
   added/modified/deleted、truncated 与 guard_impact，提示 QA 只复核相关守卫。
2. `src/api/event_store.py::get_events(include_payload=True)` 与
   `src/api/routes/issues.py` 事件 payload 补丁：返回 `files` / `guard_impact`。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 清单来源 | payload 优先、API 兜底 | 减少额外调用与延迟 |
| 大 PR | added/modified 截断，deleted 全留 | 体积可控且删除审计完整 |
| 守卫命中 | 跳过删除 | 保守，避免守卫由绿转红 |
| 事件类型 | 复用现有 | 避免路由矩阵扩张 |