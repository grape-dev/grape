# Agent 启用开关（enable）设计方案

> 版本：v1.0
> 日期：2026-08-08
> 状态：待评审
> 需求：① `devops_agent_session` 增加 `enable` 字段（仓库级启用/停用）；② `agent-description.json` 每 Agent 增加 `enable` 默认值（sre=false，其余 true）；③ 前端 Dashboard 每个 Agent 卡片增加开关，用户可自行开启/关闭。

---

## 一、需求梳理

| # | 需求 | 落点 |
|---|------|------|
| 1 | `devops_agent_session` 增加 `enable`，表示是否启用该 Agent；用户可在仓库 Dashboard 开启/关闭 | 仓库级实例开关 |
| 2 | `agent-description.json` 每 Agent 增加 `enable` 属性，默认 `true`；**仅 sre 默认 `false`** | 全局默认（agent_type） |
| 3 | 前端 Dashboard 每 Agent 卡片增加开关选项 | 用户交互 |

### 语义澄清

- **双层 enable**：
  - `agent_type.enable`：**全局默认**（来自 JSON seed，sre 默认关）。
  - `devops_agent_session.enable`：**仓库级实际开关**（用户 dashboard 调整后为准，覆盖全局默认）。
- **读取优先级**：事件路由/执行时读 `devops_agent_session.enable`（仓库级）；若该 repo 的 agent 会话尚未创建，则回退读 `agent_type.enable`。
- **关闭效果**：该 Agent 不再被事件路由触发（`run_pipeline` 跳过 + `event_skipped` 记录 `agent_disabled`）；A2A 调用该 Agent 也被拒绝。已排队的任务不受影响（由执行侧完成）。

---

## 二、数据模型变更

### 2.1 `agent_type` 增加 `enable` 列（全局默认）

```sql
ALTER TABLE agent_type ADD COLUMN enable INTEGER DEFAULT 1;   -- 1=启用 0=停用（默认开）
```

- 由 `agent-description.json` 每 Agent 的 `enable` 字段 seed（`INSERT OR REPLACE` 幂等覆盖）。
- sre 默认 `0`，其余 `1`。

### 2.2 `devops_agent_session` 增加 `enable` 列（仓库级开关）

```sql
ALTER TABLE devops_agent_session ADD COLUMN enable INTEGER DEFAULT 1;   -- 1=启用 0=停用
```

- 创建会话时继承 `agent_type.enable`（见 §4.2）。
- 用户 Dashboard 开关写本列。

### 2.3 `agent-description.json` 变更

```json
{
  "vod":   { "name": "VOD Agent",   ..., "enable": true,  ... },
  "issue": { "name": "Issue Agent", ..., "enable": true,  ... },
  "coding":{ "name": "Coding Agent",..., "enable": true,  ... },
  "qa":    { "name": "QA Agent",    ..., "enable": true,  ... },
  "sre":   { "name": "SRE Agent",   ..., "enable": false, ... }
}
```

> 迁移兼容：旧 JSON 无 `enable` 字段时按默认 `true` 处理（`info.get("enable", True)`）。

---

## 三、DB 迁移（`db_setup._migrate_tables()` 幂等区新增）

```python
# ===== new-version2：agent 启用开关 enable =====
try:
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "agent_type", "enable", "INTEGER DEFAULT 1")
        _ensure_column(c, "devops_agent_session", "enable", "INTEGER DEFAULT 1")
        # 存量数据：默认全部启用（sre 默认关由 seed 覆盖 agent_type；session 继承见 §4.2）
        db.commit()
except Exception as e:
    log.warning(f"Add enable column failed: {e}")
```

> 存量会话：`devops_agent_session.enable` 默认 1（保持现状不误关）。仅新建会话时按 `agent_type.enable` 继承（sre 新会话默认关）。

---

## 四、代码改动清单

### 4.1 seed（`db_setup._insert_seed_data` agent_type 分支）

```python
enable = 1 if info.get("enable", True) else 0
c.execute(
    "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, default_runtime_id, icon, order_num, subscribe_events, enable) VALUES (?,?,?,?,?,?,?,?)",
    (agent_type, display_name, description, default_runtime_id, icon, order_num, subscribe_events, enable)
)
```

### 4.2 会话创建继承（`session_store.py`）

- `get_or_create_agent_session`：INSERT 增加 `enable`，值取自 `agent_type.enable`。
- `ensure_default_agent_sessions`：INSERT OR REPLACE 同样带 `enable`。
- `agents.py update_agent_config`：INSERT OR REPLACE 带 `enable`（body 可选传，默认取 agent_type）。

### 4.3 路由拦截（`event_pipeline.run_pipeline`）

路由到 `agent_type` 后、入队/执行前，读 `devops_agent_session.enable`：

```python
# 路由命中后
_enabled = _is_agent_enabled(repo_key, agent_type)   # 仓库级 enable；无会话回退 agent_type.enable
if not _enabled:
    append_skipped_event(repo_key, event_type, "agent_disabled", agent_type)
    return result
```

- 同样需覆盖 `webhook.py` A2A 路径（`a2a_endpoint` 调 `_execute_agent` 前检查）。

### 4.4 后端 API（`agents.py` 或 `dashboard.py`）

新增开关端点：

```python
@router.put("/{name}/{owner}/{repo}/enable")          # 或复用 PUT /api/agents/{name}/{owner}/{repo} body.enable
async def set_agent_enable(name, owner, repo, body: {"enable": bool}, user):
    # 写入 devops_agent_session.enable（无会话则先 get_or_create 再写）
    return {"enabled": body.enable}
```

> 建议：扩展现有 `PUT /api/agents/{name}/{owner}/{repo}` 的 body 增加 `enable` 字段（复用 update_agent_config 的 INSERT OR REPLACE 路径），不新增端点。前端 Dashboard 开关调用同一接口。

### 4.5 Dashboard 后端（`dashboard.py get_dashboard`）

- `agent_sessions` 查询增加 `s.enable` 字段返回。
- 补全缺失 agent 的占位行时带 `enable`（从 agent_type 读默认）。

### 4.6 前端（`dashboard.js`）

- 每个 Agent 卡片头部增加开关（toggle）：
  ```
  ┌─ ● Issue Agent ──[✓ 开启]─────────┐
  │  最近事件 / 状态 / 统计           │
  └───────────────────────────────────┘
  ```
- 关闭态：卡片置灰（`opacity`）+ 显示"已停用"徽标。
- 点击开关 → `PUT /api/agents/{name}/{owner}/{repo}` body `{"enable": bool}` → 刷新。
- 权限：仅 Maintainer 可切换（前端按 `data.role` 控制，后端依赖既有权限校验）。

### 4.7 `_get_agent_type_meta`（`agents.py`）

- 返回体增加 `enable`（读 agent_type.enable），供前端 Agent 详情页展示全局默认。

---

## 五、拦截语义细化

| 场景 | 行为 |
|------|------|
| 事件路由到 `sre`（全局默认关，未建会话） | 跳过，`event_skipped(agent_disabled)` |
| 事件路由到 `sre`（仓库已开启） | 正常执行（仓库级 enable=1 覆盖全局） |
| 用户关闭 `issue`（仓库级 enable=0） | 该 repo 的 issue 事件跳过 |
| A2A 调 `qa`（仓库级 enable=0） | 拒绝/跳过，返回 403 或空 |
| 关闭后历史排队任务 | 不取消，由执行侧照常完成（避免中途丢弃） |
| 事件无关联 session 且 agent_type 无记录 | 沿用现状（`no_route` 跳过） |

---

## 六、验收用例

| ID | 场景 | 预期 |
|---|---|---|
| EN-1 | seed 后 agent_type.enable | sre=0，vod/issue/coding/qa=1 |
| EN-2 | 新建仓库会话继承 | sre 会话 enable=0，其余=1 |
| EN-3 | 事件路由到关闭 agent | 跳过 + event_skipped(agent_disabled)，不入队 |
| EN-4 | Dashboard 开启 sre | 后续 sre 事件正常执行 |
| EN-5 | Dashboard 关闭 issue | 该 repo issue 事件跳过 |
| EN-6 | A2A 调已关闭 agent | 拒绝/跳过 |
| EN-7 | 前端卡片开关交互 | 切换即时生效 + 卡片置灰 |
| EN-8 | 迁移幂等 | 已有库升级不报错，存量 enable=1 |
| EN-9 | 关闭不回滚执行 | 关闭前的排队任务照常完成 |

---

## 七、影响面与风险

| 项 | 说明 |
|----|------|
| 术语 | 无 backend/runtime 命名冲突；`enable` 独立 |
| 迁移 | `_ensure_column` 幂等，老库安全 |
| 兼容 | 旧 `agent-description.json` 无 enable → 默认 true；旧会话 enable=1 不误关 |
| 前端 | Dashboard 卡片 + toggle，需要 `?v=` 版本提升 |
| A2A | 需在 a2a_endpoint 同步拦截 |
| 并发 | 开关写入走 db_lock，无额外风险 |

---

## 八、里程碑归属

| 改动 | 里程碑 |
|------|--------|
| DB 迁移 + seed + 会话继承 | M4（前端配套）或独立小版本 |
| 路由/A2A 拦截 | M2 执行链路（新增逻辑） |
| Dashboard 后端 + 前端开关 | M4 |

> 建议作为独立小版本发布（非 M0-M4 阻断项），因涉及 DB 新列 + 前端交互，需完整回归。
