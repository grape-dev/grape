# 需求：PR 订阅事件在需求中心 / 流水线页 / 场景详情页排序错误

日期：2026-11-10（v2：补充流水线页与场景详情页同类问题）
建议落点：平台后端 `GET /issues/{owner}/{repo}`、`GET /pipeline/{owner}/{repo}`
工作分支：`weekly-mys`（scene `3f40dd97-ab9a-4483-ad18-36257e0d6df4`「运行域-任务编排专用开发流水线」）

## 1. 背景 / 现象

用户（XOcto）反馈 PR 订阅事件在三处显示顺序都不对：

1. 需求中心：PR 卡片「最新的一直排在后面，要翻很久」；
2. 流水线页：PR 记录整体沉底，且块内 PR 号「从小到大」；
3. 场景详情页「相关流水线」：同样沉底 + 从小到大。

三者是**两个同源排序缺陷**（见 §3）。

## 2. 现状代码（已逐行核对）

### 2.1 需求中心（`GET /issues/{owner}/{repo}`）

线上前端为静态版（`src/api/static/index.html` 引用 `js/pages/issues.js`）。

1. 前端一次拉全量、自己分页：
   - `src/api/static/js/pages/issues.js:258-266`：请求 `/issues/{owner}/{repo}?sort_order=…&mode=time`，**不带 page 参数**。
   - `src/api/static/js/pages/issues.js:23` `_TASKS_PER_PAGE = 15`；`:941-946` 客户端切片分页。
2. 后端先排真实 Issue，再单独追加 PR 卡片：
   - `src/api/routes/issues.py:1857` `list_repo_issues`；
   - `:1895-1932` 真实 Issue 已按 `created_at`（`mode=default` 时状态优先）排序；
   - `:1971` `pr_items = _query_pr_subscription_events(repo_keys)` 单独查询；
   - `:1975-1976` `issues.extend(pr_items)` —— **追加到已排序列表末尾，不参与任何排序**。
3. PR 卡片块内顺序：`_query_pr_subscription_events`（`src/api/routes/issues.py:98-189`）用 `ORDER BY seq DESC`，每个 PR 取最新事件作为卡片（`created_at` = 该 PR 最近一次审查时间），块内为「最近活动倒序」。

### 2.2 流水线页 & 场景详情页（`GET /pipeline/{owner}/{repo}`）

两处共用同一份后端数据：

- 流水线页：`src/api/static/js/pages/pipeline.js:370-418` 渲染 `history` 表格（PR 行为 `PR#N`）。
- 场景详情页「相关流水线」：`src/api/static/js/pages/dashboard.js:590-700` 从同一 `pipeData` 里按 `scene_id` 过滤后分页渲染。

后端排序（`src/api/routes/pipeline.py`）：

1. `:411-429` 全量事件按 `seq ASC` 读出，`issue_ids = list(dict.fromkeys(...))` 得到 **最早在前** 的 id 序列；
2. `:496-500` 逐 id 构造 pipeline，PR 伪 issue 的 `issue_id` 在此被改写为展示值 `PR#N`（`:343-347`），原始值保存在 `raw_issue_id`；
3. `:522` `_max_seq_cache` 以 **原始 id**（`#pr_<n>_<scene>` / `#123`）为键；
4. `:523` `pipelines.sort(key=lambda p: _max_seq_cache.get(p["issue_id"], 0), reverse=True)` —— 用 **展示 id** 去查。

## 3. 根因

### 缺陷 A：PR 伪卡片不参与需求中心排序（`src/api/routes/issues.py:1975-1976`）

PR 订阅伪卡片在真实 Issue 排序完成之后被 `extend` 到列表尾部：

- 无论时间排序选升序还是降序，PR 卡片恒在全部 Issue 之后；
- 前端按 15 条/页切片后，PR 卡片整块落到**最后一页**（列表越长越靠后）。

### 缺陷 B：PR 伪卡片排序键 miss，全部沉底并按编号升序（`src/api/routes/pipeline.py:523`）

`_max_seq_cache` 的键是原始 `issue_id`，而 `pipelines[i]["issue_id"]` 对 PR 伪 issue 已经是展示值 `PR#N` → `get()` 全部 miss → 排序键恒为 0：

- 所有 PR 行被压到列表末尾（真实 Issue 的 max seq 都 > 0）；
- 键相同（都是 0）时 Python `sort` 稳定，保持 `issue_ids` 的原始插入顺序 = 事件 `seq ASC` = **PR 号从小到大**；
- 场景详情页是对同一 `pipeData` 做过滤，因此同样「沉底 + 从小到大」。

两处缺陷叠加，就是用户看到的「最新的永远在后面、从上到下都是小号 PR」。

## 4. 期望行为

- 需求中心「仓库全部」视图中，PR 订阅卡片与真实 Issue 使用**同一套排序键**参与全局排序；
- 默认 `sort_order=desc` 时，最新（最近审查/最新事件）的 PR 出现在列表最前面；
- 切到升序时，顺序整体反转，PR 卡片同样跟随；
- 流水线页与场景详情页「相关流水线」中，PR 记录按**最近活动倒序**与真实 Issue 混排，最新的 PR 在最前；
- 三处都不改变卡片内容、统计口径、数据本身。

## 5. 方案要点（两处后端，加法式约 12-18 行）

### 5.1 需求中心（`src/api/routes/issues.py:1975-1976`）

把「追加」改为「合并 + 同键重排」：

- `mode=time`：`issues.extend(pr_items)` 后按 `created_at` 统一排序，`reverse=(sort_order == "desc")`；
- `mode=default`：先按 `created_at` 排序，再按状态优先级做一次**稳定排序**（两段排序），避免单次 `reverse=True` 把状态 rank 也翻过来；
- 服务端真分页路径（`per_page>0`）保持「PR 卡片只混第 1 页」的现状，不改 total / has_more 口径（前端未走该路径）。

排序键统一用卡片展示的 `created_at`，与行上显示的时间一致；PR 被复审会浮到前面，与流水线页「按最近活动倒序」口径相同。

### 5.2 流水线页 / 场景详情页（`src/api/routes/pipeline.py:523`）

排序键改用原始 id 兜底展示 id（1 行）：

```python
pipelines.sort(key=lambda p: _max_seq_cache.get(p.get("raw_issue_id") or p["issue_id"], 0), reverse=True)
```

`raw_issue_id` 由 `_get_issue_pipeline` 已在返回体里提供（`:351`），无需新增字段；真实 Issue 两者相同，行为不变。

## 6. 验收标准

1. `GET /api/issues/grape-dev/grape?sort_order=desc&mode=time` 返回中，`is_pr_event=true` 的卡片按 `created_at` 混排在真实 Issue 之间（最新的在最前），不再整块位于数组尾部；
2. `sort_order=asc` 时整体顺序反转，PR 卡片位置同步反转；
3. `mode=default` 时先按状态优先级分组、组内按时间排序，PR 卡片与同状态 Issue 混排；
4. 不带 `page`（线上路径）行为符合 1-3；带 `per_page` 时 PR 卡片仍只混入第 1 页，`total`/`has_more` 不变；
5. `GET /api/pipeline/grape-dev/grape` 返回中，`issue_id` 形如 `PR#N` 的记录按 `raw_issue_id` 的真实最大 seq 参与倒序，不再整块沉底；
6. 场景详情页「相关流水线」（按 `scene_id` 过滤后的同一 `pipeData`）顺序随 5 同步正确，最新 PR 在前；
7. 前端无需改动（服务端顺序驱动渲染）；
8. 改动只涉及 `src/api/routes/issues.py` 与 `src/api/routes/pipeline.py`，不触碰 `/issues/mine`（本来就不混 PR 卡片）。

## 7. 范围约束

- 只改排序/合并逻辑；不改 PR 卡片字段、状态映射、`qa_history` 截断（50 条）、`events_count` 统计；
- 不改数据表结构与历史数据；
- 不改前端静态 JS 与 Vue 构建产物（含 pipeline.js / dashboard.js 的渲染与分页）；
- 不顺手改 `_query_pr_subscription_events` 的 SQL 与块内顺序语义；
- 不改 `_get_issue_pipeline` 的返回字段（`raw_issue_id` 已有）与 `active`/`history` 切分口径、`history_limit` 截断逻辑；
- 同类但不同根因的问题（如 `/issues/mine` 的归属过滤 #827/#350）不在本单范围。

## 8. 参考

- 线上前端判定与 PR 卡片定位过程：技能库 `grape-project/references/issues-center-pr-card-ordering.md`。