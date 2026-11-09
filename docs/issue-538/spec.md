# Spec: Issue #538 — 空仓库用户进入仓库大厅被误判为加载失败

> 来源：Issue Agent Spec（Issue 评论），coding 阶段引用基线。

## 目标
修复 `list_member_repos`（`GET /api/repos/member`）与 `sync_repos`（`POST /api/repos/sync`）
把「绑定正常、拉取成功、账号下无仓库」的合法空结果误判为加载失败抛 502 的缺陷；
空仓库用户应看到大厅空态引导（🔭 + 新建项目入口）而非「加载失败」。

## 范围
- `src/api/routes/repos.py`：两处 `if not all_repos` 空结果分支增加 `failed_providers` 失败信号判别。
- `tests/test_member_empty_repos.py`：新增回归测试（合法空结果 200 / 失败仍 502）。
- 前端 `app.js`：现有 `d.repos || []` → `renderRepoSelection` 已正确处理空数组，无需改动（防御已天然存在）。

## 验收标准
- 空仓库用户 GET /api/repos/member → 200 + `{"repos": [], "_cached": false}`，且空列表落库。
- 同场景 POST /api/repos/sync → 200 + `{"repos": [], "message": "同步成功"}`。
- failed_providers 非空（真失败）仍 502 / `_stale` 缓存兜底；401 reauth / 400 未绑定分支不变。
- 既有测试全绿。
