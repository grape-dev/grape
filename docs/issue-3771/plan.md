# Plan: Issue #3771

## 方案概述
把 issue 表的 COUNT 收口到模块级 `_count_issues()`（内部 `db_lock` + `_rt_db.cursor()`），
`my_issues` 改调该辅助并按 `issues_list` 的 `total=None` 降级口径返回分页元数据；
`list_repo_issues` 同点收口但保留其现有失败语义，避免连带行为变更。

## 为什么不再新增原语
`_DomainDbProxy` / `db_registry`（`current_domain` / `uses_own_dsn` / `new_connection`）已存在，
本次属「迁移漏点复用既有原语」：单点 1 行路由修正 + 失败语义。

## 实现步骤
1. `src/domains/runtime/routes/issues.py`：在 `@router.get("/mine")` 前新增
   ```python
   def _count_issues(where, params, label="issues") -> int | None:
       from src.kernel.session_manager import db_lock
       try:
           with db_lock:
               c = _rt_db.cursor()
               c.execute(f"SELECT COUNT(*) FROM issue WHERE {where}", params)
               return c.fetchone()[0]
       except Exception as e:
           log.warning(f"{label}: count failed: {e}")
           return None
   ```
   - `label` 保留 `issues mine: count failed` 原文案，线上告警口径不失效。
2. `my_issues`：`from src.kernel.session_manager import db, db_lock` → 删除（不再裸用进程库）；
   `_total = _count_issues(where, params, label="issues mine")`；
   `_total is None` → `{"total": None, "has_more": len(issues_out) >= per_page}`，
   否则维持精确 `has_more`。
3. `list_repo_issues`：`from src.kernel.session_manager import db, db_lock` 删除；
   `_cnt = _count_issues(where, params)`；`_total = (0 if _cnt is None else _cnt) + len(pr_items)`，
   与现状（失败 0 + PR 卡叠加）逐字等价。
4. 测试 `tests/runtime/test_issue_3771_mine_count_domain.py`
   - `_FakeConn/_FakeCursor` + `monkeypatch` `db_registry.current_domain/uses_own_dsn/new_connection`
     模拟 mgmt 进程 + runtime 自有 DSN；`_query_tasks` / `_get_gitcode_user_by_user_id` 打桩。
   - A：`per_page=20` → COUNT 落在 runtime 域游标，`total=1693`（≠1146/0），`has_more` 精确。
   - B：COUNT 抛错 → `total is None`，满页 `has_more=True`、未满页 `False`。
   - C：`per_page=0` → 零跨域连接、legacy 形态（无 `total` 键）。
   - D：AST 静态守门 —— `issues.py` 内 `db.cursor()` 调用数为 0；`SELECT COUNT(*) FROM issue WHERE`
     字面量只出现一次且位于 `_count_issues` 内。

## 测试 seam
公开 seam = `GET /api/issues/mine` 的处理函数 `my_issues(...)`（直接调用，注入 user dict），
配合 `db_registry`（既有三库拓朴 seam，先例 `tests/runtime/test_3350_runtime_domain_db_routing.py`）；
不测 `_DomainDbProxy` 内部实现。

## 风险与回归
- `list_repo_issues` 失败语义保持不变（失败仍 0 + PR 卡），仅换连接来源。
- `db_lock` 语义未动（两侧同锁对象）。
- `/mine` 非分页、`q`/`statuses` 过滤构造未动：由既有 `tests/runtime/test_mine_filter_behavior.py`、
  `tests/runtime/test_issues_mine_paging.py`、`tests/test_issue_2082_paging.py` 回归覆盖。