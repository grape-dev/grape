# 能效看板重构设计（对齐 Flow Efficiency Board 参考样式）

> 状态：**设计稿（待评审后实施）**
> 参考样式：设计稿截图（能效看板.jpg，未随仓提交）（10 卡片：流量 KPI / 周吞吐积压 / 周期时间 / 积压龄期 / 在办分布 / 首响）
> 已定参数（2026-11-09 拍板）：⑤ 年龄阈值默认 **7 天**；⑩ 默认展示**全部**（不预选分组）
> 修订（2026-11-09 v1.1）：⑩ 由「首响」改为「任务交付时长」——grape 为自动处理场景，实测 issue→任务派发中位 0.01 分钟（97.4% ≤1 分钟），首响无信息量且真人评论仅覆盖 5 个 issue，详见 §4.4
> 现状实现：`src/api/routes/dashboard.py::get_efficiency_board`（Issue #1229）+ `src/api/static/js/pages/observability.js::renderObsEfficiency`（3 KPI + 贡献者表）
> 定位：从"按协作者列数字"升级为"看工作流入、积压、周期、瓶颈、响应"——**看瓶颈不看绩效**

---

## 1. 现状 vs 目标

| 维度 | 现状（#1229） | 目标 |
|---|---|---|
| 视图 | 3 个 KPI（提需求/完成/token）+ 一张贡献者表 | 4 行 × 3 列共 10 个卡片 + 1 整行 |
| 分析视角 | 个人产出排行 | 流效率（Flow）：流入/积压/周期/瓶颈/响应 |
| 时间维度 | 无（全量累计） | 周维度（W34–W45）+ 本周/累计双口径 |
| 统计量 | 计数 + token | 计数 + 分位数（P50/P75/P90）+ 分布直方图 |

**保留**：token 归因链路（obs_llm_usage→task→issue 创建人）不删，作为第 ⑨ 卡"在办分布"的辅助列（可选展开），不再作为主视觉。

---

## 2. 目标布局（4 行 × 3 列 + 1 整行）

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 能效 · <repo>   [个人│团队]  [看板│能效]      统计范围：N 张卡片 · 已关闭 M │
├──────────────┬──────────────┬─────────────────────────────────────────────┤
│ ① 净流入      │ ② 总工作量    │ ③ 在制工作项 WIP                             │
│ 82 建 − 64 关 │ 520 张        │ 187 张未关闭（未认领 1）                      │
│ = +18 净(W45) │ 累计进入      │                                              │
├──────────────┼──────────────┼─────────────────────────────────────────────┤
│ ④ 本周完成    │ ⑤ 超龄未关     │ ⑥ 周吞吐与累计积压                            │
│ 64 张 (W45)   │ 68 张 >2 周    │ 柱(新建/关闭) + 折线(累计积压) W34–W45        │
├──────────────┼──────────────┼─────────────────────────────────────────────┤
│ ⑦ 周期时间    │ ⑧ 积压龄期     │ ⑨ 在办分布（找瓶颈，不是绩效）                 │
│ P50/P75/P90   │ P50/P90 + 桶  │ 按处理人排序 + bot 灰列 + 未分配               │
│ + 分布桶      │ + 归因入口     │                                              │
├──────────────┴──────────────┴─────────────────────────────────────────────┤
│ ⑩ 成效：任务交付 — P50/P90 + 按结果分组(completed/failed) + 时长分布桶          │
└───────────────────────────────────────────────────────────────────────────┘
```

响应式：<1100px 时列数降为 1，卡片纵向堆叠；⑥⑩ 全宽。

---

## 3. 指标口径与数据映射（核心）

数据源全部为本地已有表，**零新建表**（与插件化路线一致）：

| # | 卡片 | 口径定义 | 数据来源 | 数据缺口/注意 |
|---|---|---|---|---|
| ① | 净流入 | 本周新建数 − 本周关闭数 = 净（按 ISO 周，周一起算，本地时区）；附"近 4 周净流入" | `issue.created_at` / `issue.completed_at` | 无 |
| ② | 总工作量 | 累计进入 = 已关闭 + 未关闭 | `issue` 全量计数 | 排除 `is_subtask_mode=1`（子任务不重复计） |
| ③ | 在制工作项 | 未关闭数（status≠completed），附"未认领"= assignee 与 created_by 皆空 | `issue` | 与 ⑨ 合计一致（同源） |
| ④ | 本周完成 | 本周 `completed_at` 落在当周的 issue 数 | `issue.completed_at` | 按关闭时间归属，非创建时间 |
| ⑤ | **超龄未关**（替代"超期未关"） | 未关闭且 `now - created_at > 阈值`（**默认 7 天**，`age_threshold_days` 可配） | `issue` | ⚠️ **库里无期限字段**；webhook payload 虽带 `issue.due_date` / `milestone_id`（41 条事件含该字段），但**实测 due_date 非空 = 0**（GitCode 侧从没人设期限），且 grape 未落库——照搬参考图的"超期"永远是 0 且误导，改为年龄阈值口径，卡片文案标注"按龄期，非期限" |
| ⑥ | 周吞吐与累计积压 | 每周新建柱 + 每周关闭柱 + 累计积压折线（未关闭累计，每周周末快照） | `issue` 按周分桶 | 折线需历史快照；无历史时用"截至当周未关闭数"回算（见 §4.3） |
| ⑦ | 周期时间 | 已关闭 issue 的 `completed_at - created_at`；P50/P75/P90 + 6 桶（<1天/1-3天/3-7天/1-2周/2-4周/>4周）+ 样本数 | `issue` | 需 `completed_at` 非空且 > created_at，脏数据剔除并计数上报 |
| ⑧ | 积压龄期 | 未关闭 issue 的 `now - created_at`；P50/P90 + 8 桶（<1h/1-4h/4-8h/8-24h/1-3天/3-7天/1-2周/>2周）+ "归因分析"入口（跳在办分布并按 >2 周过滤） | `issue.created_at` | 无 |
| ⑨ | 在办分布 | 未关闭 issue 按处理人（`created_by` 空则 `assignee`）分组计数，降序；bot 单列灰色；"未分配"独立一格 | `issue` + `_efficiency_bot_usernames()` | 复用现有 bot 名单；附带各人 token 消耗（可选列） |
| ⑩ | **成效：任务交付**（替代"首响"） | agent 任务 `completed_at - started_at`；P50/P90 + 6 桶（<1min/1-5min/5-15min/15-60min/1-3h/>3h）+ 样本数；按结果分组（completed / failed / cancelled） | `agent_tasks`（started_at, completed_at, status, issue_id, repo_key） | ✅ 363 样本可用。**为何弃用首响**：grape 无"等人接"环节，实测 issue→任务派发中位 0.01 分钟；真人评论事件仅 16 条覆盖 5 个 issue |

### 3.1 三个诚实性原则（参考图本身就在示范）

1. **数据不全要显示不全**：⑩ 必须显示样本量（"样本 363/379"）、⑦ 必须显示"样本 32"——只给分位数不给样本量就是假象
2. **口径不对就改名**：⑤ 从"超期"改"超龄"，并在卡片内注明"无期限字段，按龄期"
3. **bot 必须分列**：grape 场景 agent 是主要执行者，⑨ 的 bot 行单列不参与人类排名；⑩ 只统计 agent 任务时长，不与人工动作混算

---

## 4. 后端设计

### 4.1 端点

**单请求聚合**（与 obs/overview 同模式，避免首屏 10 个并发请求）：

```
GET /api/dashboard/efficiency/board?repo=<owner/repo>&weeks=12&age_threshold_days=7
```

响应结构（每卡片一段，前端按段渲染）：

```json
{
  "repo": "grape-dev/grape",
  "range": {"week": "2026-W45", "weeks_back": 12, "generated_at": "..."},
  "summary": {"created_total": 520, "completed_total": 333, "open_total": 187, "unassigned": 1},
  "net_flow": {"week_created": 82, "week_completed": 64, "week_net": 18,
               "last4_weeks_net": 137, "backlog_trend": "up"},
  "throughput": {"weeks": ["W34", "..."], "created": [..], "completed": [..], "backlog": [..]},
  "cycle_time": {"p50": 1.4, "p75": 4.7, "p90": 8.6, "sample": 333,
                 "buckets": [{"label": "<1天", "count": 153}, ...]},
  "aging": {"p50": 7.9, "p90": 25.8, "open": 187,
            "buckets": [{"label": "<1小时", "count": 0}, ...], "over_threshold": 68},
  "wip_by_assignee": [{"name": "FXF-1688", "count": 62, "kind": "human"}, ..., 
                      {"name": "grape_bot", "count": 5, "kind": "bot"},
                      {"name": "未分配", "count": 1, "kind": "unassigned"}],
  "task_delivery": {"p50_min": 1.0, "p90_min": 4.9, "p99_min": 22.0,
                    "sample": 363, "total_tasks": 379,
                    "buckets": [{"label": "<1分钟", "count": 140}, ...],
                    "by_status": [{"status": "completed", "count": 298, "p50_min": 1.2},
                                  {"status": "failed", "count": 61, "p50_min": 0.0}],
                    "group_by": "all"}
}
```

### 4.2 代码结构

```
src/observability/efficiency.py         ← 新增：纯聚合模块（无 HTTP 依赖）
  ├── week_bounds(ts) / iso_week_label()   时间工具（本地时区周一起算）
  ├── build_net_flow() / build_throughput() / build_cycle_time()
  ├── build_aging() / build_wip_distribution() / build_task_delivery()
  └── build_board(repo, weeks, age_threshold) -> dict（编排上述函数）
src/api/routes/dashboard.py              ← 新增 /efficiency/board 路由（薄壳：鉴权 + 调 build_board）
```

**为什么独立成模块**：与插件化路线 §2.1 对齐——后续阶段 A 可直接把这批函数注册为 resolver（`efficiency_*` 指标族），零改动被看板/导出复用。

### 4.3 周吞吐与累计积压的实现（唯一需要技巧的点）

累计积压 = 每周周末的未关闭数。两种实现：

- **A（推荐，无需历史表）**：用 `created_at`/`completed_at` 回算——对第 W 周周末：`count(created_at <= W_end) - count(completed_at <= W_end)`
- B（远期）：落周快照表（`eff_weekly_snapshot`），适合数据量上千后

本设计取 A，纯 SQL 回算，12 周 × 2 次聚合，成本可控。

### 4.4 任务交付时长的实现细节

**为什么换掉首响**（2026-11-09 实测 dev 库）：

| 检查项 | 实测结果 | 结论 |
|---|---|---|
| issue → 首个 agent 任务派发延迟 | 116 个样本，**中位 0.01 分钟**（不到 1 秒），P75 0.02 分钟，97.4% ≤1 分钟 | 自动流水线无"等人接"环节，首响恒为零 |
| `issue_comment` 事件 | 仅 16 条，覆盖 **5 个 issue**（主任务 307 个） | 真人响应样本不足以成图 |
| `events.issue_id` | 386 行全部 NULL/0；issue 号在 `payload.issue.iid`（`number` 恒 null） | 无法直接 JOIN |
| `issue.due_date`（payload 内） | 41 条事件含该字段，**非空 = 0** | 期限数据不存在（⑤ 改用龄期） |
| agent 任务时长 | **363 个样本**（379 个任务中 363 个有 started_at+completed_at） | ✅ 可用 |

**指标定义**：任务交付时长 = `agent_tasks.completed_at - agent_tasks.started_at`（分钟）。

```sql
SELECT status,
       TIMESTAMPDIFF(SECOND, started_at, completed_at) / 60.0 AS minutes,
       repo_key, issue_id
FROM agent_tasks
WHERE repo_key IN (...)                    -- 仓库隔离
  AND started_at IS NOT NULL AND started_at <> ''
  AND completed_at IS NOT NULL AND completed_at <> ''
  AND completed_at >= started_at           -- 剔除脏数据（时钟倒挂）
```

**分桶**（分钟，绝对边界）：`<1` / `1–5` / `5–15` / `15–60` / `1–3小时` / `>3小时`

**按结果分组**：`completed` / `failed` / `cancelled` 三组各自出 P50/P90 与桶分布；**默认展示"全部"**（`group_by=all`），用户可切到单组。

实测基线（dev 库，用于验收比对）：全部 363 样本 P50 **1.0** 分钟、P75 2.5、P90 4.9、P99 22、最长 60；completed 298 条 P50 1.2；failed 61 条 P50 0.0（失败几乎立即发生，这是引擎/配置问题的信号）；cancelled 4 条 P50 8.7。

**时间格式注意**：`agent_tasks` 的 `started_at`/`completed_at` 是带时区的 ISO8601（UTC），`issue.created_at` 是无时区本地时间（Asia/Shanghai）——跨表计算必须先转 CST，否则差 8 小时（实测踩到过，见 §8 风险 2）。

**已知局限**：
- 时长含引擎排队与思考时间，不区分"纯执行"与"等待模型返回"——要拆分需 OTel span，属后续阶段
- 短任务（<1 分钟）占比高（中位 1.0 分钟），按 agent_type 切换才能看出 qa/sre/coding 的真实量级差异

**顺带修正两处前期口径**：
- ⑦ 周期时间可用样本 = **32 条**（307 主任务中仅 32 个 `status='completed'` 且有 `completed_at`；另 2 条 cancelled）。P50/P90 统计意义弱，卡片必须显示样本数
- ④ 本周完成 / ⑥ 关闭柱同理只在这 32 条里出数——"关闭"数据天然稀疏，不是查询 bug

---

## 5. 前端设计

### 5.1 渲染结构

复用现有 observability.js 的 `_kpiRowHTML` / `table` 工具与 CSS 变量色板（`--a/--g/--y/--r/--pu`），新增：

```
renderEffBoard(container, data)
├── _effHeaderHTML()          统计范围说明 + 数据同步时间 + 个人/团队切换
├── 卡①-⑤  : _effKpiCardHTML(label, value, sub, tone)   数字卡（大数字 + 一句解释）
├── 卡⑥    : _effThroughputChart(el, data)             ECharts 双轴（柱+柱+折线）
├── 卡⑦-⑧  : _effDistributionHTML(el, data)            分位数字 + 水平条形桶图
├── 卡⑨    : _effWipBarsHTML(el, data)                 水平条形排序 + bot 灰色
└── 卡⑩    : _effTaskDeliveryHTML(el, data)            KPI + 结果分组切换 + 桶图
```

ECharts 已 vendored（`src/api/static/vendor/echarts.min.js`），直接复用；不需要新库。

### 5.2 视觉规范（对齐参考图）

| 元素 | 规范 |
|---|---|
| 背景/卡片 | 页面浅灰底，卡片白底 + 1px 浅灰边框 + 8px 圆角 |
| 主色 | 蓝（新建/主数据条）`#4a7fd1` |
| 语义色 | 绿（关闭/完成）`#22a06b`、黄橙（积压折线/警示）`#e0a02a`、红（净流入正/风险）`#d64545`、灰（无数据/bot）`#8b8b8b` |
| 数字 | tabular-nums，KPI 大号 28-32px，辅助说明 12px 三级灰 |
| 解释文案 | 每个卡片底部一行小字说明口径（参考图每卡都有，这是它的精髓——"520 张累计进入，不是还剩多少"） |

### 5.3 交互

- 卡⑧"归因分析(68 张)" → 切到 ⑨ 并加 `>2周` 过滤 chip
- 卡⑨ 点击某人 → 任务与日志 Tab 带 `assignee=<name>` 过滤（复用现有深链）
- 个人/团队切换：个人 = 只看当前登录用户；团队 = 全仓（默认团队）
- 卡片 hover 显示精确数值与口径 tooltip

### 5.4 空态与降级

- 未选仓库 → 整板空态："请选择仓库"（现有行为保持一致）
- 某卡片数据不足（如样本 < 30）→ 该卡显示"样本不足，仅供参考"中性文案，不显示 0
- 后端聚合异常 → 卡片级降级（该卡显示"—"，其余正常），不整页报错

---

## 6. 实施分期

| 阶段 | 内容 | 验收（强断言） |
|---|---|---|
| **P1**（2 天） | 后端 `efficiency.py` + `/efficiency/board`：①②③④⑤⑥ 六段（流量与吞吐） | 与 DB 手工 SQL 对账一致；结账：扣掉子任务数后 总数=关闭+未关闭；周分桶边界用 3 个已知 issue 验证 |
| **P2**（2 天） | 后端 ⑦⑧⑨ 三段（周期、龄期、在办分布）+ bot/未分配分类 | P50/P90 与手工计算一致；⑨ 合计 = ③ 的 WIP 数 |
| **P3**（2 天） | 后端 ⑩ 任务交付时长 + 按结果分组 + 分桶（含短/长任务量级校验） | 抽样 10 个 agent_task 手工核对 started_at→completed_at；by_status 各组合计 = 样本数；确认 qa/sre/coding 量级差异 |
| **P4**（2-3 天） | 前端 10 卡片渲染 + 视觉规范 + 交互（归因、下钻）+ i18n（zh/en locales） | 视觉走查对齐参考图；node -c 语法检查；三档窗口（1440/1100/768）截图确认 |
| **P5**（1 天） | 旧 `renderObsEfficiency` 下线；`/api/dashboard/efficiency` 标 deprecated（保留 6 个月） | 全站无引用旧函数；openapi 重新生成 |

合计约 9-10 天。

---

## 7. 与既有体系的关系

- **不破坏**：`/api/dashboard/efficiency` 旧端点保留（前端下线后标 deprecated），token 归因链路不动
- **可复用**：`efficiency.py` 的聚合函数即插件化路线阶段 A 的 resolver 候选（`efficiency_net_flow` / `efficiency_cycle_time` 等），届时可直接接 `/api/obs/q/metrics`
- **数据缺口登记**：⑤ 无期限字段（按龄期替代）、⑦ 样本仅 32 条（须显式标注）——两处都要在卡片上写清，不能靠"看起来正常"掩盖
- **⑩ 换指标的依据**：首响在自动处理场景无信息量（派发中位 0.01 分钟），改测"任务交付时长"，363 样本

## 8. 风险

1. **样本量偏小**：⑦ 周期时间仅 32 条（307 主任务里只有 32 个 completed）、⑩ 363 条但 qa/sre/coding 各族量级差异大——所有分位数必须带样本数
2. **周分桶时区**：必须固定"本地时区周一起算"，否则与用户认知的周不齐（services 已用 Asia/Shanghai 语义）
3. **bot 名单漂移**：⑨ 依赖 `_efficiency_bot_usernames()`，名单缺失会把 agent 算进人类排名（现有函数已 fallback，但要监控）
4. **性能**：单请求 10 段聚合，12 周回算 2 次全表聚合——issue 表当前千级，可接受；上万后需加 5 分钟缓存或周快照表
5. **dev 库基线失真（实测）**：`issue` 表最新记录停在 2026-10-20，当前已是 11-09——273 个未关闭 issue **全部 >14 天**，⑤ 卡在 dev 上会显示 100% 超龄。这不是口径错误，是测试库 20 天没有新需求。对策：看板头部固定显示"issue 数据最后同步时间"，超过 3 天标注"数据陈旧"，避免把停更当积压
6. **时间基准**：所有"现在"类计算（⑤⑧）以服务端时间对齐 CST，不用浏览器时间（防跨时区用户看到不同数字）

---

## 9. P1 实施记录（2026-11-09 完成）

已落地：`src/observability/efficiency.py`（新增 430 行）、`GET /api/dashboard/efficiency/board`、
`tests/test_efficiency_flow_board.py`（11 条断言全绿）。commit `f61e4dbdd`。

与设计稿的差异（逐条说明，避免"文档说一套代码做一套"）：

| 项 | 设计稿 | 实际实现 | 原因 |
|---|---|---|---|
| 聚合位置 | §4.3 写"纯 SQL 回算" | **Python 侧聚合**（一次取轻量列，分桶在内存） | conftest 强制 SQLite，MySQL 的 `YEARWEEK`/`TIMESTAMPDIFF` 在单测里不可用；仓库级 issue 行数百级，内存聚合成本可忽略，换来方言无关 + 口径唯一 |
| 周期时间单位 | 固定"天" | **按 P90 量级自动选 minutes / hours / days** | 自动化场景周期常是分钟级，固定"天"会输出 "P50 0.0 天"（实测 hello-flask 首个实现即如此） |
| 数据新鲜度 | §8 风险里建议"显示最后同步时间" | 已实现为 `data_quality` 段（`issue_last_sync` / `stale` / `stale_days`） | 把风险变成契约字段，前端直接消费 |
| ⑤ 阈值 | 默认 7 天 | 默认 7 天，clamp 1–90；`weeks` clamp 4–26 | 防越界参数打爆聚合 |

实测对账（dev 库，`gitcode:requiredsa/hello-flask`）：

| 指标 | 看板输出 | 独立 SQL | 一致 |
|---|---|---|---|
| 总工作量 / 已关闭 / 未关闭 | 52 / 9 / 43 | 52 / 9 / 43 | ✅ |
| 超龄 >7 天 | 43 | 43 | ✅ |
| 周期样本 | 9 | 9 | ✅ |
| 周新建 W35–W40 | 11,2,26,5,7,1 | 11,2,26,5,7,1 | ✅ |
| 周关闭 W35–W36 | 6,3 | 6,3 | ✅ |

已知表现：该仓 issue 最后活动 2026-09-29（`stale_days=41.1`、`stale=true`），
所以 ①⑥ 本周全 0、⑤ 100% 超龄——这是数据停更的真实反映，不是聚合错误。

**P1 未做（按计划留给后续）**：⑧ 龄期分布图（数据已算在 `aging.buckets`，前端 P4 渲染）、
⑩ 任务交付时长（P3）。

## 10. P2 实施记录（2026-11-09 完成）

已落地：`build_wip_distribution` + `load_uid_names` + `is_bot_name`（§9 同文件），
`build_board` 输出新增 `wip_by_assignee`；测试增至 **15 条**全绿。

⑨ 在办分布口径：
- 归属链：`created_by` → `assignee` → `user_id` 映射（unified_user_mapping）→ 「未分配」
- 分列：人类按数量降序 → bot 行（`kind=bot`，前端灰色，不计入人类排名）→ 「未分配」垫底
- 强口径：`wip_by_assignee.total` 必须等于 `summary.open_total` 与 `aging.open`
  （三处同源同数，测试 `test_wip_total_equals_aging_open_in_board` 钉死）
- `top2_share` 直接暴露"负载是否集中在两人"——参考图那张卡的核心问句

bot 判定两条规则取并集（与 dashboard.py::_is_bot_name 一致，Issue #1337 教训）：
1. 形态规则 `bot` / `*_bot` / `*-bot`（裸 `endswith('bot')` 会误杀 abbot/robot/talbot）
2. 名单：`managed_repos.bot_username` + 全局配置，由路由层 `_efficiency_bot_usernames()` 注入

实测（dev 库，真实数据）：

| 仓库 | 未关闭 | 人类占比 | Top2 集中度 | bot | 未分配 |
|---|---|---|---|---|---|
| requiredsa/hello-flask | 43 | 100% | 100%（全部 requiredsa） | 0 | 0 |
| requiredsa/ai-interview-swarm | 28 | 32% | 32% | **18（gt_test_bot 64%）** | 1 |

第二个仓库正好证明 bot 分列的必要性：不过滤的话 `gt_test_bot` 会占据榜首 64%，
图表会误导成"某个人承担了大半工作量"。SQL 独立对账逐项一致。

**P2 未做**：⑩ 任务交付时长（P3，已完成，见 §11）、token 消耗列（设计标注"(可选)"，
本阶段不做，避免把能效板和成本归因两件事混在一起）。

## 11. P3 实施记录（2026-11-09 完成）

已落地：`build_task_delivery` + `_fetch_task_rows` + `_task_durations_minutes`，
`build_board` 输出新增 `task_delivery`；端点新增 `group_by` 参数；测试增至 **18 条**全绿。

契约（按 §4.1 原定，**单位统一分钟**）：
```json
"task_delivery": {
  "p50_min": 0.18, "p90_min": 2.26, "p99_min": 3.38,
  "sample": 45, "total_tasks": 46, "dirty_skipped": 0,
  "group_by": "all",
  "buckets": [{"label": "<1分钟", "count": 37}, ...],
  "by_status": {"completed": {"p50_min": 0.18, "sample": 35, ...},
                "failed": {"p50_min": 0.0, "sample": 10, ...}},
  "by_agent_type": {"vod": {"p50_min": 0.18, "sample": 42}, "issue": {...}}
}
```

实现要点：
- **不做单位自适应**：本卡从 2 秒到 3 小时都有，任何固定单位都会在某些组输出
  "P50 0.0"。后端只给分钟原值，前端按值格式化（<1 分钟显示秒）
- `group_by` ∈ {all, completed, failed, cancelled}，非法值静默回退 all；
  `by_status` / `by_agent_type` 始终全量拆分，切组不用二次请求
- `sample` 只算 started_at + completed_at 都有的行；`total_tasks` 如实报总数
  （差值就是排队中/缺时间戳的任务）
- agent_tasks 缺表时降级为空（最小测试库/老库不报 500）
- 时区安全：本卡只在 agent_tasks 内部做减法，UTC 差值与时区无关；
  **不跨 issue/agent_tasks 算差值**（那会差 8 小时）

实测对账（dev 库 `requiredsa/ai-interview-swarm`）：

| 项 | 看板 | 独立 SQL | 说明 |
|---|---|---|---|
| 任务总数 | 46 | 46 | ✅ |
| 有时长样本 | 45 | 45（10 failed + 35 completed） | ✅ |
| completed 样本 | 35 | 35 | SQL 按 status+起止过滤后为 35；按 status 单条件会有 36，多出的 1 条**缺 started_at**（`t_74fce60ffdb3`），正确地不计入时长 |
| 分桶合计 | 37 + 8 = 45 | — | ✅ |

数据画像：该仓 91% 任务 <5 分钟（vod 类事件处理），`failed` 组 P50 接近 0（几乎立即失败），
`issue` 类 P50 0.58 分钟。**这印证了 ⑩ 与 ⑦ 的分工**：⑦ 看需求多久关闭（32 样本），
⑩ 看任务多久干完（45 样本），两者上下夹住交付链路。

**P3 未做**：前端渲染（P4，已完成，见 §12）、OTel span 拆分"纯执行 vs 模型等待"（后续阶段，需 trace 数据）。

## 12. P4/P5 实施记录（2026-11-09 完成）

前端已切新版（commit `0204191b3`），旧端点标 deprecated。

实现：
- `renderEffBoard`：3 列网格 + 底部整宽卡，与参考图版式一致；CSS 用注入的
  `#effBoardStyle`（不污染全局 app.css）
- ①-⑤ 数字卡、⑥ ECharts 双轴（柱新建/关闭 + 折线累计积压）、⑦⑧⑨⑩ 分布卡
- ④ 加"近 6 周关闭"火花柱、⑤ 加龄期分布条：这两张卡原本只有数字，
  在等高网格里留白过大；补上同源数据后密度与参考图接近
- ⑩ 分组切换在客户端完成（`by_status` 已全量返回），切组不发请求
- 数据陈旧横幅：`stale_days > 3` 时置顶告警，写明"停更 ≠ 全线积压"
- 交互：`近 4/8/12/24 周` 下拉（重取数据）、⑩ `全部/成功/失败/取消` 切换

视觉验证（headless chromium 1440 宽截图）：
新版预览截图（能效看板-新版预览.png，用 dev 数据渲染，未随仓提交）。
判读：10 卡全部渲染、无错位溢出、图表正常；已知留白集中在 0 值分桶卡
（参考图同样保留 0 值行，属于刻意对齐）。

P5：`GET /api/dashboard/efficiency` 标 `deprecated=True` 并注明保留 6 个月；
旧 `renderObsEfficiency` 已删除，前端无残留引用。

**已知遗留（不改，记录在案）**：
1. ⑤ 超龄未关 与 ⑧ 积压龄期 在 grape 上信息重叠——参考图的 ⑤ 是"期限已过"，
   grape 无 due date 只能按龄期，导致两卡同源。若后续接入期限字段再拆开
2. `个人/团队` 切换未做：后端目前只有仓库维度，无用户维度聚合；
   强行做会造出假数据，留给需要时再加（需要 API 支持 user 过滤）
3. ⑧ 的"归因分析"按钮未做：⑨ 数据不含龄期维度，无法按 >2 周过滤

### 12.1 评审反馈修订（2026-11-09 第二轮）

1. **卡片内多行行距过大** —— 根因不是 gap（原 5px），而是 **类名与 app.css 撞车**：
   计数列用了 `class="ct"`，而 app.css 里 `.ct` 是主内容容器
   （`padding:22px 28px;flex:1`），每行被撑到 59px。改用 `ef*` 前缀
   （`eftrack/eflb/efnum/efnum2/efcell/efsp/efsb/efsl/efunit/efcnt/efon/efspacer`）
   后，实测行高 15px、步距 17px（注入调试测量：`ROW h=14.84px`、`WRAP gap=2px`）。
   `.espark .sb` 也撞了侧栏 `.sb`、`.espark .sp` 撞了工具栏 `.sp`，一并改名。
2. **在办分布只看得到"新建"** —— 后端 `build_wip_distribution` 增加 `completed`
   （同一归属链统计已完成），`completed_total` 汇总；前端改双列「未关闭 / 已完成」。
   实测 hello-flask：requiredsa 43 / 8。
3. **⑩ 脚注说明句删除** —— 移除 `delivery_foot` 文案与其 key。

教训（写进代码注释）：主服务前端是全局 app.css + 各页面内联样式，**两字母类名
（ct/tr/lb/sp/sb/p/u）几乎必然撞车**；页面级注入样式一律加前缀。

### 12.2 下钻能力（2026-11-09 第三轮）

**新增两个端点**

| 端点 | 用途 | 参数 |
|---|---|---|
| `GET /api/dashboard/efficiency/daily` | 周 → 日拆解（每日新建/关闭/当日累计积压） | `repo` / `week=2026-W45`（空则当前周） |
| `GET /api/dashboard/efficiency/issues` | 桶 → issue 列表（编号/标题/状态/阶段/场景/龄期或耗时/归属/创建时间） | `repo` / `kind=aging\|overdue\|cycle` / `bucket`（桶标签）/ `threshold_days` / `limit` |

强口径：`build_daily` 与该周的周桶同源回算，**周值 = 该周 7 天之和**（测试断言）；
`build_issue_list(aging, <桶>)` 的条数必须等于 `build_aging().buckets[<桶>]`，
`overdue` 等于 `over_threshold`。

**前端交互**
- ④ 本周完成的火花柱：点任一周 → 该周每日拆解弹窗（柱+折线+7 天明细表）
- ⑥ 周吞吐双轴图：点某一周（柱或折线）→ 同一个弹窗
- ⑤超龄未关 / ⑦周期时间 / ⑧积压龄期：点任一分桶行 → 该桶的 issue 列表弹窗
- 弹窗支持 ESC / 点遮罩 / 右上 × 关闭；场景列取 `devops_pipeline_scene.display_name`
  （缺失回退 name，再缺回退原始 scene_id——不静默编造）
- ⑨ 在办分布改双条：蓝色=未关闭、绿色=已完成，右侧双列数字（43 / 8）

**踩到的坑（值得记）**：`.efdual` 里两条轨道用了 `flex:1`，在纵向容器 + 自动高度下
flex-basis 为 0，轨道被压成 0 高（实测 `efdual h=2`，2px 全是 gap，条形完全看不见）。
改成 `flex:0 0 7px / 0 0 4px` 才正常。

### 12.3 弹窗与在办分布修订（2026-11-09 第四轮）

1. **弹窗贴顶**：遮罩改 `align-items:center`，modal 加 `max-height:calc(100vh-48px);overflow:auto;margin:auto`
   —— 长列表在弹窗内滚动，弹窗本体居中
2. **创建时间列溢出**：表格改 `table-layout:fixed` + `<colgroup>` 定列宽（场景列 126px），
   `td{word-break:break-word}`；场景/标题单行省略 + title tooltip；表头 `position:sticky` 吸顶
3. **在办分布两条线合成一条**：单条轨道，长度 = 该人合计（未关闭+已完成），
   段内蓝 = 未关闭占比、绿 = 已完成占比；图例加"整条 = 合计"。
   （上一版两条分开画看不出总量比例，被评审指出）

### 12.4 在办分布：条长必须与排序一致（2026-11-09 第五轮）

问题：所有条一样长 + 没有按总数排。

两个根因：
1. **条一样长**：`.ebrow .eftrack{flex:1}` 被 `.efbarwrap .eftrack` 继承，flex-grow 把每行拉满，
   内联 `width:x%` 失效 → 改 `flex:0 1 auto` 并显式保留 width
2. **排序错**：`build_wip_distribution` 原本按「未关闭数」排序，而条长画的是「合计」
   → 改成按 `open + completed` 排序；**人和 bot 一起排**（不再把 bot 固定垫底），
   否则会出现"条更长却排在下面"的自带矛盾；bot 仍标灰且 `top2_share` 只算人类，
   未分配仍垫底（它不是人）

顺带确认：⑨ 是 WIP 口径——**没有在办的人不进这张卡**（只有完成记录的人不出现），
这一点写进测试 `test_wip_sorted_by_total_and_bar_length_variable`。

### 12.5 issue 列表分页 + 懒加载（2026-11-09 第六轮）

问题：某桶 148 条全量返回，弹窗被撑成超长页。

做法：
- 后端 `build_issue_list` 加 `offset`，`total` 恒为全量条数，`items` 只回当前页
  （路由 `limit` 默认 20、上限 200，`offset` 默认 0）
- 排序加次级键（metric desc, number asc），保证翻页结果稳定、不重不漏
- 前端 `_effIssueState` 保存 kind/bucket/page/pageSize/total，翻页即按需请求；
  已取过的页进 `_effIssueCache`（Map），返回上页不重复请求
- 请求带 `reqId` 守卫：快速连续翻页时丢弃过期响应，避免后到的旧页覆盖新页
- 分页条：`第 x–y 条 / 共 n 条` · 每页 20/50/100 · 上一页 / 第 p / t 页 / 下一页；
  首页禁用上一页、末页禁用下一页
- 弹窗本身限高 `calc(100vh-48px)` 内滚动，表格区随页切换显示 loading 占位

实测（148 条的 ">2周" 桶）：第 1 页 1–20、第 2 页 21–40、共 8 页，上一页/下一页
状态正确；测试 `test_issue_list_pagination_offsets_without_overlap` 断言
三页并集 = 全集且互不重叠、越界页返回空但 total 不变。

### 12.6 弹窗高度上限（2026-11-09 第七轮）

评审反馈"弹窗太长"：原上限 `calc(100vh-48px)` 基本铺满屏幕。

改为 `max-height:min(62vh,560px)` + 弹窗内部 flex 列（标题固定、表格区 `overflow:auto`、
分页条固定底部）。实测 900px 视口：`modalH=558 (62%)`，
`#effIssueTable clientH=436 / scrollH=522 / scrollable=true / overflowY=auto` ——
即弹窗不再撑满，20 行记录在表格区内滚动，分页条始终可见。
