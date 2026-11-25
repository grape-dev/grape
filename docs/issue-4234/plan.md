# Plan: Issue #4234

## 方案（Spec 方案 A：统一映射表）
1. 在 `mapStatusL` 旁定义 `STATUS_VIEW_MAP`：每个视图状态集中声明
   `api`（列游标/statuses= 下发的原始 status 集）、`labelKey`（列头/卡片/详情共用词条）、
   `fallback`、`strip`（是否过 stripTag，保持旧 STATUS_LABELS 行为）。
2. 派生消费点（值不变，杜绝重复字面量）：
   - `TASK_STATUS_TO_API` ← `Object.fromEntries(STATUS_VIEW_MAP)`（原 3052 行字面量删除）；
   - `BOARD_CURSOR_STATUSES.active_flow` ← pending ∪ in_progress 的 api 并集（仍为
     `active,processing`），其余列直接取对应 api；
   - `STATUS_LABELS`（列头/筛选选项）← `viewStatusLabel(k)`；
   - `statusInfo().text`（卡片状态点）← `viewStatusLabel(mapStatusL(tk))`；
   - `detailCardHtml` 状态点：`isDone/isProc/isCancelled` 改由 `mapStatusL(tk)` 派生，
     不再裸比原始 `tk.status`（修复 active+流水线 stage 时详情误显「待处理」）。
3. 词条：复用现有 `status.tag_*` 键，zh/en 均已齐备，无需新增。

## 回归保障
- 新增 `tests/js/test_issue_4234_status_wording_map.js`：
  T1 单一事实来源断言（派生接线、无重复字面量）；T2 statuses= 口径与游标取值不变、
  看板六列 key 不变；T3 列名与卡片名一致性（active+stage → 处理中；active 无 stage →
  待处理；processing → 处理中）；T4 详情改走 mapStatusL；T5 zh/en 词条齐备。
- 现有看板相关测试回归：#1077 列分屏、#2649 回合化、#1740 视图切换、#2346 接管徽标
  均通过；基线（weekly-mys）上既有的 10 个失败用例与本改动无关（前后一致）。
