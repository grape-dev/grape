# Spec: Issue #486 — 自动化任务引擎选择按「仓库作用域 + 用户归属」过滤

> Spec 已由 Issue Agent 发布于 Issue #486 评论（2026-10-28），本文件为开发侧存档副本。目标分支：`weekly-08-mys`。

## Objective
创建/编辑自动化任务（autopilot）时的引擎选择列表，仅展示当前用户在当前仓库下有权使用的引擎；服务端同步校验 `engine_runtime_ids` 归属，杜绝 API 层越权选择。

## 根因
- 前端 `autopilot.js` 的 `_apLoadEngineList` / `_apOpenEnginePicker` 调用 `GET /settings/agent-runtimes` 时未传 `repo` 参数（后端自 #277 起已支持该过滤口径）。
- 后端 `autopilot.py` 的 `POST /tasks` / `PUT /tasks/{task_id}` 对 `engine_runtime_ids` 无服务端归属校验。

## 验收标准
1. 仓库 A 创建任务时引擎列表不含仓库 B / 其他用户个人的引擎。
2. 编辑同样过滤；原绑定引擎超范围有「已不在当前可用范围」提示且不可再选中。
3. API 层提交越权 `engine_runtime_ids` 被服务端拒绝（400，明确 message）。
4. 新增回归测试 `tests/test_issue_486_autopilot_engine_filter.py` 通过；现有测试通过（重点回归 #277/#473）。

详细内容以 Issue #486 评论中的 Spec Document 为准。
