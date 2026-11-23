# Plan: Issue #3891 — /pipeline 与主资源共用解析链路的实现方案

> 基线 `arch-weekly-11-ljt`；改动仅限 `src/domains/runtime/routes/issues.py` 与测试。

## 1. 现状（基线代码事实）

`src/domains/runtime/routes/issues.py`

- `get_issue_detail`（L2769）：内部 miss 时依次 `_vcs_issue_lookup`（#672 通道，命中即返回
  `source="vcs"` 形状）→ `_sync_missing_issue_row`（VCS 核实 + 补建内部行）→ 三态 404
  （`_detail_404("issue_not_found"|"internal_sync_missing")`）。
- `get_issue_pipeline`（L2855）：**只查内部三表**，`segments` 为空且 `num not in _issue_stage_map()`
  直接 `HTTPException(404, {"code":404,"message":"Issue #N 不存在"})`——没有 owner/repo/number 回落，
  这正是外部托管仓既有 issue（内部 `issue_id=null`）稳定 404 的根因。
- 编号归一在两处各写一遍（`number[1:] if number[:1]=="#"` + `isdigit→int`），无单一入口。

## 2. 改动切片（一竖切片一测试）

### 切片 1（红）测试：外部托管仓 issue 的 /pipeline 必须 200 空 pipeline

新增 `tests/runtime/test_issue_3891_pipeline_shared_resolution.py`：

1. `test_pipeline_vcs_fallback_returns_empty_pipeline`：patch `_vcs_issue_lookup` 命中，
   `GET /api/issues/org/hosted/124/pipeline` → 200、`status=="unknown"`、`segments==[]`、
   `diagnostics == {exists:True, has_pipeline_data:False, reason:"issue_unregistered", source:"vcs"}`。
2. `test_eight_numbers_two_endpoints_agree`：42/119–125 全部编号，主资源与 `/pipeline`
   端点均为 200 且存在性口径一致（主资源 `source=="vcs"` ↔ `/pipeline` `diagnostics.exists`）。
3. `test_truly_missing_404_is_discriminable`：`_vcs_issue_lookup` → None 且
   `fetch_issue_via_vcs` → `("missing", {})`：两个端点均 404，
   `detail["reason"]=="issue_not_found"` 且 `detail["error_code"]=="issue_not_found"`。
4. `test_unverifiable_vcs_404_internal_sync_missing`：`fetch_issue_via_vcs` → `("unknown", {})`：
   两端口径一致 404 `internal_sync_missing`（不与 200 空 pipeline 混淆）。
5. `test_registered_issue_pipeline_shape_unchanged`：`set_issue_stage` 登记 + 事件 → 200 有 segments、
   **无 `diagnostics` 键**（结构不变）。
6. `test_registered_issue_without_events_unchanged`：登记但无事件/档案 → 200 空 pipeline，
   同样**无 `diagnostics` 键**。
7. `test_pipeline_does_not_backfill_internal_row`：VCS 核实 `("exists", {...})` 且
   `ensure_issue_task` 被 patch 为断言不被调用 → `/pipeline` 仍 200，内部仍无行（只读）。

### 切片 2（绿）实现

1. `_normalize_issue_number(number)`（放 `_num_from_issue_id` 之后）：
   ```python
   num = str(number or "").strip()
   if num[:1] == "#": num = num[1:]
   norm = normalize_issue_ident(num)
   if norm: num = norm
   return str(int(num)) if num.isdigit() else num
   ```
2. `_sync_missing_issue_row(repo_keys, number, backfill=True)`：`backfill=False` 时
   `state=="exists"` 直接返回，不调 `ensure_issue_task`。
3. `_resolve_issue_presence(...)-> dict{num,state,source,vcs,vcs_provider,synced}`：
   ledger 命中（stage/status/group 任一）→ `exists/ledger`；
   `_vcs_issue_lookup` 命中 → `exists/vcs` + 原文；
   否则 `_sync_missing_issue_row(..., backfill=)` → `missing`/`unknown`/`exists(vcs, synced)`。
4. `get_issue_detail` 改为助手驱动，分支顺序/返回形状逐字保持（VCS 原文命中 → `source="vcs"` 形状；
   `synced` → 重读四张映射）。
5. `get_issue_pipeline`：`_normalize_issue_number` + 助手（`backfill=False`）；
   `segments` 空且内部未登记时 `exists` → 200 + `diagnostics`（`issue.number` 归平台原生形态），
   否则 `_detail_404`。
6. `_detail_404` 增量补 `error_code`。
7. `tests/runtime/test_issues_api.py::test_pipeline_not_found` 补 patch（`_vcs_issue_lookup`→None、
   `fetch_issue_via_vcs`→`("missing",{})`），避免单测真实外呼。

## 3. 测试 seam（编码前声明）

- seam = HTTP 路由层（`TestClient` 打 `/api/issues/{owner}/{repo}/{number}[/pipeline]`），
  与先例 `tests/runtime/test_issue_672_dual_path_resolution.py`、`test_issues_api.py::TestIssuePipeline` 同层。
- 外部依赖（VCS）在 `_vcs_issue_lookup` / `fetch_issue_via_vcs` 处 patch——既有 seam，未新建。
- 断言口径 = 用户可见行为（状态码 + 存在性/数据标志 + 结构），不断言内部函数调用次数
   （只读性用「内部行不存在」这一可观察事实断言）。

## 4. 验证命令

```bash
python3 -m pytest tests/runtime/test_issue_3891_pipeline_shared_resolution.py -q
python3 -m pytest tests/runtime/test_issues_api.py::TestIssuePipeline -q
python3 -m pytest tests/runtime/test_issue_672_dual_path_resolution.py -q   # 主资源回归
python3 -m pytest tests/runtime/test_issue_667_detail_fallback.py -q
```

基线已知失败（与本改动无关，改动前即红）：`tests/runtime/test_issues_api.py::TestListIssues::
test_aggregates_stage_and_status`（列表聚合用例的数据/顺序依赖）。

## 5. 风险控制

- 主资源分支顺序保持原样：先 `_vcs_issue_lookup`（命中即返回），再补建核实路径。
- 已登记 issue 的 `/pipeline` 不走新分支（`num in stage_map` 时跳过解析助手），返回结构零变更。
- 失败排查：单测中所有 VCS 外呼点均已 patch，若出现真实网络等待即为漏 patch。