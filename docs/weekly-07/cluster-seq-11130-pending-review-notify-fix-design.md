# 集群 Seq#<N> pending-review 飞书通知缺失定位

> 状态：根因已定位（heartbeat SQLite 方言 UPSERT 在 MySQL 后端漏翻 → 1064 → 环失效 → bot lease 乒乓 → 通知 no_ctx）；**方案 A 已实施**（`translate_sql` 增加 ON CONFLICT→ON DUPLICATE KEY 翻译）；单测 + 全量回归零新增失败；端到端（集群 E-1~E-3）待运维验证（2026-10-13）｜环境：master 集群（DEPLOYMENT-CLUSTER.md），`grape.example.com`
> 现象：QA agent 执行日志确认 `grape issues pending-review --issue_id <ID>`（changed=true）成功；issue 已置 `pending_review`，但用户未收到飞书通知。

---

## 1. 事件与执行链路（已确认）

- **事件**：Seq#<N>，`a2a_event` / qa / `grape-skill-validator`，runtime=`<WORKER_ID>`（worker 节点执行），repo=`gitcode:<OWNER>/<REPO>`，issue=#N。
- **QA 动作**（执行日志 Step 28，18:22:21 CST）：
  ```
  grape issues pending-review --issue_id "<ID>" \
    --json '{"review_points":"PR #1 skill 合规检查全部通过...","references":"PR #1"}' --json-out
  ```
  返回 `changed=true` → 服务端路由 `POST /api/issues/{id}/pending-review` 置 `issue.status=pending_review`。
- **DB 确认**（MySQL `grape_system_db`）：
  - `issue id=<ID>`：repo_key=`gitcode:<OWNER>/<REPO>`，issue_number=3，`user_id=<UID>`，`assignee=''`，status=`pending_review`，conversation_id 已生成。
  - `user_accounts id=38`：username=`<OWNER>`，nickname=`<NICKNAME>`。
  - `unified_user_mapping user_id=<UID>`：`feishu_open_id=<OPEN_ID>`，`app_id=<APP_ID>`。
  - `chat_agent_session user_id=<UID>`：`im_provider=feishu`，`im_app_id=<APP_ID>`。

## 2. 通知代码路径（服务端）

路由 `src/api/routes/issues.py:945 issue_pending_review` → `_set_issue_pending_review`（成功）→ 进入通知分支：

```python
_open_id = _get_feishu_open_id_by_user_id(it["user_id"]) or _get_feishu_open_id(it["assignee"])
bm = api_main.bot_manager
_ctx = _resolve_user_chat_bot(bm, it["user_id"]) if bm else None   # ← 关键
_api = bm.api if bm else None
asyncio.create_task(_run_pending_review_notify(_ctx, _api, it["user_id"], conv_id, _open_id, ...))
```

`_run_pending_review_notify`（`src/api/issues_center.py:893`）发送条件：
```python
if open_id and api and ctx:   # 三者都满足才 send_card_to_user
    await api.send_card_to_user(...)
else:
    _im_notify_skip("pending_review", ...)   # reason=no_ctx / no_openid / no_api
```

`_resolve_user_chat_bot`（`issues_center.py:934`）：
- 从 `chat_agent_session` 查用户 `im_app_id`（38 → `<APP_ID>`）；
- 从 `bot_manager.get_all_bots()` **进程内存**中取该 bot；
- 取不到 → 打 WARNING `reason=bot_not_in_memory` 并返回 **None** → 通知 skip。

## 3. 根因（已定位）

**<OWNER> 的本人 IM bot（`<APP_ID>`）在 <MASTER_NODE> 主进程的 IM lease 持续 renew 失败 → grace 过期 disconnect → 不在 `get_all_bots()` 内存 → `_resolve_user_chat_bot` 返回 None → pending-review 通知以 `reason=no_ctx` 静默跳过。**

### 3.1 决定性证据（<MASTER_NODE> `/opt/grape/data/main.log`）

```
10:54 [lease] bot <APP_ID> renew failed x1/3
10:55 renew failed x3, grace period 90s started
10:57 grace period expired, disconnecting
11:04~11:47 循环：renew failed x1/3 → … → x3 grace → expiring → disconnecting
```

事件 11130 通知时刻（10:16-10:22 UTC）该 bot 处于 lease 失效/周期断开状态；同窗口无任何 `im-notify` INFO 记录（成功通知为 INFO，`--log-level warning` 本会丢弃；但 `_im_notify_skip` 是 WARNING 应留存——本窗口内亦未见 pending_review skip，说明跳过发生在 `_resolve_user_chat_bot` 打 WARNING 前或窗口日志滚动，机制上等价：`ctx=None → skip`）。

### 3.2 同款既有现象

- 10:47 / 11:08：`[im-notify] resolve user=X bot=<APP_ID> reason=bot_not_in_memory hint=sticky-lease` → `type=awaiting issue=#N user=X reason=no_ctx`（GTD 仓库同样收不到）。
- 集群 4 个 master 进程都可能有 lease（`grape_im_instance` 表），bot lease 归属不落在处理请求的进程即失配。

## 4. 根因层次

| 层 | 结论 |
|----|------|
| 业务层 | `pending-review` 状态设置、issue 数据、conversation、user 映射**全部正确**（不是 pending-review 逻辑缺陷）|
| 通知前置条件 | `open_id` 可解析（<OPEN_ID>…）；**卡在 `ctx`（bot 内存对象）缺失**|
| 基建层 | IM bot `<APP_ID>` sticky-lease **持续 renew 失败 → disconnect** → 不在主进程内存（`cluster-im-lease-stability-design.md` §3.x 场景）|
| 影响面 | 所有需要本人 chat bot 发送的双态通知（awaiting_input / pending_review / task notify）都受影响 |

## 5. 终极根因（定位完整）

### 5.1 决定性证据（<MASTER_NODE> `/opt/grape/data/main.log` 持续刷屏）

```
[lease] heartbeat error: (1064, "You have an error in your SQL syntax; ...
  near 'CONFLICT(instance_id) DO UPDATE SET last_heartbeat=CAST(NOW() AS CHAR), hostname' at line 1")
```

**每次 sweep（每 10s）都抛 MySQL 1064 语法错误**，`grape_im_instance` 表存在但**恒空**（心跳从未写入）。

### 5.2 代码根因：`bot_lease.heartbeat()` 用了 SQLite 方言 UPSERT

`src/im/bot_lease.py:230`
```sql
INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled)
VALUES (?,?,?,1)
ON CONFLICT(instance_id) DO UPDATE SET
  last_heartbeat=datetime('now'),
  hostname=COALESCE(excluded.hostname, hostname),
  ip=COALESCE(excluded.ip, ip), im_enabled=1
```

集群 `db-backend.json: backend=mysql` → 所有 SQL 经 `src/mysql_compat.translate_sql`（`executor.execute → t = translate_sql(sql)`）。但 `translate_sql`（mysql_compat.py:233-316）**不处理 `ON CONFLICT ... DO UPDATE SET ... excluded.x`**（SQLite/PostgreSQL 方言）→ MySQL 收到裸语句 → 1064 语法错误 → `except` 吞掉 → `grape_im_instance` 永远为空。

### 5.3 因果链（最终完整版）

```
MySQL 后端 + heartbeat() 用 SQLite 方言 ON CONFLICT/excluded
  → translate_sql 不翻译 → MySQL 1064 → grape_im_instance 恒空（心跳从未写入）
      ├─ list_active_instances() 恒空 → IM_LEASE_STICKY ring 恒空 → sticky 归属退化为“先到先得”
      ├─ is_owner_alive() 恒 False → 接管保护失效 → 多 master 无条件互抢同一 bot lease
      └─ 每次 sweep 报错刷日志
  → <MASTER_NODE_1> / <MASTER_NODE_2> 两实例争夺同一批 bot（lease_owner 互换）
      ├─ 持有方 batch_renew WHERE lease_owner=self 连败（owner 已被抢）→ renew failed x3
      └─ grace 90s → disconnect → 重新 acquire … 乒乓
  → 用户 bot（<APP_ID> 等）频繁不在 get_all_bots() 内存
  → pending-review / awaiting / task 通知 _resolve_user_chat_bot → None → no_ctx 跳过
```

> 单机部署（1 实例）未暴露：ring 空退化为“自我竞争”，无第二个实例抢；**集群多 master 才触发乒乓**。

## 6. 修复方案（推荐 A：translate_sql 透传 UPSERT 方言）

### 方案 A：`mysql_compat.translate_sql` 增加 `INSERT … ON CONFLICT … DO UPDATE SET … excluded.x → ON DUPLICATE KEY UPDATE … VALUES(x)` 翻译

保持 SQLite 侧行为不变（SQLite 仍执行原生 `ON CONFLICT`），仅在 MySQL 后端把方言翻译成 MySQL 语义。

```python
# src/mysql_compat.py translate_sql() 中，?→%s 替换之前/之后插入：
# 匹配: INSERT ... ON CONFLICT(<pk>) DO UPDATE SET col=expr(excluded.col), ...
_CONFLICT_UPSERT_RE = re.compile(
    r"(?is)\bON\s+CONFLICT\s*\(([`\"\w]+)\)\s+DO\s+UPDATE\s+SET\s+(.+?)(?=;|$)")
def _mysql_upsert_repl(m):
    _pk, _sets = m.group(1), m.group(2)
    # excluded.col → VALUES(col)（MySQL 8.0.20+ 用 VALUES() 兼容旧版；新写法 row alias）
    _sets = re.sub(r"\bexcluded\.([`\"\w]+)\b", r"VALUES(\1)", _sets)
    return f"ON DUPLICATE KEY UPDATE {_sets}"
sql = _CONFLICT_UPSERT_RE.sub(_mysql_upsert_repl, sql)
```

- 作用：`heartbeat()` 的 UPSERT 被翻译为
  ```sql
  ON DUPLICATE KEY UPDATE
    last_heartbeat=CAST(NOW() AS CHAR),
    hostname=COALESCE(VALUES(hostname), hostname),
    ip=COALESCE(VALUES(ip), ip), im_enabled=1
  ```
- `grape_im_instance` 恢复写入 heartbeat → ring 恢复 → 接管保护恢复 → 多 master 不互抢 → bot lease 稳定 → 通知正常送达。
- `joined_at` 首个 INSERT 保留（仅 ON DUPLICATE 时更新 heartbeat/hostname/ip/im_enabled）。

### 方案 B（不推荐单独用）：`heartbeat()` 改用 `INSERT OR REPLACE`

`translate_sql` 已把 `INSERT OR REPLACE` → `REPLACE INTO`（SQLite 到 MySQL 都通）。
- ⚠️ 缺点：`REPLACE INTO` 是 DELETE+INSERT，会重置 `joined_at`/`im_enabled` 语义、并可能在高并发心跳时造成短暂行消失；不如方案 A 精准。

### 兜底加强（选做）：通知在 bot 不在内存时显式告警/重试

`_resolve_user_chat_bot` 已打 `reason=bot_not_in_memory`；可再加：`_run_pending_review_notify` 在 `ctx=None` 时把通知写入可观测队列（或明确 `log.error`），避免静默丢失。

## 7. 测试用例设计

### 7.1 单元测试 A1：`translate_sql` 对 heartbeat UPSERT 的 MySQL 翻译

新增回归（`tests/test_mysql_compat.py` 或 `tests/test_cluster_im_lease.py`）：

| # | 输入（heartbeat UPSERT，SQLite 原生） | 断言（MySQL 翻译输出） |
|---|--------------------------------------|------------------------|
| A1-1 | `INSERT INTO grape_im_instance (instance_id, hostname, ip, im_enabled) VALUES (?,?,?,1) ON CONFLICT(instance_id) DO UPDATE SET last_heartbeat=datetime('now'), hostname=COALESCE(excluded.hostname, hostname), ip=COALESCE(excluded.ip, ip), im_enabled=1` | `ON DUPLICATE KEY UPDATE` 替换 `ON CONFLICT(instance_id) DO UPDATE SET`；`excluded.hostname → VALUES(hostname)`、`excluded.ip → VALUES(ip)`；`datetime('now') → CAST(NOW() AS CHAR)`；`? → %s`；无残留 `excluded.` |
| A1-2 | `ON CONFLICT(instance_id) DO UPDATE ... hostname=COALESCE(excluded.hostname, hostname) ...`（含反引号 db 名变体）| 同上映射，反引号/大小写兼容 |
| A1-3 | 非 UPSERT 语句（`SELECT` / 普通 `INSERT INTO x VALUES(?)`）| 原样返回（翻译不动非冲突语法）|
| A1-4 | 多行 / 尾部无分号 | 仍匹配到 `DO UPDATE SET` 一处，翻译完整 |

### 7.2 单元测试 A2：`heartbeat` 双向端可用（mock MySQL 游标）

用 `unittest.mock` 给 `MySQLCompatCursor.execute` 喂 heartbeat SQL：
- 断言 MySQL 后端路径下**不抛异常**且游标收到翻译后 SQL（含 `ON DUPLICATE KEY UPDATE`）。可用 `mock_cursor.execute` 记录 SQL 后比对子串。

### 7.3 集成测试 A3：heartbeat 幂等 + 归属/接管恢复

沿用 `tests/test_cluster_im_lease.py::_alive` 基建（SQLite）：
| # | 用例 | 步骤 | 断言 |
|---|------|------|------|
| A3-1 | heartbeat 写入实例 | `store.heartbeat(hostname='h', ip='')`；读 `grape_im_instance` | 行存在、`instance_id` 正确、`last_heartbeat` 非空、`im_enabled=1` |
| A3-2 | heartbeat 幂等（重复调用不重复行）| 连续调用 2 次同 instance_id | 行数仍为 1，`last_heartbeat` 更新 |
| A3-3 | `list_active_instances` 返回心跳存活实例 | 先 heartbeat（刚写）再查询 | 返回该 instance_id |
| A3-4 | `is_owner_alive` 判定 | 某实例刚 heartbeat → True；未写/超时 → False | 与接管保护（try_acquire steal 逻辑）联动正确 |

### 7.4 端到端验证（集群生产）

| # | 用例 | 步骤 | 断言 |
|---|------|------|------|
| E-1 | 部署修复后 heartbeat 落库 | 重启 `grape-main.service`，观察 `main.log` | `[lease] heartbeat error: 1064` 消失；`grape_im_instance` 有行且 `last_heartbeat` 随时间刷新 |
| E-2 | 多 master 稳定 | 待 5-10 分钟 | `renew failed` / `grace period expired` 不再刷屏；bot lease_owner 稳定单一实例 |
| E-3 | pending-review 通知可送达 | 新起 issue 触发 QA `grape issues pending-review` | `_im_notify` 记录 `sent_ok`（或日志无 `no_ctx`）；用户飞书收到卡片 |
| E-4 | 回归全量 | `pytest -m "not e2e_ui"` | 失败节点与基线一致（无新增）|

## 8. 落档

- 结论（2026-10-13）：pending-review 业务正确；飞书缺失的**终极根因 = `bot_lease.heartbeat()` 使用 SQLite 方言 UPSERT，在 MySQL 后端被 `translate_sql` 漏翻 → 1064 → `grape_im_instance` 恒空 → 多 master 环失效互抢 lease → bot 内存频繁缺失 → 通知 `no_ctx` 跳过**。
- 修复：方案 A（`translate_sql` 增加 ON CONFLICT→ON DUPLICATE KEY 翻译）为核心修复；方案 B/兜底为增强。
- 与 GTD awaiting 缺失同根因（均 `bot_not_in_memory → no_ctx`）。