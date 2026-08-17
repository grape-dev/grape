# Chat Agent backend 选择解析修复方案（本机直连被解析成共享 worker）

> 状态：设计方案（v0.1，待评审）｜ 分支：new-version2
> 现象：用户 Chat Agent 配置为"本机直连 opencode"，但 Feishu chat 任务入队到远端共享 worker（`runtime=1(opencode-worker-123249)`，mode=queue_remote），且该 worker 并发繁忙时反复 claim。

## 1. 问题现象

- 2026-08-17 日志：`Task enqueued: ... runtime=1(opencode-worker-123249) skill=chat`、`enqueued task=... mode=queue_remote`。
- worker 对同一任务反复 claim（10:56:20 / 10:56:27，间隔≈poll 周期），直到 session 空闲才真正执行。
- 用户 Chat Agent 只能选"本机直连"与"个人 scope worker"，不该走到**共享全局 worker**。

## 2. 根因：backend 名 → runtime_id 保存解析错位

### 2.1 Chat Agent 可选面（`chat.py:656-687`）
| 类型 | name | 来源 |
|---|---|---|
| 本机直连 | 裸引擎名（如 `opencode`） | `agent_runtimes WHERE credential_id=0 AND installed=1` |
| 个人引擎 | 实例名（如 `opencode-worker-123249`） | `runtime_credentials.scope='user'` |

用户选"本机直连 opencode" → 前端保存 `backend="opencode"`。

### 2.2 保存解析（`session_manager.set_chat_config:660-669`）
```python
_rid = resolve_instance_id("opencode", scope="user")      # ① 个人实例名匹配
if _rid is not None and scope_of_runtime(_rid) == "user":
    runtime_id = _rid
else:
    runtime_id = get_runtime_id_by_name("opencode")        # ② 兜底 scope="global"
```

- **①** `resolve_instance_id("opencode", scope="user")`：个人实例名是 `opencode-{device}`，"opencode" 不是个人实例名 → 返回 None。
- **②** `get_runtime_id_by_name("opencode")`（`user/db.py:1613`，默认 `scope="global"`）：
  ```sql
  WHERE lower(name)='opencode' AND scope='global' AND owner_id=''
  ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, last_seen_at DESC
  ```
  命中**共享全局 worker**（runtime 1）。本机直连（runtime 22）也被视为 global（`scope_of_runtime` 对 credential_id=0 历史行按 global 处理），但 `status='offline'`（credential_id=0 行无心跳）被排序到后面。

### 2.3 生产数据坐实
| runtime | name | scope | owner_id | credential_id | status |
|---|---|---|---|---|---|
| 1 | opencode | global | '' | 1（共享 worker） | **online** |
| 22 | opencode | global | '' | 0（本机直连） | **offline** |

→ `get_runtime_id_by_name("opencode")` 返回 **runtime 1**。用户选"本机直连"，保存的却是共享 worker。

## 3. 修复方案

### 3.1 默认 backend 物化（原则）
**前端默认展示的 backend，就是 chat_agent_session 应配置的 runtime，无需用户额外保存。**

- 前端默认显示 = 本机直连（`resolve_local_engine()` 派生，`get_chat_config` 读取时已能返回该默认）。
- 设计：用户**未显式选择**时，把默认 backend 的 runtime_id **幂等物化**进 `chat_agent_session`（repo_key='' 行），使：
  - "未配置"有明确落库状态 = 本机直连，不依赖读取时兜底；
  - 避免"无配置行"与"脏配置行（如 runtime_id=1）"并存的状态；
  - Feishu/web 读取一致（读到的就是默认本机直连）。
- 物化时机：`set_chat_config` 未被调用时，可在 `get_chat_config` 首次读取（或启动/首次访问）时把默认 runtime_id 写入（幂等：仅当行不存在或 runtime_id 无效时）。

### 方案 A（首选，最小）：`set_chat_config` 解析收紧
backend 为**裸引擎名**（不含 `-` 实例后缀）时，**优先 credential_id=0 的本机直连**：
1. 无 `-` 后缀 → `SELECT id FROM agent_runtimes WHERE lower(name)=? AND credential_id=0 AND installed=1 LIMIT 1`（本机直连优先）；
2. 有 `-` 后缀（实例名）→ `resolve_instance_id(backend, scope="user")`（个人 worker）；
3. 以上皆无 → 回落现有 `get_runtime_id_by_name` / `ensure_default_backend`。

> 注意：裸引擎名如 `opencode` 可能同时存在本机直连（22）与共享 worker（1），必须显式优先 `credential_id=0`。

### 方案 B（更稳，推荐一并做）：前端保存直接带 runtime_id
`saveChatConfig` 携带所选 option 的 **runtime_id**（`_backends` 每项附 `runtime_id`），后端按 id 落库，**不做名解析**。从根上消除"名冲突/同名多实例"歧义。兼容：旧配置仍可按名字解析（用方案 A 规则）。

### 方案 C（数据清理）：脏配置回落本机直连
把 chat 配置中 `runtime_id` 指向 **scope=global 且 credential_id>0（共享 worker）** 的用户（如 user44）回落为本机直连（credential_id=0 对应行）。

## 4. 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| E01 | 裸引擎名保存到本机直连 | 存在同名 本机直连(22) + 共享worker(1) | `set_chat_config(backend="opencode")` → runtime_id=22（credential_id=0） |
| E02 | 实例名保存到个人 worker | 有 scope=user 的实例 | `set_chat_config(backend="opencode-xxx")` → 该实例 id |
| E03 | 未知名回落现有逻辑 | 无匹配 | 走 `ensure_default_backend`/空（不崩溃） |
| E04 | 本机直连 chat 任务走 direct | 配置 runtime_id=22 | `runtime_execution_kind(22)==EXEC_MODE_DIRECT`，不入队远端 |
| E05 | 脏配置回落 | user44 runtime_id=1 | 回落为本机直连后 chat 不再 claim 远端 |
| E06 | 未配置用户自动物化默认 | 无 chat_agent_session 行 | 读取/首次访问后行存在且 runtime_id=本机直连（无需 Save） |

## 5. 实施步骤

1. **默认物化**：`get_chat_config` 首次读取时将默认本机直连 runtime_id 幂等写入（§3.1）。
2. `session_manager.set_chat_config`：按方案 A 规则重写 backend→runtime_id 解析。
3. `chat.py get_chat_config` `_backends` 每项附 `runtime_id`；`chat.js saveChatConfig` 发送 `runtime_id`；后端优先采用（方案 B）。
4. 数据清理：对 `runtime_id` 指向共享 global worker 的 chat 配置回落本机直连（方案 C，可脚本化）。
5. 测试 E01-E06 + 回归 → 部署。

## 6. 结论

根因是 Chat Agent backend 名 → runtime_id 的保存解析把"本机直连"（裸名）解析成了同名的共享全局 worker。修复以"默认 backend 物化 + 裸名优先本机直连 + 前端直接传 runtime_id"落地：未配置即默认本机直连且落库，杜绝同类错位。

## 7. 关联清理：`chat_agent_session.runtime` 字符串列为死列（随本方案一并处理）

实施本方案时顺带确认 `chat_agent_session` 的 runtime 列：

| 列 | 现状 | 结论 |
|---|---|---|
| `runtime`（字符串，原 backend） | `update_session(sid, history, status, oc_session_id, backend)` 的 `backend` 参数**已被忽略、不再写入**；`get_or_create_session` **不读取**；引擎决策一律走 `runtime_id`；仅 `/clean` 重置与迁移 DDL 触碰 | **死列，可清理/移除** |
| `runtime_session_id` | `update_session` 写 / `get_or_create_session` 读 → **opencode 会话延续**（Feishu 续上下文） | **保留** |
| `runtime_id` | 引用 `agent_runtimes.id`，引擎选择权威列 | **保留**（有此列后 `runtime` 字符串列冗余） |

> 处理：将 `runtime` 字符串列标记为可清理（不重建表，仅文档标注 + 后续可选迁移移除）；`runtime` 字符串列其余引用属 `events.runtime`（另一张表），与本表无关。随本方案一起落库为：只维护 `runtime_id`，不再维护 `runtime` 字符串列。
