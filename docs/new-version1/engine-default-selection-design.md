# 仓库托管默认引擎选择策略设计（engine-selection）

> 版本：v2.0（定案：每 (仓库, agent) 引擎集合模型，替代"单引擎 / 引擎池"二分）
> 日期：2026-08-15
> 状态：待评审
> 需求：① 仓库托管时，devops agent 绑定的默认引擎**必须是 `agent_runtimes` 中已注册的引擎**，废除硬编码默认引擎 `opencode`，消除不确定性；② 引擎选择策略开放配置文件（`configs/engine-selection.json`）支持可配置；③ **每个 (仓库, agent) 绑定一组引擎（引擎集合）**：仓库 A 的 vod agent 可同时选全局 engine1 与仓库级 engine2，issue agent 可只选 engine2；④ 集合选择策略 **专属优先、全局兜底、本机最后兜底**；⑤ **本机直连（local）可进集合**，作为最低优先级成员，选中即 direct；⑥ 无候选时仍建会话但 `runtime_id` 留空，对应 agent 框图标识"无 engine，不可用"；`enabled` 与是否设置 engine 无关，绝不回落任何硬编码默认；⑦ 执行通道收敛为 `direct`（本机）｜ `queue_remote`（远端），废除 `queue_local`；⑧ 本文档只做设计，不涉及代码改动。

---

## 一、需求梳理

| # | 需求 | 落点 |
|---|------|------|
| 1 | 托管时默认引擎**必须是已注册引擎**（`agent_runtimes` 有行），不允许任何隐式默认 | `ensure_default_agent_sessions` 及所有硬编码 opencode 兜底点 |
| 2 | 默认引擎选择策略开放配置文件，未来支持可配置 | 新增 `configs/engine-selection.json`（+ `.example.json`） |
| 3 | **每 (仓库, agent) 支持多 engine（引擎集合）**；可选面 = {该仓库仓库级引擎} ∪ {全局引擎} ∪ {本机直连}，排除 user 级 | 新增绑定表 `devops_agent_runtime_binding`（§4.4） |
| 4 | 集合选择策略：**专属优先（仓库级）→ 全局兜底 → 本机最后兜底**；候选内按配置规则选一个 | 新增派发选择函数（§6） |
| 5 | **本机直连可进集合**（最低优先级成员）；devops / chat agent 默认兜底都落到本机直连 | 集合成员建模（§4.3/§4.4） |
| 6 | 无任何候选引擎时**不绑定默认**：仍建会话但 `runtime_id` 留空，对应 agent 标识"无 engine，不可用"（绝不放飞） | 派发选择 + 会话/前端标识 |
| 7 | 执行通道：`direct`（本机，`credential_id=0`）｜ `queue_remote`（远端，`credential_id>0`）；废除 `queue_local` | `runtime_execution_kind` 收敛（§9） |

### 语义澄清

- **"已注册"定义**：`agent_runtimes` 表中存在对应行（worker 通过 `POST /api/runtime/register` 写入，`src/api/routes/runtime.py:100-150`）。
- **"引擎集合" = 绑定**：每 (repo_key, agent_type) 一组有序引擎（可 0..N），即"devops agent 支持多个 engine"；单引擎只是规模为 1 的特例。**不再有独立的"单引擎模式"与"引擎池"两个概念**（§9）。
- **仓库可选面**：仓库 R 的 devops agent 只能选 {仓库 R 的仓库级引擎（owner_id=R）} ∪ {全局引擎（含本机直连，本机默认视为全局）}；user 级排除（`agents.py:352-354` 同口径）。
- **执行通道**：按选中引擎宿主推导——本机（credential_id=0）→ `direct`；远端（credential_id>0）→ `queue_remote`。`queue_local` 已废除。

---

## 二、现状与问题

### 2.1 托管绑定链路（现状，全部硬编码）

托管入口：`host_repo` / `join_repo` / `create_new_repo`（`src/api/routes/repos.py:151,216,231`）→ `ensure_default_agent_sessions(repo_key)`（`src/api/session_store.py:265`）：

```python
_default_runtime_id = ensure_default_backend("opencode") or 0   # session_store.py:287
```

`"opencode"` 是**写死的字符串**，5 个 agent（vod/issue/coding/qa/sre）共用。随后 `ensure_default_backend` → `ensure_default_runtime("opencode","global")`（`src/user/db.py:1660`）。

### 2.2 全部硬编码默认点（改造范围）

| 位置 | 代码 | 说明 |
|------|------|------|
| `src/api/session_store.py:287` | `ensure_default_backend("opencode")` | **主改造点**：托管建 5 个 agent 会话的默认 runtime |
| `src/api/session_store.py:103,132-133` | `get_or_create_agent_session(..., backend="opencode")` → `ensure_default_backend(backend)` | 事件执行兜底建会话 |
| `src/user/db.py:1630-1632` | `ensure_default_backend(name="opencode")` | 兼容别名 |
| `src/user/db.py:1660-1684` | `ensure_default_runtime(name="opencode", ...)`；`db.py:1681` **硬编码 `installed=1, healthy=1`** | 会为"未安装引擎"建假健康占位行 |
| `src/user/db.py:1613` | `get_runtime_id_by_name(name, scope="global", owner_id="")` | 默认只看全局、owner 为空 |
| `src/api/event_pipeline.py:363,393` | `backend = "opencode"` | 会话缺失时的兜底 backend |
| `src/api/event_pipeline.py:599` | `runtime_id = ensure_default_runtime("opencode")` | 入队侧无 runtime 兜底 |
| `src/api/db_setup.py:613` | `info.get("backend", "opencode")` | agent_type 种子默认 runtime |
| `src/api/routes/agents.py:221` | `AgentConfig.backend: str = "opencode"` | 配置接口默认值 |
| `src/api/routes/agents.py:197` | `meta.get("default_backend", "opencode")` | 详情接口兜底展示 |
| `src/config.py:149-150` | `get_agent_backend() -> "opencode"` | 全局兜底函数 |
| `configs/agent-description.json` | 5 个 agent 均 `"backend": "opencode"` | 种子配置 |

> `src/agent/executor.py:23` 的 `get_backend(name)` 兜底 `_BACKENDS["opencode"]` 属**引擎注册表内部实现兜底**（代码层 fallback），保留；本文档针对的是"绑定默认引擎"这一业务选择。

### 2.3 已确认的确定性风险（本次设计必须消除）

1. **未安装引擎也被当作"健康默认"绑定**：`is_known_engine_name`（`src/user/db.py:1635`）只看 `discover_backends()` 的名字白名单（`src/agent/executor.py:26` 恒返回全部 4 个名字），**不看 `installed`**；`ensure_default_runtime` 建行时硬编码 `installed=1, healthy=1`。
2. **后果**：服务器没装 opencode 时照样绑定 → 每个事件 direct 执行 `stream_agent`（`src/agent/executor.py:157-164`）`shutil.which` 找不到 → `exit_code=-1, errors=["opencode not found"]` → 事件 failed；queue 模式则任务长期卡 queued。
3. 托管、执行兜底、入队兜底、种子四处各自为政，改一处不漏其它。

---

## 三、目标设计总览

```
托管仓库（host/join/create）
   └─ ensure_default_agent_sessions(repo_key)
        └─ 为每个 devops agent 建"引擎集合"（写入绑定表）   ← 新增
             集合成员可选面：仓库级 ∪ 全局（含本机直连，最低优先级）
             （托管默认：按策略把选中引擎写入集合；未选则集合为空）

事件分发（每任务）
   └─ engine_select.pick_engine(repo_key, agent_type)      ← 新增统一派发选择
        ├─ 从该 (repo, agent) 的引擎集合，按优先级取候选：
        │    1) 仓库级引擎（专属优先）
        │    2) 全局引擎（兜底）
        │    3) 本机直连 local（最后兜底，credential_id=0）
        ├─ 候选内按 rule 选健康引擎（order / random）
        ├─ 选中 remote → enqueue_task(runtime_id=该引擎) → queue_remote claim
        ├─ 选中本机   → _execute_agent direct 就地执行
        └─ 集合空 / 无健康成员 → event_skipped(engine_unavailable)，agent 标识"无 engine，不可用"
```

**不可变原则**：`pick_engine` 返回的 `runtime_id` 必须对应 `agent_runtimes` 中真实存在的行；查不到候选就返回 `None`，调用方禁止自行回落任意字符串名。

---

## 四、数据模型与关键字段

### 4.1 `agent_runtimes`（引擎实例，已注册事实源）

| 字段 | 语义 | 与本设计关系 |
|------|------|--------------|
| `id` | 引擎行主键 | 集合成员；派发选中后写入 `agent_tasks.runtime_id` |
| `name` | 引擎类型名（opencode/codearts/…） | 展示 |
| `credential_id` | 承载 worker 凭据；0=本机直连 | 本机/远端判别、健康判定 |
| `scope` | global / repo / user（随凭据 scope，`runtime.py:118,143`）；本机直连以 `credential_id=0` 判别（§4.3） | 集合成员主过滤键 |
| `owner_id` | repo 时=仓库标识；user 时=user_accounts.id；**现状 repo 行缺失**（见 §7.1） | 仓库级归属过滤 |
| `status` | online / offline | `only_online` 过滤 |
| `healthy` / `installed` | 引擎健康/已装 | `only_healthy` / `only_installed` 过滤 |
| `last_seen_at` | 心跳时间 | 排序参考 |

### 4.2 `runtime_credentials`（worker 凭据，scope 与归属的权威来源）

| 字段 | 语义 |
|------|------|
| `id` | 凭据主键（= worker 身份） |
| `scope` | global / repo / user（签发时写入：管理台仅 global，`admin RuntimeCredentialService.SUPPORTED_SCOPES`；用户端 `POST /api/settings/runtime-credentials` 支持 repo，`settings.py:384-404`） |
| `owner_id` | repo 时 = 仓库全名 `owner/repo`（签发时写入，`settings.py:395-404`） |
| `owner_name` | 签发人/worker 名 |

> **归属判定一律走凭据维度**（`ar.credential_id = rc.id AND rc.scope='repo' AND rc.owner_id=<repo_key>`），与可见性判断同口径（`settings.py:199-208`），规避 `agent_runtimes.owner_id` 缺失问题（§7.1）。

### 4.3 本机直连（local / direct）——可进集合

- **判别**：`credential_id=0`（零迁移，方案 B 定案，不新增 scope 枚举）。
- **定位**：服务端本机安装并注册的引擎，执行通道恒 `direct`（无 worker、无 claim）。
- **默认视为全局**：`scope_of_runtime` 将 `credential_id=0` 当 global（`instances.py:80-95`），对所有用户可见（`settings.py:200-201`），健康按 `installed+healthy` 派生（`settings.py:270-280`）。
- **可进集合（定案）**：本机直连是 (repo, agent) 引擎集合的**最低优先级成员**。派发时只有仓库级/全局均无健康成员时才选中本机 → `direct` 就地执行。
- 本机直连**不参与 claim**（无 claim 方），因此只作为**派发选择**的兜底成员，不进入 claim-based 调度（§9）。

### 4.4 新增绑定表 `devops_agent_runtime_binding`（引擎集合存储）

**选型（定案：绑定表，性能/可扩展性优于 JSON 列）**：
- 可索引、可查询（派发/展示/审计），无 JSON 解析成本；
- 可表达"仓库 A 的 vod = {engine1, engine2}，issue = {engine2}"的每 (仓库, agent) 粒度；
- 比 `agent_type_runtime_pool`（agent_type 级、跨仓库共享）更贴合"仓库专属"隔离。

```sql
CREATE TABLE IF NOT EXISTS devops_agent_runtime_binding (
    repo_key    TEXT NOT NULL,          -- 仓库全名（归一化）
    agent_type  TEXT NOT NULL,          -- vod / issue / coding / qa / sre / chat
    runtime_id  INTEGER NOT NULL,       -- agent_runtimes.id
    priority    INTEGER NOT NULL DEFAULT 50,  -- 小=高优先；默认按 scope 派生：repo=10 / global=50 / local=90，可覆盖
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (repo_key, agent_type, runtime_id)
);
CREATE INDEX IF NOT EXISTS idx_binding_repo_agent ON devops_agent_runtime_binding(repo_key, agent_type);
```

- 迁移：现有 `agent_type_runtime_pool`（agent_type 级池）→ 迁移到本表（补 repo 维度）；`devops_agent_session.runtime_id` 保留为"当前/主引擎"（派发结果与展示），不再是唯一绑定来源。
- 会话与绑定的一致性：`ensure_default_agent_sessions` 建会话时同步建默认集合；Configure 的 `backends` 多选写本表。

---

## 五、配置文件设计

### 5.1 `configs/engine-selection.json`（正式配置，可提交）

```jsonc
{
  "enabled": true,                       // 自动选择开关；false = 不自动绑定默认引擎（runtime_id 留空、
                                         // agent 标识"无 engine，不可用"）；与是否设置 engine 无关，
                                         // 绝不回落任何硬编码默认引擎
  "scopes": ["repo", "global", "local"],  // 优先级顺序（专属优先→全局兜底→本机最后兜底）；已排除 user
                                          // local = 本机直连（credential_id=0 的已注册引擎，恒 direct）
  "rule": "order",                       // random | order（order = 按 priority 升序，同 priority 按 runtime id 升序）
  "filters": {
    "only_online": true,                 // 仅 status='online'（本机按 installed+healthy 派生）
    "only_healthy": true,                // 仅 healthy=1
    "only_installed": true               // 仅 installed=1
  },
  "local_priority": 90                   // 本机直连成员的默认 priority（低于仓库/全局，作最后兜底）
}
```

### 5.2 `configs/engine-selection.example.json`（示例 + 注释，可提交）

- 同字段，附注释说明各取值含义（与 `server-config.example.json` 的惯例一致）。

### 5.3 读取方式

- 新增加载函数（建议放 `src/runtime/engine_select.py`，随用随读、缺失/损坏回默认，参考 `read_server_config` 风格，`src/config.py:161-173`）。
- 默认值 = 上表字段值；文件缺失时按默认策略执行（**默认也是"必选已注册引擎"**，不回落 opencode）。
- 配置与代码默认保持同一语义：默认即 `scopes=["repo","global","local"], rule="order", filters 全开`；无 `enabled` 字段时视为自动选择开启。

---

## 六、选择 / 派发算法（伪代码）

```python
def pick_engine(repo_key: str, agent_type: str) -> int | None:
    """从 (repo, agent) 的引擎集合中，按 专属→全局→本机 优先级选健康引擎；无候选返回 None。"""
    cfg = load_engine_selection()                     # §5.3
    if not cfg.get("enabled", True):
        return None                                   # 不自动选择：跳过执行；绝不回落任何默认引擎
    repo_key = normalize_repo_key(repo_key)           # 统一 to_repo_key/from_repo_key（§7.2）
    for scope in cfg["scopes"]:
        cand = binding_members(repo_key, agent_type, scope)   # 绑定表按 scope 分组（§4.4）
        cand = apply_filters(cand, cfg["filters"])            # only_online/healthy/installed
        if not cand:
            continue
        if cfg.get("rule") == "order":
            return min(cand, key=lambda r: (r["priority"], r["id"]))   # 专属优先，同档按 id 升序
        return random.choice(cand)                                 # 随机（同优先级内）
    return None
```

- **绑定成员按 scope 取**：
  - repo：`binding.runtime_id → agent_runtimes`，凭据 scope='repo' 且 `owner_id = repo_key`；
  - global：凭据 scope='global' 且 `credential_id>0`（远端全局 worker）；
  - local：`credential_id=0` 且 `installed=1`（本机直连，最后兜底）。
- **选中后派发**：
  - 选中远端（credential_id>0）→ `enqueue_task(runtime_id=选中引擎)` → `queue_remote`，该引擎 claim（确定路由，等价"严格绑定到该引擎"）；
  - 选中本机（credential_id=0）→ `_execute_agent` 就地 `direct`；
  - 返回 None → `event_skipped(engine_unavailable)`，不回落硬编码 backend。

---

## 七、前置缺陷修复（设计阶段确认，实施时一并处理）

### 7.1 `agent_runtimes.owner_id` 仓库级缺失（必须修）

`runtime.py:119-128` 只对 `user` scope 回填 `owner_id`（user_accounts.id），**repo scope 时 `owner_id` 保持空串**；而凭据行 `runtime_credentials.owner_id` 在签发时已存仓库全名（`settings.py:395-404`）。

- **修复方案**：`runtime_register` 对 `cred_scope=="repo"` 也回填 `owner_id = 凭据行的 owner_id`（select 增加 `owner_id` 字段）。SELECT 键同字段（`runtime.py:133`）。
- **算法双保险**：即使历史行 owner_id 为空，§6 的"走凭据维度 join"仍能正确归属，不受影响。

### 7.2 repo_key 归一化

- 托管侧 `repo_key` 带平台前缀（`gitcode:owner/repo`，`event_pipeline.py:249-252`）；凭据 `owner_id` 是 `owner/repo`。
- 选择/绑定函数内部统一 `to_repo_key()` / `from_repo_key()`（`src/repo_key.py`）归一化后比较。

### 7.3 废除"假健康占位行"

- `ensure_default_runtime` 的建行逻辑（`db.py:1681` 硬编码 `installed=1, healthy=1`）在默认选择策略接管后**不应再被托管/入队兜底路径调用**；保留该函数仅作显式注册引擎的幂等确认（且 `installed/healthy` 以 `discover_backends().installed` / 注册信息为准）。
- `is_known_engine_name` 改为**"注册表中存在该引擎"或"发现结果 installed=True"**，而非仅名字白名单。

---

## 八、代码改动清单（实施期）

### 8.1 新增模块 `src/runtime/engine_select.py`

- `load_engine_selection()`：读 `configs/engine-selection.json`，缺失/异常回默认。
- `pick_engine(repo_key, agent_type) -> int | None`：§6 派发选择。
- `set_binding(repo_key, agent_type, runtime_ids, ...)` / `get_binding(repo_key, agent_type)`：绑定表读写（迁移 `set_agent_pool` 语义到 repo 维度）。
- `query_engines(scope, repo_key)`：凭据维度 join 查询（`agent_runtimes` ⋈ `runtime_credentials`）。

### 8.2 替换挂载点

| 位置 | 替换为 |
|------|--------|
| `session_store.py:287` | `ensure_default_agent_sessions` 建集合（绑定表）而非写 `runtime_id`：按策略把选中引擎写入 `devops_agent_runtime_binding`；集合空则 `runtime_id` 留空 |
| `session_store.py:132-133` | `get_or_create_agent_session` 的 backend 解析改为 `pick_engine(repo_key, agent_type)`（保留既有集合/主引擎不复查） |
| `event_pipeline.py:495-504` | 分发改为 `pick_engine`：选中 remote → `enqueue_task(runtime_id=选中引擎)`；选中 local → `_execute_agent` direct；None → 跳过（`engine_unavailable`） |
| `event_pipeline.py:363,393,599` | 去掉 `"opencode"` 兜底与 `ensure_default_runtime("opencode")`，改为 `pick_engine`；仍 None 则跳过 |
| `src/runtime/service.py:430-462` | `claim` 的"池分支"（`runtime_id=0 + pool_agent_type`）随 claim-based 池弃用而移除，仅保留确定路由（§9） |
| `db_setup.py:613` | agent_type 种子 `default_runtime_id` 置 NULL 由运行时解析（或按策略写入默认集合），不再写 opencode |
| `agents.py:197,221` | 详情/配置接口默认 backend 移除 `"opencode"` 字面默认；`backends` 读写落到绑定表（repo 维度） |
| `config.py:149-150` | `get_agent_backend` 改为转发 `pick_engine` 或标注废弃 |
| chat 默认绑定（`src/api/routes/chat.py` / `resolve_instance_id(scope='user')`） | chat agent 默认兜底落到本机直连（local）；个人引擎仅用户显式选择时使用 |

### 8.3 配置样例与文档

- 新增 `configs/engine-selection.json` + `configs/engine-selection.example.json`。
- 迁移说明：`agent_type_runtime_pool` → `devops_agent_runtime_binding`（repo 维度）。

### 8.4 前端

- **Configure 引擎多选**：`data.backends` 展示/保存落到该 (repo, agent) 的绑定表（`agents.js` 已用 `backends` 数组，仅数据源换）；下拉按仓库可选面分组：仓库级 / 全局 / 本机直连。
- **Dashboard "无 engine，不可用"标识**：agent 卡片在绑定集合为空或派发 None 时置灰显示（§10）。

---

## 九、与既有机制的边界

### 9.1 "单引擎 vs 引擎池" → 统一为"引擎集合"（定案）

**不再保留"单引擎模式"与"引擎池"两个概念**：绑定 = 每 (repo, agent) 一个引擎集合（0..N），单引擎 = 规模 1 的特例。派发时从集合按"专属→全局→本机"优先级 + 健康选一个引擎（§6）。

| 原概念 | 现模型 |
|--------|--------|
| 严格单引擎（`devops_agent_session.runtime_id>0`） | 集合规模 1；派发必选该引擎 |
| 引擎池（`agent_type_runtime_pool`，agent_type 级） | 集合规模 N；**且带 repo 维度**（`devops_agent_runtime_binding`），仓库隔离天然成立 |
| claim-based 池调度（`runtime_id=0 + pool_agent_type`） | **弃用**：改派发时确定选中引擎（`runtime_id>0` 入队），claim 仅确定路由 |

**选型依据（性能/可扩展性）**：派发选择（dispatch-time selection）优于 claim 抢单池——
- 优先级（专属优先）在 claim 抢单下难以强制（仓库级与全局引擎同时轮询拉任务，谁先抢到是谁）；派发选择确定性执行优先级；
- 本机直连无 claim 方，claim 模型无法包含本机成员；派发选择可把本机作为最后兜底；
- 无"auto 下 `runtime_id=0` 判 direct、池失效"的既有缺陷（§9 原记录），因为派发已确定引擎、`runtime_id` 恒 >0；
- claim 侧只保留 `runtime_id=?` 确定路由分支，逻辑更简。

### 9.2 本机直连可进集合（最低优先级）

- 本机直连（credential_id=0）是集合成员，`priority` 默认最低（`local_priority=90`），仅在仓库级/全局均无健康成员时被选中 → `direct`。
- 本机直连**不参与 claim**（无 claim 方）；因此只在**派发选择**语义下作为兜底成员，不进入 claim 调度。
- 通道不变式保持：`credential_id=0` → 仅 `direct`；`credential_id>0` → 仅 `queue_remote`。

### 9.3 废除 `queue_local`（保留结论）

**现状证据**：`start_local_worker_loop()` 是空函数（`service.py:756-758`）；`submit_local` 无生产调用方（`service.py:761`）；`claim` 生产消费仅远端 RuntimeAgent（`runtime.py:175`）；auto 从不产出 `queue_local`。强制选择即卡 queued。

- **决策**：执行通道收敛为 `auto / direct / queue_remote`（`auto` 默认，推导 `direct`｜`queue_remote`）。
- **清理**：`EXEC_MODE_LOCAL`、`runtime_execution_kind` 强制值分支、`settings.py:489-490` 白名单、死代码 `start_local_worker_loop` / `submit_local` / `_run_task`（本地版）/ `_worker_pool`；相关文档同步。
- **前提补齐**：① direct 并发控制（`_execute_agent` 无界线程 → 有界并发，复用 `RUNTIME_MAX_CONCURRENT_TASKS`）；② 本机任务可观测性（决策点：看 events，或 direct 补写 `agent_tasks` 终态）。

---

## 十、边界与降级行为

| 场景 | 行为 |
|------|------|
| 集合含仓库级引擎（在线） | 专属优先，按 rule 选仓库级引擎 |
| 仓库级离线/无，含全局引擎 | 全局兜底，按 rule 选 |
| 仓库级/全局均无健康成员，含本机直连 | 本机最后兜底，`direct` 就地执行 |
| 集合为空 / 均不健康 | 事件跳过（`engine_unavailable`）；agent 标识"无 engine，不可用" |
| 全局执行模式取值 | `auto`（默认）\| `direct` \| `queue_remote`；`queue_local` 已废除（§9.3），设置即拒绝 |
| 配置 `enabled=false` | 不自动绑定默认引擎（集合留空 + 标识不可用）；与是否设置 engine 无关，绝不回落硬编码默认 |
| 选中引擎派发后离线 | 任务入队该引擎等待/被 `recover stale dispatched` 回收重排；或派发时只选健康成员降低概率 |
| worker 未上线但已注册 | 视为候选（"已注册"即满足硬约束；健康过滤由 `filters` 决定） |

---

## 十一、验收用例

| ID | 场景 | 预期 |
|----|------|------|
| ES-1 | 仓库 A 有仓库级 engine2 + 全局 engine1；vod 集合={engine1,engine2}，issue 集合={engine2} | 各自按集合派发：vod 专属优先选 engine2（engine2 在线）；issue 恒 engine2 |
| ES-2 | 仅全局 engine1；rule=order | 派发选 engine1（id 最小全局引擎） |
| ES-3 | 集合为空 | 事件跳过（`engine_unavailable`），agent 标识"无 engine，不可用"；托管成功 |
| ES-4 | 仓库级 engine2 offline、全局 engine1 online | 仓库级不健康 → 回落全局 engine1（专属优先、健康优先） |
| ES-5 | 仓库级/全局均无健康成员，本机直连已注册 | 回落本机，`direct` 就地执行 |
| ES-6 | 配置缺失/损坏 | 按默认策略（repo→global→local、order、filters 全开），不回落 opencode |
| ES-7 | 托管后事件触发 | `pick_engine` 返回已注册引擎；remote → `agent_tasks` 出现该 runtime_id 行；local → direct 找到二进制 |
| ES-8 | `enabled=false` | 不自动绑定（集合留空 + 标识不可用）；不回落旧硬编码行为 |
| ES-9 | repo 引擎 worker 注册（repo scope 凭据） | `agent_runtimes.owner_id` = 仓库标识（§7.1 修复生效） |
| ES-10 | 事件路由到集合为空的 agent | 跳过执行，`event_skipped(engine_unavailable)`，不回落硬编码 backend |
| ES-11 | Configure 勾选多引擎 | 写绑定表（repo 维度）：vod={engine1,engine2}；事件按优先级+健康派发 |
| ES-12 | Configure 勾选 1 个引擎 | 集合规模 1，派发必选该引擎（等价原严格绑定） |
| ES-13 | 本机直连进集合（无远端成员时） | 兜底选中本机，恒 `direct`；devops / chat agent 同 |
| ES-14 | 本机直连 + 远端引擎同集合 | 本机为最低优先级成员；远端健康时优先远端（queue_remote），远端不可用才本机（direct） |
| ES-15 | 全局模式设置为 `queue_local` | 校验拒绝（白名单仅 auto/direct/queue_remote） |
| ES-16 | direct 并发控制 | 本机直连并发不超过 `RUNTIME_MAX_CONCURRENT_TASKS` |
| ES-17 | `agent_type_runtime_pool` 存量数据 | 迁移到 `devops_agent_runtime_binding`（repo 维度），无孤儿任务 |

---

## 十二、影响面与风险

| 项 | 说明 |
|----|------|
| 行为变更 | **破坏性**：新库不再默认绑定 opencode；无引擎时仍建会话但集合留空、agent 标识"无 engine，不可用"。需要产品侧同步文案 |
| 数据 | 新增绑定表 `devops_agent_runtime_binding`；`agent_type_runtime_pool` 迁移；`agent_runtimes.owner_id` 补写（幂等） |
| 前端 | ① Configure 多选数据源改为绑定表，按仓库可选面分组（仓库级/全局/本机直连）；② Dashboard agent 卡片"无 engine，不可用"标识 |
| 执行模式 | 废除 `queue_local`（§9.3）；白名单收敛 auto/direct/queue_remote；死代码清理；direct 并发补齐 |
| claim | 移除 `runtime_id=0 + pool_agent_type` 池分支，仅保留确定路由；远端 RuntimeAgent 协议不变 |
| 兼容 | 存量已绑定会话迁移为单成员集合；`enabled=false` 不回落旧硬编码行为 |
| 回归 | 需覆盖 `test_runtime.py` / `test_runtime_protocol.py` / `test_runtime_auto_mode.py` / `test_m2_execution_mode.py` / `test_runtime_pool.py` / `test_onboarding.py` / `test_bot_manager_load.py`（`ensure_default_agent_sessions` 与池相关用例因模型变更调整） |
| 与 worker 注册 | repo 凭据签发（`settings.py:384`）与管理台 global 凭据（`RuntimeCredentialService`）不受影响 |

---

## 十三、里程碑归属

| 改动 | 建议 |
|------|------|
| §7.1 owner_id 补写 + §7.3 假健康占位修复 | 独立小版本（先行，低风险） |
| §9.3 废除 `queue_local`：模式收敛 + 死代码清理 + direct 并发控制 | 独立小版本（先行，低风险） |
| 绑定表 `devops_agent_runtime_binding` + `agent_type_runtime_pool` 迁移 | M1 |
| 新增 `engine_select.pick_engine` + 替换 `session_store.py:287` / `event_pipeline` 分发 / `claim` 池分支 | M1-M2 |
| 配置 `engine-selection.json` + 样例 + 验收 | M2 |
| Configure / Dashboard 前端（分组 + "无 engine，不可用"） | M3 |

---

## 附：相关文件速查

| 文件 | 用途 |
|------|------|
| `src/api/session_store.py` | 托管建会话 + 默认集合（主改造点） |
| `src/runtime/engine_select.py` | 新增：配置读取 / `pick_engine` / 绑定读写 |
| `src/runtime/service.py` | `claim` 池分支移除；direct 并发控制 |
| `src/api/event_pipeline.py` | 分发改 `pick_engine`；去掉 opencode 兜底 |
| `src/user/db.py` | ensure_default_* / get_runtime_id_by_name / is_known_engine_name |
| `src/api/routes/runtime.py` | worker 注册（§7.1 修复点） |
| `src/api/routes/settings.py` | 引擎列表可见性 / 凭据签发 / 执行模式白名单 |
| `src/api/routes/agents.py` | `backends` 读写落到绑定表 |
| `src/api/db_setup.py` | 绑定表 DDL / agent_type 种子 |
| `configs/agent-description.json` | 每 agent backend 字段（降级为展示） |
| `src/repo_key.py` | repo_key 归一化 |
| `src/api/routes/chat.py` | chat agent 默认绑定（本机直连兜底） |
| `src/api/static/js/pages/agents.js` | Configure 多选 → 绑定表 |
| `src/api/static/js/pages/runtimes.js` | Engines/Workers 视图分组（本机直连） |
