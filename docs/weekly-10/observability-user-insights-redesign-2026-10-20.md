# 可观测性重设计：用户维度洞察 + 引擎/Token 成本归因

> 版本：v1.0（2026-10-20 重建于 docs/weekly-10/，原 10-20 写入 docs/weekly-07/ 的副本因仓库 checkout 丢失）
> 前置文档：observability-redesign-design.md（运行驾驶舱）、observability-token-attribution-design.md（三级归因）、observability-llm-usage-design.md（usage_collect）、observability-guance-deep-research-v2.md（观测云调研）
> 本报告是用户维度重设计的输入材料：调研观测云等业界方案 + 工程洞察（Engineering Insights）业界实践，围绕需求「仓下各参与用户的数据情况」给出数据模型与产品形态建议。

---

## 1. 需求与现状差距

**新需求**：在 grape 控制台看到"这个仓下各参与用户的数据情况"——谁在参与、参与多深、贡献质量如何、花了多少 Token/成本。

**现状盘点（本机 dev 库）**：

| 数据源 | 用户维度字段 | 现状 |
|---|---|---|
| `events` | `actor`（事件触发人，如 issue 创建/评论者） | ✅ 有，但未做聚合展示 |
| `agent_tasks` | `user_id`, `account`（任务发起/关联用户） | ✅ 有，未利用 |
| `obs_llm_usage` | `user_id`, `repo_key`, `agent_type`, `engine_id`, token/cost 分列 | ✅ 字段齐全（六字段归因已落地），user_id 归因链路刚启用 |
| `issue` | 仓库关联 | 可作为聚合维度 |
| `task_messages` | 无独立 user 列（经 agent_tasks 关联） | 需 join |

**差距结论**：数据基础已具备（obs_llm_usage 的列设计已对齐 Braintrust 六字段标准），缺的是：① 用户维度的聚合查询与展示层；② events.actor 与 tasks.user_id 的身份归一（同一人多平台账号）；③ 贡献质量类指标（PR 粒度）完全没有。

---

## 2. 业界调研

### 2.1 观测云（Guance）——国内最完整的对标

观测云 2026 已把 AI 可观测做成完整产品线（与我们的场景高度同构）：

- **AI Agent 可观测**：Session → Trace → Span 层级，统一分析模型调用、工具执行、Token 消耗、风险事件；查看器支持按应用/风险等级/Token 区间/Session ID 筛选，先看会话总数、高风险数、平均 Trace 数，再下钻。
- **LLM 可观测**：模型请求、Token 成本、延迟、错误、应用链路聚合。
- **ABA（智能体行为安全监控）**：检测 Agent 工具调用、高危操作、异常消耗——把"安全"维度并入可观测。
- **obs-agent-connector**（开源）：一个 CLI 统一接管 Claude/Codex/Cursor 等 coding agent 的遥测采集——与 grape worker 侧 usage_collect 定位一致，"connector + hooks 模式"可参考。
- **观测云 2026 路线图**四个方向：Blueprint（观测编排 DAG）、变更观测、BYOS（数据主权）、AI/LLM 成本归因。核心判断："Agent 行为不确定 + Token 贵"是 AI 业务健康度的核心矛盾。

**可借鉴**：按"会话→链路→调用"三层收口浏览路径；风险/成本双维度；接入侧给每次调用一个"应用身份"（app_id + client token）——grape 等价物是 task_id + session token。

### 2.2 工程洞察（SEI/Engineering Insights）——用户贡献维度的业界标准

**平台格局**：DX（研究驱动，DORA+SPACE+DevEx 融合成 DX Core 4）、LinearB（指标+自动化）、Swarmia、Jellyfish、Faros（开源）。

**DX Core 4 框架**（速度/效果/质量/影响四维）：
- 速度：PRs per engineer（仅组织级平均，明确警告**不要用于个人考核**）、lead time、部署频率
- 效果：开发者体验指数（问卷）、time to 10th PR（新人上手速度）
- 质量：change failure rate、失败恢复时间
- 影响：新能力投入占比

**DORA 四指标**是业界基石：部署频率 / 变更前置时间（分解为 coding→pickup→review→merge 四段）/ 变更失败率 / 恢复时间。LinearB 基准表有明确 elite/good/fair 分档线。

**GitHub 原生 Insights 的局限**（CodePulse 等第三方批评）：Contributors 图只有 commit 数、Pulse 是静态快照无趋势、无团队切片、**无 bot 过滤**——AI 参与度高的仓库里 bot/AI 账号会严重污染人类指标。

**AI 时代的用户洞察（LinearB 2026 报告，8.1M PR 样本）**——对 grape 尤其重要：
- PR 三分类：**Unassisted（纯人写）/ AI-assisted（人主导 AI 辅助）/ Agentic（纯 agent 产出）**
- 关键数据：AI-assisted PR 30 天合并率 32.7% vs 人类 84.5%；agentic PR pickup 时间是人类的 4.6-5.3 倍；AI-assisted PR 体积 P75 超 400 行（人类 2.5 倍）
- 结论：**采纳率已饱和，区分度在交付质量**——"谁在用 AI"不重要了，"AI 产出能否走完交付链路"才是组织分水岭

**Flyte Pulse / GitHub Insights 社区实践**（可直接抄的口径）：
- bot/AI 账号识别：`[bot]` 后缀 + GraphQL `__typename == Bot` + 配置名单，bot 的 PR/commit/review 全部单列不计入人类指标
- **AI 归因**：识别 `Co-Authored-By` / `Assisted-By` commit trailer 中的 AI 工具名——每个贡献人可展示"AI 使用率"
- review 覆盖率 = 被他人 review 过的 merged PR 占比（自查不计）
- 公司/组织归属：按贡献当天的 affiliation 记账
- 新贡献者定义：时间窗内首次贡献

---

## 3. grape 场景的适配设计

### 3.1 用户实体归一（前置条件）

grape 的特殊性：一个参与者在不同环节有不同身份——GitCode 用户名（events.actor）、OAuth user_id（agent_tasks.user_id）、issue 触发人。现有 `unified_user_mapping` 表正是为此准备的。设计要求：

- 所有用户维度聚合必须先过 mapping 归一到统一 user_id
- 展示名取 user_accounts 的昵称，多平台账号（gitcode/github）映射到同一人
- bot/agent 身份（grape_bot 等）单独标记 `kind=bot`，所有人类指标默认排除，单列"AI 参与"统计

### 3.2 指标体系（三层）

**L1 用户卡片（仓内参与者总览）**——回答"谁在参与、参与多深"：

| 指标 | 来源 | 说明 |
|---|---|---|
| 触发事件数 | events by actor | issue 创建/评论/PR 操作等 |
| 发起任务数 | agent_tasks by user_id | 含 agent 类型分布 |
| 任务成功率 | agent_tasks status | 按 agent 分列 |
| Token 消耗 / 成本 | obs_llm_usage by user_id | input/output/cache 分列 |
| 使用引擎偏好 | obs_llm_usage engine_id | opencode/hermes/aishell 分布 |
| 活跃天数 | events+tasks 去重日 | 最近 N 天 |
| 首次参与时间 | events 最早记录 | 新用户识别 |

**L2 贡献质量（PR 粒度）**——回答"产出质量如何"，需新增 PR 维度指标化（webhook 已有 pull_request 事件，落地到 events 但未做指标化）：

| 指标 | 说明 |
|---|---|
| 创建 PR 数 / 合并率 | 人 vs agent 产出的 PR 分开统计 |
| PR review 参与度 | 该用户 review 过的 PR 数（自查排除） |
| pickup 时间 | PR 创建→首次 review（LinearB 同款口径） |
| agentic PR 占比 | 用户触发流水线中 agent 产出 PR 的比例 |
| AI 辅助率 | LinearB 三分类在用户粒度的展开 |

**L3 成本洞察**——三级归因（引擎/任务/阶段）之上加用户维度 rollup：

- per-user token/cost 趋势（median + p99——防单任务爆款扭曲）
- 用户 × agent_type 成本矩阵（谁在哪个环节烧钱）
- 成本异常告警（单用户单日超阈值）

### 3.3 数据与查询设计

1. **不新建表**，以 `obs_llm_usage`（成本）+ `agent_tasks`（任务）+ `events`（行为）三表为聚合源，API 层 join 后聚合；量级上来后再落 ClickHouse/TimescaleDB（Langfuse v4 宽表模式：每行冗余 user/repo/agent 上下文，免 join）。
2. **聚合缓存**：用户洞察 API 加 5 分钟缓存（复用 machine-health 模式）。
3. **bot 过滤**：所有"人类"指标 SQL 统一带 `kind != 'bot'` 条件；agent 产出的 PR 按 agent_tasks.user_id 是否为空判定。
4. **展示入口**：仓库详情 → 「参与洞察」页（用户偏好：功能入口放详情页内），默认时间窗 30 天可切换，用户列表按活跃度排序，点用户卡片进 L2/L3 下钻。

### 3.4 分期落地

- **P1（快）**：用户卡片 L1 全指标——纯查询层开发，数据都已就位（2-3 天）
- **P2**：PR 粒度质量指标——需补 events 的 PR 维度指标化 + bot 识别规则
- **P3**：身份归一完善（unified_user_mapping 自动补全）+ 用户 × 成本矩阵 + 告警
- **P4（远期）**：观测云式 Session→Trace 下钻整合 + 存储 ClickHouse 化 + DX Core 4 风格团队对比视图

---

## 4. 与既有设计的关系

- 三级归因（引擎/任务/阶段）保持不变，用户维度是**第四个归因轴**（user_id 在 obs_llm_usage 里已有，纯展示层增量）
- 运行驾驶舱（三页合一）不动；用户洞察作为仓库详情下的新入口，不复用驾驶舱布局
- usage_collect 采集链路不动；如参考 obs-agent-connector 模式，未来可把采集从"引擎旁路"升级为"引擎 hook"，但不是本期范围

## 5. 风险与注意

1. **个人绩效敏感**：DX/LinearB 都明确警告 PR 数类指标不可用于个人考核（易被 game、有滥用史）——产品文案定位为"协作洞察"而非"绩效排名"，默认只展示组织/团队聚合，个人明细仅本人和管理员可见
2. **user_id 归因断链**：obs_llm_usage 早期数据 user_id 可能缺失——聚合 API 需要兜底"未归属"桶，且不能把未归属算到任何人头上
3. **bot 污染**：grape 场景 bot 密度远高于普通仓库——过滤规则要做白名单/黑名单配置化，防误杀

---

## 参考

- 观测云 AI Agent 可观测：guance.com/product/ai-agent-observability
- 观测云 2026 路线图：guance.com/learn/articles/article-197
- GuanceCloud/obs-agent-connector（GitHub）
- DX Core 4：getdx.com、thenewstack.io 4-north-star-metrics
- LinearB 2026 Engineering Benchmarks（AI PR 三分类数据）
- LinearB DORA 指标指南与基准表
- Flyte Pulse（flyteorg/flyte-pulse，贡献统计口径与 AI 归因实现）
- duynhlab/github-insights（bot 过滤 + by-user 指标实现）
