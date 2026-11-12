# P3 收口决策清单（2026-11-11）

基线：js 测试 **78 通过 / 91**（起点 13 通过 / 78 红）。以下 13 项按「补代码 / 改验收 / 退役 / 运行期验证」四类。
每项都带证据与工作量，勾选后我执行。

---

## A. 补代码（3 项，共 ~25 行，纯前端，不重启）

- [ ] **A1 · #290 查看 Issue 链接分叉**（1 行）
  - 现象：`IssuesView.vue:2185` `openIssueLink` 里 `tk.is_pr ? '/pull/' : '/issues/'`，违反 #290 验收「统一 /issues/」
  - 依据：PR 伪卡已有独立入口 `openPrLink()`（:2187），此按钮不该分叉
  - 改法：`vcsUrl(tk.repo_key, '/issues/' + tk.issue_number)`
  - 影响：需求/PR 伪卡点「查看 Issue」一律开 Issue 页

- [ ] **A2 · #406 选 VOD 技能后禁用 SRE**（~15 行）
  - 现象：DashboardView 全文件 `:disabled` 只出现在分页按钮，**无 VOD↔SRE 联动**
  - 依据：旧 dashboard.js 有此联动（防 VOD 场景误配 SRE 引擎）
  - 改法：`pickSkill` 命中「开发者声音分析（CLI版）」时把 sre 节点置 disabled + 提示
  - 影响：场景编辑页 SRE 节点选择行为变化

- [ ] **A3 · P10d 密码长度谓词散落**（~8 行）
  - 现象：`AdminView.vue:1107/1125/1165` 三处 `password.length < 8` 硬编码
  - 依据：`test_password_blocklist` P10d 要求走统一谓词 `isPwdTooShort`（auth.js 有该谓词）
  - 改法：AdminView 引入/定义 `isPwdTooShort(p)`，3 处替换
  - 影响：无行为变化，仅口径统一

## B. 改验收 / 补功能（2 项，二选一）

- [ ] **B1 · #1102 事件历史「加载更早」**（补 ~40 行 / 或改验收）
  - 现象：`IssuesView.vue:1604-1608` 一次性渲染全部 events，无「默认 20 + 加载更早」与 stopPropagation
  - 选项：① 补回分页（默认 20、按钮阻断冒泡）② 改验收为「一次渲染全部」，测试退役
  - 建议：②先改验收，事件量大再回头补（当前单需求事件量通常 <20）

- [ ] **B2 · #123 异常任务「每页条数」选择器**（补 ~12 行 / 或改验收）
  - 现象：固定 10 条/页 + 页码翻页，无选择器
  - 选项：① 加 select 接 pageSize ② 改验收为固定 10 条
  - 建议：①（小且用户可感知）

## C. 补代码（小回归，2 行）

- [ ] **C1 · #1477 API Key 时间显示**（2 行）
  - 现象：后端 `list_api_keys` 返回 `normalize_event_time()` = **UTC ISO 带 +00:00**，
    AccountView 直接 `{{ k.created_at }}` → 用户看到 `2026-11-08T07:33:07+00:00`
  - 依据：`src/domains/mgmt/routes/settings.py:596`；`fmtTimestamp8/fmtLocalTime` 已存在但未用
  - 改法：渲染处套 `G.fmtLocalTime(k.created_at)`（本地时区、人类可读）

## D. 运行期验证（2 项，我起 headless 浏览器 + 真实数据验；~10 分钟）

- [ ] **D1 · chat_task_bubble（飞书重复气泡）**
  - 现状：ChatView 与后端均无去重逻辑（`dedupe/duplicate` grep 0）
  - 需验：发一条飞书消息，Web Chat 是否出现两个气泡；不去重则判定为缺口并补
- [ ] **D2 · scene_apply_detail_refresh（应用场景后详情刷新）**
  - 现状：SceneMarket 有 `loadDetailData` + popstate 重进；应用后是否刷新待验
  - 需验：市场应用一个场景 → 返回详情页是否还显示「场景不存在」

## E. 拟退役（3 项，文件已登记 `tests/js/RETIRED.md`，等你点头才删）

- [ ] **E1 · token_create_deeplink** —— `account.js` 已删，`deeplink` 在 app.js/AccountView 出现 0 次
- [ ] **E2 · engine_guide_simplified** —— `pulse/rt-guide-pulse` 在 CSS/视图 0 次；横幅本体在 app.js 但 12 项断言形态无实现
- [ ] **E3 · #335 引擎技能缓存** —— SkillsView/RuntimesView 均无 `_engineSkillCache`；若确认「按需拉取」即可，则退役

## F. 需人工逐条判定（1 项）

- [ ] **F1 · #372 r13 UI 优化**（14 项断言 7 项失效，7 项通过）
  - 需要你或我逐条过一遍：哪些优化点仍适用 → 改写；已废掉 → 退役
  - 工作量：约 1 轮

---

## 已收口、仅记账（无需动作）

- **#648** 附件首屏内联（性能项）：现一律 GET 端点，功能正常，已记偏差
- **#123** 若选 B2② 则并入「已收口」

## 不做（明确排除）

- 后端不动；不动 i18n 文案；不重启进程（JWT 全员掉线）