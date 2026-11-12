# Spec: Issue #2072 — GET /api/issues/mine 的 issue_number 返回字符串

> 来源：Issue Agent Spec 评论（id 200108157），评级 ready。

## 目标
`GET /api/issues/mine` 返回的 `issue_number` 与 DB 列（INT）、Pydantic 模型（`issue_number: int`）及其他端点一致，输出 JSON 整数（number），非字符串。

## 范围（In）
- `src/api/issues_center.py` `_query_tasks()`：响应构造处 `issue_number` 显式 `int()` 归一（覆盖 `/api/issues/mine` 与 `/api/issues/{owner}/{repo}` 列表等全部 `_query_tasks` 调用方）；归一失败（非数字）显式抛错，不静默字符串化。
- `src/api/routes/issues.py` 巡检：
  - 列表 `GET /{owner}/{repo}`：走 `_query_tasks`，归一后 `it["number"]` 别名同步为 int；
  - 详情 `GET /{owner}/{repo}/{number}`：`_assemble_issue` 已 `int(number)`、VCS 兜底已 `int(number)`，确认无需改；
  - 任务卡 `GET /{issue_id}/status`：`issue_number` 直出 DB 行 → 同批 `int()` 归一。
- 新增类型单测：mine / 列表 / 任务卡 / 详情四处断言 `isinstance(issue_number, int)` 且 JSON 序列为 number。

## 不做（Out of Scope）
- 不改 DB 表结构、不做数据回填（DB 列已是 INT）。
- 不处理 #1104/#1105 等关联契约问题（仅巡检记录）。

## 验收
1. mine 返回的 issue_number 全为 int；
2. 列表/详情/任务卡三处类型一致（int）；
3. 新增类型单测通过，现有测试全绿。

## 技术决策
归一位置 = 响应构造层显式 `int()`（非序列化层兜底）；防护 = 类型单测锁契约。
