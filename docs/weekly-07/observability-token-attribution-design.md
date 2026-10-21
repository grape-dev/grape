# 引擎 / 任务 / 事件 三级 token 归因设计（方案，待定稿）

> 需求（用户 2026-10-19 提出）：可观测性里要能分别看到
> ① **引擎层面**：每个引擎的 token 消耗
> ② **任务视图**：每个任务的 token 消耗，且更细一层 —— **每个任务的每个事件**的 token 消耗
>
> 本文只出方案，不动代码。定稿后再按"分期"实施。

---

## 〇、口径修订与实施状态（2026-10-19，实施中）

**"事件"的定义改了**（按用户口径）：本文初稿把"事件"定义成**引擎内部 ACP 步骤**（phase/step_seq）；
用户澄清后定为 **阶段事件 = issue × `agent_type`**（一个 issue 走 `vod → issue` 就是两个事件）。
引擎内部步骤那层**暂不做**（原 §二 的 `phase/step_seq/attrib` 与本 §七 的第 3 期一并搁置，
采集侧因此**零改动**）。原 §二～§四 的步骤口径保留在文内作为将来可选层的设计记录。

**已实施（backend）**：`src/observability/tokens.py` + 4 个 L1 路由
`GET /api/obs/tokens/{engines, tasks, stages, issues/{id}/stages}`；`summary()` 提供三层合计与 `equal` 标志。
真机 MySQL 实测（dev 库 30 天）：17 行明细，`engines = tasks = stages = 7,256,528`（三层相等 ✓）；
阶段示例 `#1 coding 4,404,300 / #1 qa 741,838 / #1 issue 59,586`；`chat` 等运行无 issue → 归 `(无 issue)`。

**已实施（UI，2026-10-19 改版为"内嵌列"，不再单开一块）**：
- **引擎表**（设备与引擎页）加一列 `Token 消耗`（按 `engine_id` = `agent_runtimes.id` 匹配；行点击即打开该引擎 session 列表）
- **session 列表弹窗**两张表把「退出码」列换成 `Token`（该行 = 一次任务执行，Agent 列即阶段 vod/issue/coding/qa
  → 一张表同时给出"每个任务"与"每个阶段"的 token）
- 取数：`/obs/tokens/{engines,tasks}` 建 map（60s 节流），到货后 `_rerenderRuntimeTable()` 刷列；空数据显示 `—` 不显示 0
- 未落点：任务与日志 › 任务 Tab 那张列表当前**没有**退出码列，Token 列待定落点（见待办）

**未做**：引擎内部步骤层（要改采集与上报协议，需真机确认 ACP 事件与 usage 的对应，暂搁置）。

---

## 一、现状：数据在哪、缺什么

**已有（P2-1/token 口径 B 那轮打的地基）**

| 事实 | 位置 |
|---|---|
| 明细表 `obs_llm_usage`（+ `obs_llm_usage_archive` 归档） | `src/api/db_setup.py` |
| 关键列：`dedup_key`(幂等) / `task_id` / `seq` / `turn` / `source` / `engine_id` / `user_id` / `repo_key` / `scene_id` / `agent_type` / `run_id` / `run_kind` / `model_key` / **五类 token**(`input_tokens`/`output_tokens`/`cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens`) / `cost_usd` / `cost_status` | 同上 |
| 归属：`repo_key/scene_id/agent_type/user_id/engine_id` **由主控反查**（任务行 → runtime_id） | `src/observability/llm_usage.py` 模块注释 |
| 采集：worker 侧 `UsageCollector`（任务内引擎）；主控侧 `MasterRun`（chat/IM/devops/wiki 本机引擎） | `llm_usage.py` / `master_usage.py` |
| 粒度：**每条上报记录**（引擎分轮 = `turn`，确定性序号 = `seq`），幂等键 `task:{task_id}:{seq}` / `{run_kind}:{run_id}:{seq}` | `dedup_key_for()` |
| 汇总：`obs_metric_rollup` 有 `token_input/output/cache/total`（**4 项**，明细的 reasoning 未进 rollup），维度只有 `repo:<key>` 与 `global` | `metric_rollup.py` |
| 真实数据抽查（dev 库 2026-10-19）：明细 17 行、**engine_id 全部 >0**、覆盖 5 个引擎；`run_kind` = task 12 / chat 5 | 实测 |

**缺什么**

1. **引擎维度没有汇总**：`engine_id` 在明细里有，但 rollup 只按仓库/全局聚合 → "每个引擎的 token" 只能现扫明细（能做但慢、且图上没趋势）。
2. **没有"事件"这一层**：明细里没有"这条用量属于哪个阶段/哪一步"的字段 → 无法回答"每个任务的每个事件花了多少"。
3. **UI 完全没有暴露**：设备与引擎视图、任务视图都看不到 token。

**结论**：① 只要加聚合 + UI 即可（数据够）；② 需要**采集侧打标**（新增归因列），这是本方案的主要工作量。

---

## 二、粒度与"事件"的口径（关键定义，先定死）

```
引擎  engine_id（= runtime_credentials.id / agent_runtimes.id）
  └─ 任务/运行 run：run_kind='task' → task_id；'chat'/'im'/'devops'/'wiki' → run_id（chat:<conv>:<msg> 等）
       └─ 事件 event：**任务内部的一个可观测步骤**
```

**"事件"的定义（本文采用）**：任务执行过程中**引擎自己上报的阶段/步骤标记**，与任务时间线（`trace.timeline` 的 steps）同源，例如：

- `phase`：粗粒度阶段 —— `plan` / `impl`(编码) / `qa` / `review` / `report` / `other`
- `step_seq`：细粒度步骤序号（ACP 流里的 `step_start/step_finish` 序号，从 1 起）
- `event_key`：可选，指向该任务的一个具体事件（与统一事件视图的 `key` 对齐；如 `task:<id>:failed`）

**归属规则（按优先级，落库时写死，不做事后猜测）**

| 优先级 | 规则 | 精度标记 |
|---|---|---|
| 1 | 采集时引擎/worker 知道当前 step → 直接写 `phase/step_seq` | `attrib='exact'` |
| 2 | 有 ACP 事件流但 usage 上报无 step → 按"上一条 step_start 到本条之间的区间"归到该 step | `attrib='inferred'` |
| 3 | 都没有 → 归到最近的**前序**已知步骤；再不行归 `other` | `attrib='estimated'` |
| — | 主控侧 run（chat 等）：`phase='chat'`，`step_seq=0`，`turn` 保持引擎给的轮次 | `attrib='exact'` |

**不变量（验收硬断言）**：
`Σ 引擎 = Σ 任务/运行 = Σ 事件`（同一窗口、同一可见范围下必须逐分相等；差 1 token 即失败）。
另：重试/补采不得重复计数（沿用 `dedup_key` 幂等；跨机补采 upsert 覆盖）。

---

## 三、数据模型

**3.1 明细扩列（`obs_llm_usage` / `obs_llm_usage_archive` 同步）**

```sql
ALTER TABLE obs_llm_usage ADD COLUMN phase     TEXT    NOT NULL DEFAULT '';   -- plan/impl/qa/review/report/chat/other
ALTER TABLE obs_llm_usage ADD COLUMN step_seq  INTEGER NOT NULL DEFAULT 0;    -- 引擎步骤序号（0=无步骤概念）
ALTER TABLE obs_llm_usage ADD COLUMN event_key TEXT    NOT NULL DEFAULT '';   -- 可选：对齐统一事件视图的 key
ALTER TABLE obs_llm_usage ADD COLUMN attrib    TEXT    NOT NULL DEFAULT '';   -- exact/inferred/estimated
-- 索引
CREATE INDEX idx_obs_llm_task_step  ON obs_llm_usage(task_id, step_seq);
CREATE INDEX idx_obs_llm_engine_ts  ON obs_llm_usage(engine_id, created_at);
CREATE INDEX idx_obs_llm_run_ts     ON obs_llm_usage(run_kind, run_id, created_at);
```

> 老库走 `_ensure_column` 迁移（列为空 → 前端按"未标注"显示，绝不假装 exact）。

**3.2 汇总（复用现有 rollup，不新建表）**

给 `obs_metric_rollup` 增加**引擎维度**：`dim_key = engine:<runtime_id>`（与现有 `repo:` / `global` 并列），
指标沿用 `token_input/output/cache/total`（分钟 7 天 / 小时 30 天，沿用现有留存与追赶式补齐）。

> 可选（第 3 期）再补 `metric='token_cost_usd'`；定价现在是暂缓状态，先只做 token。

**3.3 任务/事件级**

**不建汇总表**，直查明细（`task_id` / `task_id+step_seq` 索引后量级可控；任务维度天然是"单任务内几十条"）。
需要长周期排行（如"近 30 天最费 token 的任务"）时，走 rollup 的 `repo:`/`global` + 明细到任务为上限的 Top-N 查询。

---

## 四、采集点改动（打标）

| 采集点 | 改动 | 说明 |
|---|---|---|
| worker 侧 `UsageCollector`（任务内引擎：codex/opencode/hermes/hwcloud） | `feed()` 时带上"当前 step/phase"（消费 ACP 事件流时维护一个游标：收到 `step_start` → `step_seq += 1`；`done` → `phase='report'`） | 这是**精度最高**的一路；若引擎不报 step，则 phase 按任务类型粗分 |
| 主控侧 `MasterRun`（chat/IM/devops/wiki） | `phase='chat'|'im'|…`，`step_seq=0`；`run_id`/`turn` 维持现状 | chat 的"每个事件"= 每条消息/每个 turn（见 §八 未决） |
| 上报协议 | `report_usage` 请求体加 `phase/step_seq/event_key/attrib`（**全部可选**，老 worker 不带 → 服务端按 §二 规则 3 标 estimated） | 向后兼容 |

---

## 五、API

| 端点 | 范围 | 说明 |
|---|---|---|
| `GET /api/obs/tokens/engines?range=24h&repo=` | L1 可见设备/引擎；L2 全局 | 每引擎：`input/output/cache/total/cost` + 环比、Top-N 任务、趋势序列（读 rollup 的 `engine:<id>`） |
| `GET /api/obs/tokens/engines/{engine_id}` | 同上（越权 404） | 单引擎详情：趋势 + 模型分布 + 该引擎最近任务 |
| `GET /api/obs/tokens/tasks?range=&repo=&agent=&sort=tokens&limit=` | L1 可见仓库 | 任务级列表：`task_id/issue/repo/agent/tokens/cost/attrib 分布` |
| `GET /api/obs/tokens/task/{task_id}` | L1 可见 + L2 | **任务明细**：按 `phase`/`step_seq` 分组的 token + 逐条记录（= 每个事件） |
| `GET /api/obs/tokens/events?task_id=`（可选） | 同上 | 把事件级 token 挂进统一事件视图（加一列 `tokens`） |

口径统一：一律显式时间窗 + 可见性过滤；**不降级成全量**；越权 404。

---

## 六、UI（放哪、显示什么）

**1) 设备与引擎视图（引擎层面）**
- 引擎行/卡片：加一列/一行 `本窗口 token`（`入 12.3k / 出 4.5k / 缓存 8.1k`，成本暂不显示）
- 点开引擎详情（已有 `GET /settings/agent-runtimes/{rid}` 的面板）：加「Token 趋势」小图（读 `engine:<id>` rollup）+「该引擎最费 token 的 Top 5 任务」（点击跳任务时间线）
- 空态：无数据 → `—`（不显示 0，避免误读为"零消耗"）

**2) 任务视图（任务 / 事件两层）**
- 任务列表（任务与日志 › 任务）：加一列 `Token`（可排序；带 `attrib` 标记的小图标：精确/推断/估算）
- 任务时间线弹窗（`trace.timeline`）：**每一步右侧显示该步 token**，顶部显示任务合计；未标注的步骤显示 `未标注`
- 统一事件视图：可选加 `Token` 列（事件级），只对"有事件级归因"的行显示
- 排序/筛选：按 token 降序找"最费的步骤"；筛选 `attrib=estimated` 单独看（便于排查打标覆盖度）

**3) 总览**：现有 `token_trend`（按仓库）保持不变；第 3 期加"Top 引擎"卡片。

---

## 七、分期实施与验收

| 期 | 内容 | 依赖 | 预计 |
|---|---|---|---|
| 1 | 引擎维度：rollup 加 `engine:<id>` + `/tokens/engines(+/id)` + 设备视图列/趋势 | 无（数据已够） | 1 天 |
| 2 | 任务维度：`/tokens/tasks` + 任务列表列 + 时间线逐步 token（先用 `turn`/时间窗推断 `inferred`） | 需要 `phase/step_seq` 列（先落列 + 迁移） | 1~1.5 天 |
| 3 | ~~事件级打标（引擎内部步骤）~~ **暂不做**（用户 2026-10-19 决定；§二/§四 的 `phase/step_seq/attrib` 一并搁置，采集侧零改动） | — | — |

**验收（硬性）**
1. **三层合计相等**：同一窗口 `Σ引擎 = Σ任务 = Σ事件`（含 `run_kind != task` 的运行），差 1 即失败；
2. 幂等：重复上报/重试/跨机补采后合计不变；
3. 可见性：L1 越权引擎/仓库 → 404；平台级只在 L2；
4. 方言：SQLite 与 MySQL 两条路径都对（本仓 conftest 强制 SQLite，故**必须真机 MySQL 复核**）；
5. UI：引擎列、任务列、时间线逐步 token 真实渲染（不是"扫空页=零问题"），空态与"未标注"可区分；
6. 精度标记：`attrib` 三档在 UI 可区分，`estimated` 不冒充精确。

---

## 八、未决问题（需要你定 / 需要真机验证）

1. **引擎是否上报步骤标记**：ACP 流里的 `step_start/step_finish` 与 usage 上报之间能否稳定对应（决定第 3 期是 `exact` 还是只能 `inferred`）→ 我建议拿一个真实任务跑一遍、把两边的 seq/时间戳对齐验证后再定稿。
2. **"每个事件"的定义边界**：任务内事件取"引擎步骤"（本文采用），还是也要覆盖平台事件（排队/派发/评审/合并）？后者没有 token 可归（那是平台动作）。
3. **chat/IM 的"事件"**：一条消息算一个事件，还是按引擎 turn 再拆？影响主控侧 `step_seq` 语义。
4. **成本列**：定价仍处"暂缓"，本期只做 token；`cost_usd` 已在明细里，等定价定了直接出列。
5. **归档表查询**：明细超 90 天后进 `obs_llm_usage_archive`，长周期任务查询是否要跨表 union（影响 §3.3 的"不建汇总表"结论）。
