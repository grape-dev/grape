# Spec: Issue #3771 — issues mine 分页 COUNT 落到 mgmt 库（total=0 / has_more 失真）

> 权威 Spec 见 Issue #3771 评论 `202841142`（[issue agent] 发布）。本文件为开发侧摘要。

## Objective
`GET /api/issues/mine` 真分页分支（`per_page>0`）的行数统计走 runtime 域连接：`total` 与真实
过滤条数一致，`has_more` 恢复正确；COUNT 失败不再静默落成 `0`。

## 现状（`arch-weekly-11-ljt` 核对）
- `src/domains/runtime/routes/issues.py:2323` 局部 import `db`，`:2327` 裸用
  `session_manager.db.cursor()` 执行 `SELECT COUNT(*) FROM issue WHERE ...`。
- 该路由由 mgmt 进程服务，`issue` 表归 runtime 域（`configs/table_ownership.json`）；三库拓扑下
  COUNT 打到 mgmt 库同名视图 → 恒 1146（生产实测），异常被 `log.warning("issues mine: count failed")`
  吞掉后 `_total` 保持 0。
- `:2336` `has_more = (page-1)*per_page + len(issues_out) < _total`，`_total=0` 时恒 false。
- 行数据本身正确：同文件 `_rt_db`（`_DomainDbProxy`，`:59-113`）与 `_query_tasks`
  （`src/domains/runtime/issues_center.py:4914`）都已走域代理，仅此一处 COUNT 漏点。

## Scope (In)
- `src/domains/runtime/routes/issues.py`
  1. 新增模块级 `_count_issues(where, params, label="issues")`：issue 表 COUNT 的唯一收口，内部
     `db_lock` + `_rt_db.cursor()`；失败返回 `None` 并沿用原 WARNING 文案（`issues mine: count failed`
     字面量不变，监控口径可继续用）。
  2. `my_issues`：去掉 `db` import，COUNT 改调 `_count_issues(..., label="issues mine")`；
     失败按 `issues_list` 既有降级口径 `total=None` + `has_more = len(issues_out) >= per_page`。
  3. `list_repo_issues`（`:2544`）：COUNT 同样收口到 `_count_issues`，**保持其现状语义**
     （失败 `0` + `len(pr_items)`），不改变既有接口行为。
- `tests/runtime/test_issue_3771_mine_count_domain.py`：mgmt 上下文域路由断言、COUNT 异常降级断言、
  `per_page=0` 无跨域读断言、COUNT 单点收口静态守门。

## Out of Scope
- `webhook.py` / `runtime.py` 等其它路由的 `db.cursor()`（各自域归属与调用拓扑不同，不在本 Issue 面）。
- `#3763` / `#3732` 的跨域读写回归（表与代码路径不同，不合并修）。
- 前端分页逻辑、`has_more` 的前端消费语义。

## Acceptance Criteria
- mgmt 上下文（`current_domain()=mgmt` + runtime 自有 DSN）下调用 `/mine` 分页：COUNT 走 runtime
  域连接，`total` 等于域连接返回的真实条数（非 1146 / 非 0）。
- COUNT 抛错：响应 `total is None`，`has_more` 按满页推断（满页 true / 未满 false），不返回误导性 0。
- `per_page=0`：不产生任何跨域连接（legacy 全量形态不变）。
- `issue` 表 COUNT 在 `issues.py` 中只剩一处（`_count_issues`），且无 `db.cursor()` 裸调用。
- `/mine` 的 `q` / `statuses` / `creators` 过滤口径与 `/api/issues/{owner}/{repo}` 分页语义不变。