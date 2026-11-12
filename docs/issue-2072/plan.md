# Plan: Issue #2072 — issue_number 类型归一（int）

## 根因
`_query_tasks()`（src/api/issues_center.py:3396）构造响应卡片时 `"issue_number": r[3]` 直出 DB 行。
DB `issue.issue_number` 为 INT 列，但 MySQL 兼容层/历史 GitCode 同步数据可能产出字符串
（dev 实测 mine 526 条全为 str），响应构造层无归一 → 字符串泄漏到调用方。
其他三处同类直出点：
- `src/domains/runtime/routes/issues.py` `GET /{issue_id}/status`（任务卡轮询）：`"issue_number": row[3]` 直出；
- `src/domains/runtime/routes/issues.py` `GET /{owner}/{repo}`（列表）：走 `_query_tasks`，随主修点归一；
- `src/domains/runtime/routes/issues.py` `GET /{owner}/{repo}/{number}`（详情）：`_assemble_issue`/VCS 兜底已 `int()`，无需改。

## 修改点
1. `src/api/issues_center.py` `_query_tasks()`：`"issue_number": int(r[3])`。
   - 归一失败（非数字字符串）由 `int()` 显式抛 ValueError → 500，不静默字符串化（Spec 决策）。
2. `src/domains/runtime/routes/issues.py` `get_issue_status()`：`"issue_number": int(row[3])`。
3. 无需改动：详情 `_assemble_issue`、PR 伪卡（`pr_num` 本已 int）、`_task_status_map`（内部 str key，不外泄）。

## 测试 seam（公开接口层）
新增 `tests/test_issue_2072_issue_number_int.py`：
- 测试库注入 issue 行：正常 int 行 + 文本型编号行（SQLite 动态类型下 `' 2072'` 保留 TEXT，
  模拟 dev 上 GitCode 同步混入的字符串数据；`int(' 2072')` 可无损归一）。
- `GET /api/issues/mine`（JWT）：断言所有 `issue_number` `isinstance(x, int)`；
- `GET /api/issues/{owner}/{repo}`（列表）：同断言（含 `number` 别名）；
- `GET /api/issues/{card_id}/status`（任务卡）：同断言；
- `GET /api/issues/{owner}/{repo}/{number}`（详情）：断言 `number` 为 int。

## 提交拆分（TDD）
1. `test(#2072)`: 先写失败类型测试（红：文本型编号行返回 str → 断言失败）。
2. `fix(#2072)`: `_query_tasks` / `get_issue_status` 归一，测试转绿。
