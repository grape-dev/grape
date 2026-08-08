# VOD 技能优化升级实施计划（知识基线 + 需求引导 + Issue 生命周期审视）

> 版本：v2.0
> 日期：2026-08-08
> 状态：待评审
> 需求：
> 1. **VOD 分析升级**：VOD 技能当前仅做开发者声音分析并建 Issue，缺乏项目全方位理解（代码/Issue/PR/历史事件），
>    无法精准识别开发者需求。优化目标：VOD 在引导开发者声音前，先构建项目知识基线，再通过
>    多轮引导把模糊需求收敛为清晰准确的需求。
> 2. **VOD 接管 Issue 审视**：VOD agent 接管 `issue_opened` 事件——所有非 VOD 自己创建的 Issue，都先由 VOD
>    审视一遍；若发现不清晰，与开发者沟通（可在 Issue 上，也可在飞书群）；目标是把 Issue 收敛到足够清晰，
>    再基于模板总结 Issue，最后通过 A2A 通知 Issue Agent 进行后续处理。
> 3. **Issue 生命周期状态机**：`issue_updated`/`issue_comment` 等事件按 Issue 当前所处阶段路由到对应 Agent
>    （vod → issue → coding → qa → sre），以 `issue_state` 表为权威判定。
> 范围：**技能文件 + 代码文件一起实现**（先更新本文档，代码实施见 §四/§八）。

---

## 一、Agent 定位与目标

### 1.1 Agent 职责边界（本次刷新的核心定位）

| Agent | 职责 | 产物 | 不负责 |
|-------|------|------|--------|
| **VOD Agent** | 开发者声音 → 清晰开发者需求（收集、分析、识别、沟通引导、结构化） | 清晰需求（含验收标准） | spec/标签/里程碑/分配 |
| **Issue Agent** | 清晰需求 → 可开发任务（spec 设计、打标签、排里程碑、分配责任人）= 现有 issue-advance | 可开发任务 / Spec | 原始声音→需求的澄清 |

**开发者声音的两个来源**：
1. **来源 A（GitCode Issue）**：开发者在 GitCode 直接提交的 Issue 是原始声音，尚不构成可开发需求 → VOD 分析、识别、与开发者沟通引导，使其变成清晰开发者需求
2. **来源 B（飞书 VOD Bot）**：VOD Bot 在飞书群与开发者多轮沟通，将用户需求变为明确开发需求 → VOD 主动在 GitCode 创建 Issue

**交办**：VOD 将需求收敛为明确开发需求后，通过 A2A 通知 Issue Agent 进行 spec 设计、打标签、排里程碑、分配开发责任人等（即现有 issue-advance 职责）。

### 1.2 优化目标

| 目标 | 说明 |
|------|------|
| 建立项目知识基线 | VOD 处理前先理解项目代码/架构/能力（复用 issue-advance 的 project-info 机制） |
| 项目历史全貌 | 了解 Issue/PR/事件历史，判断反馈是否重复、是否已有 PR 解决 |
| 需求引导漏斗 | 从模糊需求 → 多轮澄清 → 收敛为清晰准确需求（一轮清晰则直接建 Issue） |
| 结构化产出 | 收敛后按模板建 Issue（含目标/现状/期望/验收/优先级） |
| **Issue 审视** | **接管 `issue_opened`：非 VOD 创建的 Issue 先审视清晰度；不清晰则与开发者沟通澄清；清晰后按模板总结 → A2A 交办 Issue Agent** |
| **Issue 生命周期** | **`issue_updated`/`issue_comment` 等事件按 Issue 当前阶段（vod/issue/coding/qa/sre）路由到对应 Agent** |

### 1.3 已确认决策

| # | 决策 |
|---|------|
| D1 | **共用同一份项目知识基线**：VOD 与 issue-advance 共用 `data/workspaces/{owner}/{repo}/project-info/`（避免重复生成、口径一致） |
| D2 | **自主判断轮次**：VOD Agent 自行判断需求是否已清晰——一轮即清晰则直接建 Issue；不够清晰则在飞书群多轮对话引导 |
| D3 | **需求收敛模板**：收敛后按新模板建 Issue（含验收标准） |
| D4 | **技能 + 代码一起实现**：`skills/vod/` + `configs/agent-description.json` + 路由/状态机代码（不再仅技能文件） |
| D5 | **接管 `issue_opened` 审视**：VOD 审视所有非自己创建的 Issue；不清晰则与开发者沟通（Issue 评论或飞书）；清晰后按模板总结，A2A 通知 Issue Agent 处理 |
| D6 | **双触发场景**：VOD 同时承担「开发者声音引导建 Issue」（`vod_signal`/飞书）与「已有 Issue 审视」（`issue_opened`）两条职责，共享知识基线 |
| D7 | **审视自建排除**：VOD 自己创建的 Issue（`auto-submitted by VOD Agent` 标记）跳过审视，避免自我循环 |
| D8 | **`issue_opened` 恒给 VOD**：新 Issue 一律先进 VOD 审视阶段 |
| D9 | **`issue_updated`/`issue_comment` 按阶段归属**：取决于 Issue 当前处于哪个阶段（vod/issue/coding/qa/sre）；由 `issue_state` 表权威判定 |
| D10 | **阶段权威来源 = 新增 `issue_state` 表**（决策确认：比依赖 event.json 更可靠） |
| D11 | **状态机阶段命名**：`vod` → `issue` → `coding` → `qa` → `sre` → `done`（认可） |

---

## 二、现状与差距

### 2.1 VOD 技能现状（`skills/vod/SKILL.md`）

流程：`vod_signal` 事件 → 分析事件内容 → 聚类分析 → 价值提炼 → 创建 Issue → 输出摘要。
（仅 `vod_signal` 单触发，无 `issue_opened` 审视职责，无生命周期概念。）

### 2.2 差距（Gap 分析）

| 维度 | 现状 | 目标 |
|------|------|------|
| 项目理解 | 无知识基线，泛泛分析 | 先构建 project-graph + project-capabilities |
| 历史全貌 | 仅 prompt 注入的 `get_issue_summary`（单行流） | 知识基线 + 历史反馈聚类 + 近期事件 |
| 重复判断 | 无 | 基于历史反馈画像判断是否已提/已解决 |
| 需求引导 | 无，模糊需求直接建 Issue | 多轮澄清漏斗，收敛后建 Issue |
| 产出结构 | 简单 title/body | 结构化模板（目标/现状/期望/验收/优先级） |
| **Issue 审视** | **不承担；`issue_opened` 由 Issue Agent 直接处理（`agent-description.json` 订阅）** | **VOD 接管审视：清晰度判断 → 澄清 → 结构化总结 → A2A 交办 Issue Agent** |
| **Issue 生命周期** | **无状态机；`issue_updated`/`issue_comment` 由 `get_domain_agent` 按事件类型静态路由** | **按 `issue_state` 阶段动态路由到当前归属 Agent（vod/issue/coding/qa/sre）** |

### 2.3 可复用资产（不重复造轮子）

| 资产 | 位置 | 用途 |
|------|------|------|
| 项目知识基线流程 | `skills/issue-advance/SKILL.md` + `references/codegraph-project-analysis.md` | VOD 复用，生成/校验 project-info |
| project-info 目录 | `data/workspaces/{owner}/{repo}/project-info/` | 共用，含 project-graph.md / project-capabilities.md / .metadata.json |
| Issue 聚类摘要 | `src/api/event_store.get_issue_summary`（prompt 已注入） | 现状保留 |
| event.json 档案 | `src/api/event_archive.py`（M1，`a2a_to` 字段已预留） | 富上下文 + A2A 交办记录 |
| A2A 协议 | `webhook.py:68` `POST /api/a2a`（agent=issue 等） | 阶段间交办（VOD→issue→coding→qa→sre） |
| 阶段聚合 | `src/api/routes/pipeline.py`（AGENT_ORDER = vod/issue/coding/qa/sre） | 生命周期展示（与 issue_state 互补） |

---

## 三、方案设计

### 3.0 Issue 生命周期状态机（核心，D9/D10/D11）

#### 3.0.1 状态定义与流转

```
vod ──A2A──► issue ──A2A──► coding ──A2A──► qa ──A2A──► sre ──► done
 ▲            ▲              │  ▲            │  ▲
 └──── 回退 ──┘              └──回退─────────┘  （qa 发现问题 A2A 回 coding）
```

| 阶段 | 归属 Agent | 进入条件 | 离开条件（A2A 交办） |
|---|---|---|---|
| `vod` | VOD | issue_opened / 新 Issue 无记录 | VOD 收敛需求 → A2A issue |
| `issue` | Issue Agent | VOD A2A 交办 | spec-ready → A2A coding |
| `coding` | Coding Agent | Issue A2A 交办 | PR 创建 → A2A qa |
| `qa` | QA Agent | Coding A2A 交办 | 通过→A2A sre；发现问题→A2A 回 coding |
| `sre` | SRE Agent | QA A2A 交办 | 发布完成 → done |
| `done` | — | 关闭/已发布 | — |

#### 3.0.2 权威存储：新增 `issue_state` 表（D10）

```sql
CREATE TABLE IF NOT EXISTS issue_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key     TEXT NOT NULL,
    issue_id     TEXT NOT NULL,           -- 如 "#5"
    stage        TEXT NOT NULL DEFAULT 'vod',  -- vod/issue/coding/qa/sre/done
    owner_agent  TEXT NOT NULL DEFAULT 'vod',  -- 当前归属 agent（= 最新被交办者）
    a2a_seq      INTEGER,                 -- 触发当前 stage 的 A2A 事件 seq
    updated_at   TEXT DEFAULT (datetime('now')),
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, issue_id)
)
```

#### 3.0.3 事件路由判定（D9）

```
事件进入 run_pipeline
  ├─ event_type == issue_opened → 强制 vod 阶段（建 issue_state 行，D8）
  └─ event_type in (issue_updated, issue_comment, ...) →
       查 issue_state.owner_agent
       ├─ issue → 交给 issue agent
       ├─ coding → 交给 coding agent
       ├─ qa → 交给 qa agent
       ├─ sre → 交给 sre agent
       └─ 无记录 / vod → 交给 vod agent（默认）
```

- 阶段推进：A2A 交办时更新 `issue_state.owner_agent`（`webhook.a2a_endpoint`）
- 回退允许：qa→coding、coding 问题回退（状态机允许，见 §九 回退上限）
- `issue_state` 为**唯一权威**；`event_archive.a2a_to` 仅记录（不回读判定）

### 3.1 场景 A：开发者声音引导建 Issue（`vod_signal` / 飞书消息）

```
vod_signal 事件 / 飞书消息
    │
    ▼
① 项目知识基线（必须最先）
   - 读取 data/workspaces/{owner}/{repo}/project-info/
   - 不存在或过期 → 按 codegraph-project-analysis.md 生成/更新（与 issue-advance 共用）
   - 降级：codegraph 不可用 → README + API 基础信息（标注 degraded）
    │
    ▼
② 项目历史全貌（API 获取）
   - 近期 Issue 列表（GET /issues?state=all&per_page=20）→ 判断是否重复/已解决
   - 近期 PR 列表（GET /pulls?state=all&per_page=10）→ 是否已有方案
   - 历史反馈画像：聚类此前「auto-submitted by VOD Agent」的反馈 Issue，识别高频主题
    │
    ▼
③ 需求引导漏斗
   - 判断当前消息需求是否已清晰
   - 已清晰（一轮）→ 直接进入 ④ 建 Issue
   - 不清晰 → 飞书群多轮引导（每次 1-3 个澄清问题），直至收敛
   - 引导维度：目标用户/场景、现状问题、期望行为、验收标准、优先级
   - 引导上限：最多 3 轮；仍不清晰则建"待澄清"Issue 并说明
    │
    ▼
④ 结构化建 Issue（新模板，标注 auto-submitted by VOD Agent）
   → 建 issue_state（stage=vod，owner=vod）
    │
    ▼
⑤ A2A 交办 Issue Agent（收敛为清晰需求后）
   → issue_state.owner=issue
    │
    ▼
⑥ 输出摘要
```

### 3.2 场景 B：Issue 审视（接管 `issue_opened`，D5/D7/D8）

```
issue_opened 事件
    │
    ▼
① 项目知识基线（同 3.1 ①，若已存在则复用）
    │
    ▼
② 建 issue_state（stage=vod, owner=vod，D8）
    │
    ▼
③ 自建排除（D7）：检查 Issue 是否含「auto-submitted by VOD Agent」标记
   - 是（VOD 自己创建的）→ 直接 A2A 交办 Issue Agent（或按现状继续）
   - 否 → 进入 ④ 审视
    │
    ▼
④ 清晰度审视：读取 Issue 完整内容（title/body/标签/评论），对照四要素判断
   - 目标 / 现状 / 期望行为 / 验收标准
   - 结合知识基线判断需求是否可执行、是否重复、是否有歧义
    │
    ├─ 清晰（四要素齐全）→ 进入 ⑥
    │
    └─ 不清晰 → ⑤ 与开发者沟通澄清
              ├─ 渠道：Issue 评论（@开发者追问）或 飞书群（若绑定）
              ├─ 方式：结合知识基线提出 1-3 个具体澄清问题
              ├─ 轮次：≤ 3 轮；每轮基于上一轮回答追问，不重复
              └─ 超限仍未清晰 → 在 Issue 评论列出已获取/缺失信息，标记「待澄清」后交办 Issue Agent
    │
    ▼
⑥ 结构化总结：按模板将 Issue 正文收敛为清晰结构（目标/现状/期望/验收/优先级/来源）
    │
    ▼
⑦ A2A 交办 Issue Agent
   - curl POST /api/a2a  {"agent":"issue", "payload":{repository, account, issue_number, context:"VOD 已审视并结构化，请继续 spec/标签/里程碑/分配处理"}}
   - issue_state.owner → issue
```

### 3.3 Issue 审视判断标准（清晰度四要素）

| 要素 | 判断问题 | 不清晰示例 |
|------|---------|-----------|
| 目标 | 一句话能说清要达成什么？ | "搞一下这个" |
| 现状 | 当前行为/缺失是否明确？ | 未说明当前表现 |
| 期望行为 | 期望的具体行为/场景步骤是否清晰？ | "更好用点" |
| 验收标准 | 是否可验证？ | 无任何可检验描述 |

> 结合知识基线：若 Issue 描述的需求与项目现有能力明显冲突/重复，也视为"需澄清"或"重复"，在评论中说明。

### 3.4 需求引导漏斗（多轮对话规则，场景 A/B 通用）

| 规则 | 说明 |
|------|------|
| 第一轮 | 提取消息意图，判断清晰度；模糊则提 1-3 个针对性澄清问题（结合知识基线，问得具体） |
| 判断标准 | 目标/现状/期望/验收 四要素齐全 → 清晰 |
| 多轮 | 每次基于上一轮回答追问，不重复已确认项 |
| 上限 | 最多 3 轮；超限仍未清晰 → 场景 A 建「待澄清」Issue；场景 B 在 Issue 评论列缺失项后交办 |
| 语气 | 中文、简洁、引导式（不用"请填写模板"式，用具体问题） |

**引导示例**（基于知识基线，问得具体）：
```
用户："这个项目能不能支持批量操作？"
VOD（结合 project-graph 已知是 FastAPI + 任务队列）：
  "您指的是哪个对象的批量操作？（当前项目有任务、仓库、用户三类管理对象）
   另外期望的批量操作是仅后端接口，还是需要前端入口？"
```

### 3.5 结构化 Issue 模板（D3）

```markdown
## 目标
{用户想达成的结果，一句话}

## 现状
{当前行为/缺失，结合知识基线描述}

## 期望行为
{清晰描述期望，可含使用场景步骤}

## 验收标准
- [ ] {可验证的验收项 1}
- [ ] {验收项 2}

## 优先级
P0（紧急）/ P1（重要）/ P2（一般）

---
来源：{飞书用户反馈 / Issue 评论 / PR 评论}
auto-submitted by VOD Agent（Feishu）
```

> 场景 B 审视总结时复用此模板：`PATCH /issues/{number}` 将 body 收敛为该结构；来源标注「VOD 审视结构化」。

### 3.6 prompt 注入（代码项）

`src/im/vod_handler_async.py:_build_vod_prompt` 当前注入 `get_issue_summary`。
优化：追加 `build_issue_context`（event.json 富上下文）+ 知识基线摘要 + 历史反馈聚类 + 当前 issue_state 阶段。
与生命周期路由代码一并实施。

---

## 四、文件改动清单

### 4.1 `skills/vod/SKILL.md`（重构核心）

- 更新 description：新增"项目知识基线 + 需求引导漏斗 + Issue 审视 + 生命周期"语义
- 处理流程改为 **§3.1 场景 A（飞书引导）+ §3.2 场景 B（Issue 审视）** 双流程
- 新增「Issue 审视」章节：自建排除（D7）、清晰度四要素判断（§3.3）、澄清沟通、结构化总结、A2A 交办
- 新增「需求引导漏斗」章节（§3.4 规则，两场景通用）
- 新增「结构化 Issue 模板」（§3.5）
- 新增「生命周期交办」说明：交办后 issue_state 归属转移，后续 issue 事件由新归属 Agent 处理
- 硬约束补充：知识基线必须最先；引导不超 3 轮；审视自建排除；建/总结 Issue 用新模板；交办时 A2A（issue_number 必填）
- 操作边界更新：新增"多轮对话引导"✅、"Issue 审视澄清"✅、"A2A 交办 Issue Agent"✅；仍不建 PR/不改代码

### 4.2 `skills/vod/references/`（新增 2 个参考）

| 文件 | 内容 |
|------|------|
| `references/codegraph-project-analysis.md` | 从 issue-advance 复用（或引用指向，避免双份维护） |
| `references/requirement-elicit-guide.md` | 需求引导漏斗 + Issue 审视澄清规则 + 清晰度四要素 + 结构化 Issue 模板 + A2A 交办格式 |

> 倾向：codegraph 参考**复用 issue-advance 的同一文件**（`skills/issue-advance/references/codegraph-project-analysis.md`），VOD SKILL.md 里直接引用该路径，避免双份维护；requirement-elicit-guide 为 VOD 独有，新建。

### 4.3 `configs/skill-description.json`（VOD 描述更新）

- VOD 技能描述同步"知识基线 + 需求引导 + Issue 审视"语义

### 4.4 代码变更（D4：技能 + 代码一起实现）

#### 4.4.1 数据模型：`issue_state` 表（D10）

- `src/api/db_setup.py`：新增 `issue_state` 建表 DDL（见 §3.0.2）+ 索引（repo_key+issue_id UNIQUE）
- `src/api/session_store.py`：新增 `get_issue_stage(repo_key, issue_id)` / `set_issue_stage(repo_key, issue_id, stage, owner_agent, a2a_seq=None)`

#### 4.4.2 事件路由（D8/D9）

- `src/api/event_pipeline.py` `run_pipeline`：
  - `event_type == issue_opened` → 建 issue_state（stage=vod, owner=vod）→ 交给 VOD
  - `event_type in (issue_updated, issue_comment, ...)` → 查 `issue_state.owner_agent` 决定 `_execute_agent` 交给谁（覆盖 get_domain_agent）
  - 无 issue_state 记录 → 回退 get_domain_agent（现状兼容）
- `src/api/routes/webhook.py` `a2a_endpoint`：
  - A2A 交办时更新 `issue_state.owner_agent = 目标 agent`（写入 a2a_seq）
  - 同时写 `event_archive.a2a_to`

#### 4.4.3 event_archive `a2a_to` 写入

- `src/api/event_archive.py`：A2A 交办时在对应 pipeline 段写入 `a2a_to`（当前恒 None）

#### 4.4.4 订阅调整

- `configs/agent-description.json`：
  - `vod.subscribe_events` 增加 `issue_opened`
  - vod/issue/coding/qa/sre 均订阅 `issue_updated`/`issue_comment`（由 issue_state 二次分发，消除静态路由歧义）

#### 4.4.5 prompt 注入（§3.6）

- `src/im/vod_handler_async.py` `_build_vod_prompt`：追加知识基线摘要 + `build_issue_context` + 当前阶段

### 4.5 测试

- 新增 `tests/test_issue_state.py`：建表/幂等/get/set/stage 流转（vod→issue→coding→qa→sre→done）
- 新增 `tests/test_issue_lifecycle_routing.py`：issue_opened→vod；stage=issue 时 comment→issue agent；stage=coding 时→coding agent；qa 回退 coding；done 后不再分发
- 新增 `tests/test_vod_skill.py`：SKILL.md 含知识基线/引导漏斗/审视/交办/硬约束
- 回归：`test_qa_skill`（qa 订阅调整）、`test_m2_execution_mode`、全量 pytest

---

## 五、验收用例

| ID | 场景 | 预期 |
|----|------|------|
| VD-1 | SKILL.md 含知识基线步骤 | 有"项目知识基线"且标注"必须最先" |
| VD-2 | 复用 project-info | 引用 `data/workspaces/{owner}/{repo}/project-info/` 或 issue-advance 参考 |
| VD-3 | 历史全貌获取 | 有"近期 Issue/PR 列表"步骤 + 重复/已解决判断 |
| VD-4 | 历史反馈画像 | 有"auto-submitted by VOD Agent 反馈聚类"逻辑 |
| VD-5 | 引导漏斗一轮清晰 | 明确"一轮清晰 → 直接建 Issue" |
| VD-6 | 引导漏斗多轮 | 有"多轮对话 + 3 轮上限 + 超限建待澄清 Issue" |
| VD-7 | 结构化模板 | 含目标/现状/期望行为/验收标准/优先级/来源 |
| VD-8 | 硬约束 | 知识基线最先、不建 PR/不改代码 |
| VD-9 | 描述同步 | skill-description.json VOD 描述更新 |
| VD-10 | 回归 | 现有 VOD 相关测试通过 |
| VD-11 | Issue 审视接管 | SKILL.md 有"非 VOD 创建的 Issue 需审视"流程 |
| VD-12 | 自建排除 | 有"auto-submitted by VOD Agent 跳过审视"（D7） |
| VD-13 | 澄清渠道 | 有"Issue 评论 或 飞书群 沟通澄清" |
| VD-14 | 审视后 A2A 交办 | 有"审视清晰后 A2A 通知 issue agent"指令（含 issue_number 必填） |
| **L1** | **issue_opened → issue_state** | 建 stage=vod, owner=vod 行（D8） |
| **L2** | **VOD A2A 交办 issue** | issue_state.owner→issue；event_archive.a2a_to=issue |
| **L3** | **stage=issue 时 comment** | 事件交给 issue agent（非 VOD） |
| **L4** | **stage=coding 时 issue_updated** | 交给 coding agent |
| **L5** | **qa 发现问题 A2A 回 coding** | owner 回退 coding |
| **L6** | **done** | 后续事件不再分发给 agent |
| **L7** | **新 Issue 无记录** | 回退 get_domain_agent（现状兼容） |
| **L8** | **VOD 技能含双来源+引导+交办+生命周期** | VD-1~14 全过 |
| **L9** | **全量回归** | 零回归 |

---

## 六、实施顺序

| 步骤 | 内容 | 验证 |
|------|------|------|
| 1 | `issue_state` 表 + `session_store` get/set 函数 | test_issue_state |
| 2 | `event_pipeline.run_pipeline` 阶段路由改造（issue_opened→vod；按 owner_agent 分发） | test_issue_lifecycle_routing |
| 3 | `webhook.a2a_endpoint` 交办写 issue_state + `event_archive.a2a_to` | L2/L5 |
| 4 | `configs/agent-description.json` 订阅调整（vod+issue_opened；issue_updated/comment 多方订阅） | 路由回归 |
| 5 | VOD 技能文件重写 + references + skill-description.json | VD-1~14 |
| 6 | `_build_vod_prompt` 注入（知识基线 + build_issue_context + 当前阶段） | 手工 |
| 7 | 测试（issue_state/routing/vod_skill）+ 全量回归 | L1~9 / 全量 pytest |

---

## 七、后续演进（本期不做）

- 生命周期可视化：pipeline 页展示 issue_state 阶段（可复用 AGENT_ORDER 聚合）
- 引导轮次/反馈画像入库（复用 events/event.json）
- 阶段回退次数上限提示/告警
- 生命周期规则配置化（如某仓库跳过某阶段）

---

## 八、风险与兜底

| 风险 | 兜底 |
|------|------|
| codegraph 不可用 | 降级 README + API 基础信息（标注 degraded） |
| 引导无限追问 | 3 轮上限 + 超限建待澄清 Issue |
| 重复建 Issue | 历史全貌检查（近期 Issue 列表 + VOD 反馈画像） |
| 与 issue-advance 知识基线冲突 | 共用同一份 project-info + 增量更新策略（commit/7 天） |
| **阶段判定与 event.json 双源** | issue_state 为唯一权威；event_archive.a2a_to 仅记录（不回读判定） |
| **`issue_comment` 无 issue 编号** | 回退 get_domain_agent 或 vod（现状兼容） |
| **状态机回退死循环** | 回退上限（如 coding↔qa ≤2 次）记录到 issue_state |
| **VOD/issue 双订阅 issue_comment** | 由 issue_state 明确归属，消除歧义 |
| **审视自建循环** | D7 自建排除（auto-submitted 标记跳过） |
| **澄清失联** | ≤3 轮 + 超限列缺失项后仍交办 Issue Agent（不无限阻塞） |
| 技能/代码改动影响现网 | 全量回归 + 灰度（execution_mode direct 先验证） |
