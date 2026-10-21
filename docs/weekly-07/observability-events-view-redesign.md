# 统一事件视图重设计（**已实施** 2026-10-18：S1 后端契约 + S2 前端时间轴流 + S3 收尾）

> 背景：P1-4 把 6 类来源归一到一张表（§6.9），落地后发现"形状不对"——用户反馈
> 「一栏是 `observability.tab.events`，下面显示的 `[object Object]`，这一页看着有点奇怪」。
> 三个显示 bug 已在审计里修掉（见 `observability-audit-2026-10-17.md` §三/§四.3），
> 但**信息结构**本身需要重做：现在的"两行 chips + 8 列表格"承载不了五类异构事件。
> 本文只出方案，不动代码。

---

## 一、现状为什么别扭（逐条对应真实代码）

| 现状 | 问题 |
|---|---|
| 事件 = 「任务与日志」里的第 2 个子 Tab，8 列表格（时间/时长/来源/级别/事件/目标/状态/操作） | 横向 8 列在窄屏要横滚；"事件"视图是**跨来源时间轴**，本质不是一个"列表页" |
| 「时长」列 | 对**区间事件**（告警 firing→resolved、设备静默）有意义；对**瞬时事件**（任务完成、PR 合并、审计）恒为 `0s`/`—`，是噪声 |
| 「级别」列 | 混了两套词表：告警 severity（critical/severe/important/warning/info）vs 任务结果（failed/completed）。任务失败显示成"严重"，跟告警级别不是一回事 |
| 「状态」列 | 与「级别」列信息重叠（firing/resolved vs level），且只覆盖 5 个状态值 |
| 「目标」列 | 把仓库短名 + issue 号 + 设备号拼成一个字符串，三者混在一起 |
| 过滤区 | **两行** chips（来源 5 个 + 状态 2 个）；状态行标签就是那个 `[object Object]` |
| 分页 | 固定 `limit=200`，无翻页 → 更早的事件看不到，`ev_count` 却报总数 |
| 刷新 | 只在切 Tab 时拉一次，10s 轮询不覆盖 → **事件流不会自己更新** |
| 下钻 | 全塞在最后一列（时间线 · 日志 · 需求 · 设备），一行最多 4 个链接，挤 |

---

## 二、设计目标与不变量

**目标**：一眼看清"最近发生了什么、哪些还在进行、点一下能下钻到证据"。

**不变量**（沿用现有约定，不改）：

1. 三层受众：L1 用户面只看得到自己可见范围；`global` 维度只在 L2（管理台）。
2. 可见性由**后端强制**，越权 404；可见集合为空**绝不降级为全量**。
3. **不新建表**：按来源联合查询（monitor/gap/mutation/task/change/audit）——保持现状。
4. 前端不出现数据库内部字样（`metric` 名、`dim_key` 原值不进用户面文本）。
5. ECharts/依赖一律本地 vendored；不引入前端框架。
6. 侧栏不加项（用户偏好：侧栏不要太长）→ 事件视图**仍在「任务与日志」内**。

---

## 三、信息架构

### 3.1 主体：一条时间轴流（替代 8 列表格）

每行**两段式**（不横滚）：

```
[时间]  ●  [来源徽标]  标题（发生了什么）              [结果/状态徽标]   [目标：repo #issue / 设备 #id]
        └ 次要行（灰色小字）：持续 12 分钟 · 已恢复 · 3 > 阈值 0        [时间线] [日志] [需求] [设备]
```

- **主行**：时间（本地时区）、来源色点（monitor/gap/mutation 红黄、task 蓝、change 绿、audit 灰）、
  标题、状态/结果徽标、目标（可点）。
- **次行**：区间事件的持续时长与结束时间；`value > threshold`（仅数值型告警/mutation 有）；
  下钻动作（按可用性渲染：有 task → 时间线/日志；有 issue → 需求；设备维度 → 设备）。
- **行内展开**（点行）：统一字段明细（`state`、`last_seen_at`、`silenced`/`escalated` 标记、
  `rule_name`/`kind` 的可读名、`metric` 仅 L2 显示）。
- 来源徽标与状态徽标都**不显示原始 code**：用 `source.*` / `state.*` 词表。

### 3.2 过滤区：合并成一行

```
[来源: 全部 ▾] [状态: 全部 ▾] [时间: 24h ▾] [关键字 ___]                    [自动刷新 ☑ 30s]
```

- 来源下拉：全部 / 指标告警 / 数据缺失 / 突变 / 任务 / 变更（+L2：审计）。
- 状态下拉覆盖**真实状态全集**：进行中（firing/stuck）/ 已恢复（resolved）/ 已失败（failed）/
  已完成（done）；不再只有"进行中/已恢复"。
- 关键字可选实现（先在标题/目标/repo 短名里做前端过滤，后端不参与，避免 LIKE 与性能问题）。

### 3.3 分页与刷新

- **游标分页**：底部「加载更多」用 `before=<上一页最后一条的 started_at，含 key 去重>`；
  页面顶部保留"共 N 条（当前已加载 M 条）"的诚实计数。
- **自动刷新**：默认开，30s（与告警求值同频）；刷新只拉"游标之后的新事件"并**前插**，
  保持滚动位置；顶部显示"最近更新 hh:mm:ss"。关闭时按钮变"刷新"。
- 某来源被上限截断时明确提示：「仅显示最近 200 条（该来源已截断）」。

---

## 四、字段与语义口径（后端配合项）

| 统一字段 | 说明 | 变更 |
|---|---|---|
| `source` | monitor/gap/mutation/task/change/audit | 不变 |
| `kind` | 具体类型（`task_stuck`/`runtime_silent`/`failed`/`pr_merged`…） | 不变 |
| `ts_kind` | **新增**：`interval`（有始有终）\| `point`（瞬时） | 由来源决定，前端据此决定是否显示时长 |
| `level` | 告警级别（仅 monitor/gap/mutation 有意义） | 不变 |
| `outcome` | **新增（可空）**：任务/变更类的结果码（failed/completed/cancelled/merged/done） | 前端渲染"结果徽标"，与告警级别**分列** |
| `state_label_key` | **新增**：后端给出该状态对应的 i18n 键（`observability.state.<x>`），前端只做翻译 | 避免前端猜词表 |
| `started_at` / `ended_at` / `duration_sec` / `ongoing` | 区间语义 | 不变；`point` 类 `duration_sec=0`，前端不显示 |
| `target` | **新增**：结构化 `{repo_key, issue_id, device_id}`，替代现在拼好的字符串 | 前端自行组装显示 |
| `next_step` | 下钻动作集合（现在的单值升级为数组更合适） | `next_step: ['timeline','logs','issue','device']` |
| `value` / `threshold` / `metric` | 数值口径 | 不变；用户面仅显示 value/threshold，`metric` 只在 L2 |
| `key` | 稳定去重键（`alert:12` / `task:34` / `change:...`） | 不变（前插去重要用） |

**关键字检索（2026-10-18 拍板：走服务端，不做前端本地过滤）**：
`GET /api/obs/events?q=<kw>`（管理台 `/api/admin/obs/events?q=` 同）。实现要点：

- 关键字**只走参数绑定**（`LIKE ?`），SQL 文本里绝不出现字面 `%` —— 这正是本轮踩过的地雷
  （字面 `%` + 参数 → pymysql 当格式符抛错 → 调用处 try 吞掉 → 用户面事件整类静默消失）。
- 下沉位置：告警来源 → `rule_name`/`dim_key`（`alerts.list_events*` 的 SQL）；
  变更 → `events.title/actor/issue_id`；审计 → `audit_logs.event_type/detail`（仅 L2）；
  任务来源 → 服务端对该来源的当前卡住/失败列表过滤（它本身是 `ops_overview` 的 Top-N 视图，
  不是历史表，没有可下沉的 SQL）。
- 长度上限 64（`Q_MAX`），响应回显 `q`；`%`/`_` 与日志检索（`logs.search`）一致按通配符处理
  —— 两端行为一致优先于"更严格"。
- **索引结论（实测后不改 DDL）**：`LIKE '%kw%'` 是前缀不确定的扫描，B-tree 索引用不上；
  而各来源查询本身已被 `status/last_seen_at`、`repo_key`、`seq DESC LIMIT ?` 这类条件限定，
  事件接口实测中位 30ms（审计 §五.B）。故**不新增索引**；若将来事件量级上来，
  再走 MySQL FULLTEXT / SQLite FTS5（跨方言需在兼容层单独适配）。

**游标分页契约（已实施）**：`GET /api/obs/events?limit=50&before=<ISO>|<key>&sources=&state=&repo=&q=`
→ `{events: [...], next_cursor: "<ISO>|<key>" | null, total: n(本页), truncated_sources: ["task"], by_source, q, state, sources, global}`

- **取数窗口 = 3×limit**（每来源），合并后才有"下一页"可翻；`before` 取上一页 `next_cursor` 原样回传，
  服务端按 `(started_at, key)` 严格小于做切片 → 翻页不重不漏（末页 `next_cursor=null`）。
- ⚠️ 语义边界：各来源取的是**最新一段窗口**，所以翻页深度受该窗口约束；某来源取满窗口会在
  `truncated_sources` 里报出来（= "该来源可能还有更早的事件没进来"），前端据此提示。

**状态过滤**：`state` 参数接受 `firing|resolved|failed|done|stuck`（现在是 firing/resolved 两值，
后端 `_task_events` 里已有 stuck/failed 分支，只是前端没暴露）。

---

## 五、i18n 键表（新增 / 废弃）

新增（zh/en 各一份）：

- `state.firing` / `state.resolved` / `state.stuck` / `state.failed` / `state.done`（**替代** `ev_state.*`）
- `ev_title`（列表标题）、`ev_filter_source`、`ev_filter_state`、`ev_filter_range`、`ev_filter_keyword`
- `ev_load_more`、`ev_updated_at`（含 `{t}` 变量）、`ev_truncated`（含 `{n}`、`{source}`）
- `outcome.failed` / `outcome.completed` / `outcome.cancelled` / `outcome.merged`（结果徽标）
- `ev_expand_*`（展开区字段标签）

废弃：`ev_state`（命名空间对象，**是 `[object Object]` 的根因**，勿再用于 `t()`）、
`event_view_col.*`（列名，表格改流式后不需要）、`task_state.*`（并入 `outcome.*`）。

规矩（写进前端规范）：**`t()` 只传叶子键**；需要回退时统一用 `_obsTOr(key, fallback)`
（值 === key 视为未命中），不要写 `t(k) || x`。

---

## 六、与现有入口的关系

| 入口 | 关系 |
|---|---|
| 告警面板（总览顶部横幅 → 三子视图：事件/规则/静默） | **保留**：那是"要治理的告警"，事件视图是"发生过的一切"。事件视图里 monitor/gap/mutation 行的「时间线」按钮可跳到面板详情的同一事件（用 `key=alert:<id>` 对齐），不重复实现治理动作（静默/改阈值仍在 L2/面板） |
| 任务与日志 › 任务 | 保留；事件行有 `task_id` 时点「日志」跳过去 |
| 任务与日志 › 异常任务 | **可选简化**：异常任务是"任务的过滤视图"，可并入「任务」子 Tab 的过滤 chip（`status=failed/stuck`），从而把子 Tab 从 4 个减到 3 个（任务/事件/日志）。**待定稿** |
| 设备与引擎 | 保留；设备维度事件点「设备」跳设备列表（现有 `obsShowRuntimeTasks`） |

---

## 七、实施拆分与工作量

| 片 | 内容 | 工作量 |
|---|---|---|
| S1 | ~~后端契约：`ts_kind` / `outcome` / `state_label_key` / `target` 结构化 / `next_step` 数组 / 游标分页（`before` + `next_cursor`）/ `state` 全集 / `truncated_sources`；**保持旧字段**，前端可灰度~~ ✅ **已全部实施（2026-10-18，含真机 MySQL 验证）**；`q` 同批完成 | 0.5~1d |
| S2 | ~~前端时间轴流：两段式行 + 行内展开 + 合并过滤行 + 加载更多 + 自动刷新（前插 + 滚动保持）+ 状态徽标~~ ✅ **已实施**（()`_obsEventRowHtml`/`_renderObsEventFlow`/`_obsEventShellHtml`/`obsEventLoadMore`/`_obsEventSoftRefresh`/`obsEventApplyNew`）；「异常任务」子 Tab 并入「任务」的过滤 chip | 1~1.5d |
| S3 | ✅ **已实施**：i18n 键表落地（新增 `state.*`/`outcome.*`/`ev_filter_keyword`/`ev_search`/`ev_load_more`/`ev_new_events`/`ev_truncated` 等，zh/en 同步）+ 真实 i18n 审计用例已覆盖事件视图（真实语言包，断言无原始 key / 无 `[object Object]`）+ 空态/错误态/截断提示 + 本文转"已实施" | 0.5d |

**实施记录（2026-10-18，两次提交）**：

1. `q` 服务端检索 + S1 契约（`fa306cf983` / `c6a7cba055`）—— 后端 `events_view`/`alerts`/两条路由 + 用例；
   真机 MySQL 复核（`%`/`_`/注入串不炸、翻页零重叠、`state=failed` 只出 task）。
2. S2 前端时间轴流 + 子 Tab 合并 + i18n（本次）—— `observability.js` 重写事件页；
   `tests/js/test_observability_events_flow.js` 覆盖"服务端检索 / 加载更多不重不漏 / 30s 前插提示 / 截断提示 / 行内展开"。

**测试要求（硬）**：

1. 后端：游标分页边界（首页/末页/恰好整页/新事件前插去重）、状态全集过滤、可见性
   （越权 404、设备维度并集、平台级不出现）、`point` 类 `duration_sec=0`。
2. 前端：真实 i18n 审计（无 `observability.` 原文、无 `[object Object]`）+ 断言目标元素**确实渲染**；
   分页"加载更多"不重复不遗漏；自动刷新前插保持滚动位置。
3. SQL 文本护栏：新增查询若含字面 `%` 必须用 `substr/instr`（沿用本轮加的兼容层逃逸 + 用例）。

---

## 八、验收标准（定稿后照此验收）

1. 打开「任务与日志 › 事件」：无横滚；每行两段式；进行中的事件在顶部且带"…"标记。
2. 事件视图 30s 自动刷新，新事件前插不跳滚动；关掉开关后按钮变"刷新"。
3. 能翻到更早事件；计数显示"共 N 条（已加载 M）"，某来源截断时有明确提示。
4. 六类来源都有可读标题/状态（无 raw key、无 `[object Object]`），任务类显示"结果"而非"级别"。
5. 点行展开能看到 value/threshold（数值型）、静默/升级标记、可读规则名。
6. L2（管理台）同一组件 + `scope=global`，多出 `audit` 来源与 `metric` 字段。

---

## 九、开放问题（**已定稿 2026-10-18**）

| # | 问题 | 定稿 |
|---|---|---|
| 1 | 子 Tab 顺序 | **并入**：「异常任务」并入「任务」→ `任务 / 事件 / 错误日志`；**入口形态**：任务 Tab 内的按钮 + **弹窗**看明细（2026-10-18 二次拍板；不用内联过滤 chip，也不做事后 DOM 插挂——后者会在 10s 局部刷新时累积成多行，实测踩到） |
| 2 | 自动刷新默认值 | **默认开**：30s 顶部前插新事件（只提示"有 N 条新事件"，不强制滚动） |
| 3 | 审计来源 | **保持隐藏**（L1 不显示 `audit`；`L1_SOURCES/L2_SOURCES` 划分不变） |
| 4 | 关键字过滤 | **服务端检索**（已实施，见 §四）|

原始问题留档：

1. **子 Tab 顺序**：现在是 `任务 / 事件 / 异常任务 / 错误日志`。是否把「异常任务」并入「任务」，
   变成 `任务 / 事件 / 错误日志`？（少一个 Tab，但"异常任务"这个入口会消失）
2. **自动刷新默认值**：30s 前插（推荐）还是默认关、手动刷新？（事件多时前插会打断阅读）
3. **审计来源**：L1 一律不显示 `audit`（现状）确认保持？
4. **关键字过滤**：前端本地过滤（零后端成本，只覆盖已加载页）够用吗？还是要服务端检索
   （需要给 `obs_*` 加全文/前缀索引，见审计 §五 的性能面）？