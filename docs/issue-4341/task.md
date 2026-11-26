# Tasks: Issue #4341

> 每切片先红后绿（`test(4341)` → `feat(4341)`），切片内改动与其测试同属一个原子变更。

## T1 单号解析：`_num_from_issue_id()` 取 `#` 后整串

- [x] 红：`test_num_from_issue_id_full_ident` / `_numeric_compat` / `_pseudo_pr_id_empty`
- [x] 绿：去掉 `re.search(r"(\d+)")`，转发 `normalize_issue_ident()`；`#pr_*` → `""`

## T2 归组键：`<provider>:<完整编号>`

- [x] 红：`test_group_keys_unique_mixed_idents`（同仓 `#IKXR3A/#IKXR3B/#3/#pr_1_x`）
- [x] 红：`test_group_events_not_merged_across_providers`（`gitcode:3` vs `gitee:3`）
- [x] 绿：`_issue_group_key` / `_issue_group_keys` / `_issue_group_events`；
      `_group_events_by_issue` 逐行 `repo_key` 归组（SQL 补 `repo_key` 列）

## T3 stage map 兜底对齐完整 ident

- [x] 红：`test_stage_map_issue_state_fallback_full_ident`
- [x] 绿：`_issue_stage_map` 兜底分支键与 `issue` 单表同形（显式注释）

## T4 三端点 / 列表 / 关联富化同键

- [x] 红（HTTP seam）：`IKXR3B/events` 空、`IKXR3A/pipeline` 非空、`IKXR3B/pipeline` 空、
      `/3/pipeline` 不含 IKXR3A 事件
- [x] 绿：`get_issue_detail`、`get_issue_events`、`get_issue_pipeline`、`_assemble_issue`、
      `_issue_title_map`、`_resolve_issue_presence`、列表页富化、`tasks.py`、`event_archive.py`

## T5 既有用例随迁 + 门禁

- [x] `tests/insight/test_issue_859_event_output_logs.py`、`tests/runtime/test_issue_events_api.py`
      按新键取值（用 `_issue_group_key()` 构造，不硬编码）
- [x] `tests/runtime/test_3055_issue_ident_hardening.py::_ALLOWED_LINES` 失效条目（原 `issues.py:174`）删除 ——
      该文件已无 int()/isdigit 命中，行号漂移后条目恒不命中（QA Nit）
- [x] `python -m pytest tests/runtime/test_issue_4341_ident_grouping.py + 受影测试` 全绿（除基线已知红）
- [x] lint 门禁（import/重复定义/F811/async 均 OK）+ `scripts/check-arch.py`：R1/R5/R7 OK，R2/R8 为基线既有项（本单未触碰）

## T6 复审补齐（QA Required / Optional / Nit）

- [x] Required① 门禁接入：新套件 + 可复算受影套件纳入 `.github/workflows/lint.yml`
      （step「Issue ident grouping (provider + full ident) regression (issue #4341)」），
      基线既有红套件（859 / events_api / 1938 / 2050 / perf / 3055）与原因在 step 注释中显式记录
- [x] Required② 随迁路径各补 1 例：`tasks/current`（gitee ident 断言 `events_count`/`latest_event`）、
      `build_issue_context`（字母数字 ident 事件摘要非空且不跨 ident），均在 CI 覆盖文件内
- [x] Optional 前缀路径：`_repo_keys_of()` 补去前缀裸键 + 「裸键存量行 + 前缀路径可查」用例
- [x] Nit：`_ALLOWED_LINES` 失效条目删除（见 T5）

## 协同回归

- [x] #4328 / PR #2149 的 ident `int()` 清理口径：本单不重复实现，合并时一并回归
      `tests/runtime/test_3055_issue_ident_hardening.py`
## 验证记录（2026-11-25）

- 新增 `tests/runtime/test_issue_4341_ident_grouping.py`：18 passed（首轮 14 例 + 复审 4 例：
  tasks/current、build_issue_context、前缀路径候选键、裸键存量行可见）。
- CI 实际执行的套件组合（新套件 + 252 / 3891 / 539 / 667 / event_structured / 3865）本地全绿，
  与 `.github/workflows/lint.yml` 新增 step 逐文件一致。
- 受影响既有用例（859 / 252 / 1938 / 2050 / perf / events_api / 3891 / 539 / 667 / 672 /
  issues_api / uc48 / 3055 / event_structured / 3865）：本次改动**零新增失败**，
  且 10 条基线红用例随桶键迁移转绿。
- 基线既有红（与 #4341 无关，未在本次修复）：`/events` 等接口 `output` 未内联
  （`_attach_event_payloads` 无调用点，#859 回归）、`service.py:4355/4362` 的
  int()/isdigit 守卫、`test_issues_api` 分页/聚合、`test_issue_648` OBS 签名、
  `test_issue_672` 模块级错误、`test_uc48` 平台字面量锚点。
