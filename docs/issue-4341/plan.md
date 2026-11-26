# Plan: Issue #4341 — 归组键「平台前缀 + 完整编号」的实现方案

> 基线 `arch-weekly-11-ch`；改动仅限 `src/domains/runtime/routes/issues.py`、
> `src/domains/runtime/routes/tasks.py`、`src/api/event_archive.py` 与测试。

## 1. 现状（基线代码事实）

`src/domains/runtime/routes/issues.py`

- `_num_from_issue_id()`（L140）：`re.search(r"(\d+)")` 抠首个数字串，无数字才回退
  `normalize_issue_ident()`（#3055 内核判据）。`#IKXR3B` → `"3"`。
- `_group_events_by_issue()`（L382）：SQL 跨 `repo_keys` 一次取全仓事件，
  以 `_num_from_issue_id(iid)` 为键 `out.setdefault(n, []).append(item)` → 三个 IKXR3*
  同键 `"3"`，且与纯数字 `#3` 同桶。
- `_issue_stage_map()`（L300）：`issue` 单表键 `str(issue_number)`（完整 ident），
  `issue_state` 兜底键 `_num_from_issue_id()`（`"3"`）→ 同号两源不同键。
- `get_issue_detail()`（L2854）：`num = _normalize_issue_number()`（完整 ident）后用
  `group_map.get(num)` 查 → 恒 miss（键是 `"3"`）。
- `get_issue_events()`（L2927）：用 `_num_from_issue_id(number)` 取键 → 命中 `"3"`，
  于是任一 IKXR3* 都返回同一批其它 issue 的事件。
- `get_issue_pipeline()`（L2943）：同 detail，`segments` 恒空。
- `_resolve_issue_presence()`（L3144）：`num in _group` 判存在性，键不一致时漏判。
- 列表页（L2736）`evs = group_map.get(str(_num))` 同键假设。

`src/domains/runtime/routes/tasks.py:573`：`_num = re.sub(r"[^0-9]", "", str(row[5]))`
→ `#IKXR3B` 变 `"3"`；`group.get(_num)` 同键假设。
`src/api/event_archive.py:215`：`gmap.get(str(issue_num).lstrip("#"))` 同键假设。

## 2. 目标不变量（设计）

1. **编号解析唯一口径**：`_num_from_issue_id(issue_id)` = `#` 后整串的合法 ident
   （`normalize_issue_ident`），`#pr_*` → `""`。不做任何数字提取。
2. **归组键唯一口径**：`_issue_group_key(repo_key, ident)` = `<provider>:<ident>`，
   `provider` 取 `parse_repo_key()`（裸键按约定默认 `gitcode`）。
   - 同仓字母数字单号各自独键；
   - 同号不同平台（`gitcode:3` vs `gitee:3`）不撞桶；
   - 同一逻辑仓库的 provider 别名由**候选键列表**覆盖，不靠「跨 repo_keys 合并」。
3. **取值唯一口径**：`_issue_group_events(group_map, repo_keys, ident)` 按候选键集
   （先调用方给的 repo_key，再候选集）取出**同一 ident** 的所有桶，按 seq 去重后升序合并；
   不同 ident（`IKXR3A` vs `3`）永不混合——分桶在归组层，合并只发生在同一 ident 的
   provider 别名之间（裸键 legacy 行 / 镜像仓双写不得丢事件）。
   detail/events/pipeline/assemble/presence/list/tasks/archive 全部改用它。
4. **stage/status 键**：保持裸 ident（`issue` 表 `issue_number` 文本原样），
   仅把 `issue_state` 兜底从「首个数字串」改成完整 ident（随 ①自然成立）。

## 3. 改动切片（一竖切片一测试一提交）

### 切片 1（红 → 绿）：单号解析与归组键唯一

- 红：`tests/runtime/test_issue_4341_ident_grouping.py::test_num_from_issue_id_*`、
  `test_group_keys_unique_mixed_idents`（同仓 `#IKXR3A/#IKXR3B/#3/#pr_1_x` → 键集合
  `{gitcode:IKXR3A, gitcode:IKXR3B, gitcode:3}`）、
  `test_group_events_not_merged_across_providers`（`gitcode:3` 与 `gitee:3` 各自独立）、
  `test_issue_group_events_lookup`。
- 绿：`_num_from_issue_id` 重写；新增 `_issue_group_key` / `_issue_group_keys` /
  `_issue_group_events`；`_group_events_by_issue` 改按行 `repo_key` 归组（SQL 补取
  `repo_key` 列，索引固定放末位）。

### 切片 2（红 → 绿）：`_issue_stage_map` 兜底对齐完整 ident

- 红：`test_stage_map_issue_state_fallback_uses_full_ident`（只有 `issue_state` 行 `#IKXR3B`
  时，`_issue_stage_map()` 键为 `IKXR3B`，与 `issue` 单表键同形）。
- 绿：兜底分支随切片 1 的 `_num_from_issue_id` 语义自然对齐（保留显式注释与用例看护）。

### 切片 3（红 → 绿）：三端点 + 列表 + 关联富化改同一键

- 红（HTTP seam，`TestClient`，与 `test_issue_3891_pipeline_shared_resolution.py` 同层）：
  - `GET /api/issues/{o}/{r}/IKXR3B/events` → `total=0`（此前 8 条串号）；
  - `GET /api/issues/{o}/{r}/IKXR3A/pipeline` → `segments` 非空；
  - `GET /api/issues/{o}/{r}/IKXR3B/pipeline` → `segments=[]`、`status=unknown`；
  - `GET /api/issues/{o}/{r}/3/pipeline` → 只含 gitcode `#3` 自身事件（不含 IKXR3A）。
- 绿：`get_issue_detail` / `get_issue_events` / `get_issue_pipeline` / `_assemble_issue`
  （签名首参改 `repo_keys` 列表）/ `_issue_title_map` / `_resolve_issue_presence` /
  列表页富化 / `tasks.py` / `event_archive.py` 全部改 `_issue_group_events`；
  `tasks.py` 的 `_num` 改 `normalize_issue_ident`。

### 切片 4：既有用例随迁

- `tests/insight/test_issue_859_event_output_logs.py`、`tests/runtime/test_issue_events_api.py`
  等按新键形状取值（用 `_issue_group_key()` 构造断言键，不硬编码字符串）；
  `tests/runtime/test_3055_issue_ident_hardening.py::_ALLOWED_LINES` 的
  `routes/issues.py` 行号随行数变化同步（语义不变：仍是 PR 号归一那行）。

## 4. 验证

```bash
python -m pytest tests/runtime/test_issue_4341_ident_grouping.py \
  tests/runtime/test_issue_events_api.py tests/insight/test_issue_859_event_output_logs.py \
  tests/runtime/test_issue_3891_pipeline_shared_resolution.py \
  tests/runtime/test_3055_issue_ident_hardening.py tests/runtime/test_issues_api.py \
  tests/runtime/test_uc48_gitee_pipeline_e2e.py -q
```

基线已知红（非本单引入，PR 说明中标注）：`service.py` int()/isdigit 守卫 2 行、
`_attach_event_payloads` 未被调用导致的 `output` KeyError 4 例、`test_issue_events_api` 同因 1 例。