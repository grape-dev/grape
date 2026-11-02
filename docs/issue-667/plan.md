# Plan: Issue #667

## 改动定位（本仓 grape，base=weekly-08-mys）

1. `src/api/event_pipeline.py`
   - 新增 `fetch_issue_via_vcs(repo_key, issue_number) -> (state, data)`：state ∈
     exists/missing/unknown（GitCode HTTP 404 → missing；token 缺失/网络异常 → unknown）。
   - `_inject_issue_pr_env`：`GRAPE_ISSUE_ID` 解析为空 → `ensure_issue_task` 补建 → 重解析。
2. `src/api/routes/issues.py`
   - `get_issue_detail`：三表未命中 → VCS 核实 → 补建返回（`synced_by_fallback: true`）/
     404 `reason=issue_not_found` / 404 `reason=internal_sync_missing`。
   - `issue_awaiting_input`：卡片 id 无行时支持 `body.issue_number` 兜底（会话仓库作用域
     匹配 + VCS 核实 + `ensure_issue_task` 补建）；404 detail 带语义区分。
3. `src/api/issues_center.py`：`TaskAwaitingInputBody` 增量字段 `issue_number: int = 0`。
4. `help/api/11-issues.md`：detail/awaiting-input 404 reason 语义与降级指引。
5. `tests/test_issue_667_detail_fallback.py`：新增用例（monkeypatch VCS 层，不打真实 API）。

## 关键决策

- 兜底触发点：仅内部三表未命中时才查 VCS（避免每次请求多一跳 VCS 调用）。
- 缺行自动补建：是（补建前经 VCS 核实，或事件链路自身即存在性证据）。
- 404 语义：`reason` + `suggestion` 字段化，便于 Agent 程序化降级。
- `issue_number` 为纯增量可选字段，不破坏既有调用方。

## 测试

`python3 -m pytest tests/test_issue_667_detail_fallback.py tests/test_issues_api.py tests/test_issue_pending_review.py tests/test_task_center_ensure.py`
