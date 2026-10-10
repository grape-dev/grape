# 方案：集群 IM 均匀分布 + Lease 粘性抗抖动 + 通知可观测（Seq #9708）

> 状态：技术方案（2026-10-10）｜分支：weekly-06-lyz
> 动机：集群事件 Seq #9708（qa pending-review）未在触发瞬间发出飞书「需求待审核」卡片，约 1 小时后才经用户评论回环触达。根因（实证见 §1.2）：
> 1. 集群多实例同时跑 IM WS/lease，**分布无归属、纯先到先得**，且 `renew` 与 MySQL 写延迟竞争导致 lease 在续约前过期 → 被其它实例抢走 → bot 反复断开/重连（日志 `grace period expired → removed → connecting → connected` 分钟级 flutter）；
> 2. pending-review 通知依赖「当前处理实例恰好持有目标用户的 bot」这一瞬态 → `_resolve_user_chat_bot` 返回 None → **静默跳过**（无日志、无重试、无落库）。
> 目标：**① 使集群 IM 尽量均匀分布在存活的各实例上；② 一旦 bot 连上，羊皮化（sticky）尽量不迁移，显著降低抖动**（方向 2）；**③ 通知的 bot 解析与发送全程可观测**（方向 4）。

---

## 一、问题与实证（2026-10-10 集群定位）

### 1.1 现状 lease 机制（代码核对）

| 参数 | 值 | 说明 |
|------|----|------|
| `BOT_LEASE_TTL` | 90s | lease 存活（config.py:163） |
| `BOT_LEASE_RENEW_INTERVAL` | 20s（=每 2 次 sweep 续约） | `lease_sweep_loop`（feishu_async.py:1531） |
| `BOT_LEASE_SWEEP_INTERVAL` | 10s | 每次 sweep：游标查可抢 bot → `try_acquire` → 直接连；批量续约；is_active=0 断开；每 ~5min reload |

关键实现点：
- **分布 = 先到先得**：`list_expired_or_unheld(ORDER BY bot_app_id)` 从表头开始扫，每个实例每次 sweep 都抢同一批头部 bot —— 无归属、无 hash 均匀；
- **续约**：`batch_renew` 先 `UPDATE ... WHERE lease_owner=?` 再按 token 校验（bot_lease.py:125）；失败→单条重试→仍败→**30s 宽限期后释放+断开**；
- **可被抢**：`try_acquire` 只要 `lease_expires_at < now` 就无条件接管（bot_lease.py:44-49），**不校验旧 owner 是否存活**。

### 1.2 集群实证（Seq #9708，issue 2246 / grape-dev/grape #284）

- 09:38-09:39 CST：qa(seq 9708) 完成 → 触发 pending-review → 应发「需求待审核」卡；
- 当日主日志：`T02:59:47 [chat_bot_cli_aa1c0e93] connecting→connected`（=10:59 CST bot 才回内存）；`T03:04:48` 又一次全家重连；`T04:57` 又 `grace period expired → disconnect`；Oct 8 亦出现 `cli_aa1c0e93 renew failed → grace → IM WS stopping`；
- 结论：**pending-review 触发时刻（09:38），用户本人 bot `cli_aa1c0e93` 恰不在该实例 bot_manager**（lease 被抢/释放），`_resolve_user_chat_bot`（issues_center.py:690）只认内存 `bm.get_all_bots()` → None → `_run_pending_review_notify` 静默 return；
- open_id（选了 user_id=38 的 mapping）与 chat 会话（`sess-cfg-38`）均配置齐全——**唯一断点就是「bot 在内存里的瞬态」**。

---

## 二、方向 2：均匀分布 + Lease 粘性 + 抗抖动

### 2.1 实例注册表 + 心跳（分布/接管的唯一真值）

新增表 `grape_im_instance`（MySQL/SQLite 兼容，`src/api/db_setup.py` 加建表）：

```sql
CREATE TABLE IF NOT EXISTS grape_im_instance (
  instance_id   VARCHAR(64) PRIMARY KEY,
  hostname      VARCHAR(64) NOT NULL,
  ip            VARCHAR(64) DEFAULT '',
  joined_at     DATETIME DEFAULT (datetime('now')),
  last_heartbeat DATETIME DEFAULT (datetime('now')),
  im_enabled    INT DEFAULT 1
);
```

- 每个启用 IM 的实例启动时 `INSERT ... ON DUPLICATE KEY UPDATE` 注册自身，并每 10s 更新 `last_heartbeat`（与 lease sweep 同一循环合拍）；
- 存活实例集合 = `last_heartbeat` 距今 < `IM_INSTANCE_TIMEOUT`（默认 45s，3 倍心跳周期）且 `im_enabled=1`；
- **实例退出/失联的 bot 被动接管依据**：`try_acquire` 前校验「当前 lease_owner（若在实例表中）已失联」，否则**拒绝接管**（§2.3）。

### 2.2 一致性哈希归属（均匀 + 粘性）

- 每个实例独立计算**一致性哈希环**：`ring = [hash(instance_id) for active instances]`（复刻 ketama：每个 instance 放 N 个虚拟节点，如 N=50）；`owner(app_id) = ring.clockwise(hash(app_id))`；
- `lease_sweep_loop` 第 1 步改为：**仅当 `owner(app_id) == self.instance_id`** 才 `try_acquire`（候选仍从 `list_expired_or_unheld` 分页取，但做归属过滤）；
- 效果：
  - **均匀**：bot 按 hash 环均匀落在实例上，不再扎堆表头；
  - **粘性**：实例集合不变的稳态下，每个 bot 只有一个确定性 owner，**不会因「先到先得」被他人抢**；
  - **最小迁移**：实例上下线只影响其邻近环段的 bot，其余不动（一致性哈希性质）；
- 环重建仅发生在实例心跳集合变化（加入/超时），重建后 owner 变化的最小集合自然接管。

### 2.3 Lease 抗抖动硬化（bot_lease.py）

| 项 | 现值 | 改为 | 理由 |
|----|------|------|------|
| `BOT_LEASE_TTL` | 90s | **180s**（env 可调） | 给续约周期留 ≥6x 余量，容忍 MySQL 慢写 |
| `BOT_LEASE_RENEW_INTERVAL` | 20s | **30s**（TTL/6） | 降低 DB 写频、减少争用 |
| renew 失败→宽限期 | 单次重试仍败即 30s | **连续 3 次 sweep 失败**（30s 观察窗）才进宽限期 | 瞬时抖动不再立刻断连 |
| 宽限期 | 30s | **90s** | 给恢复窗口，避免断连后立刻重建 |
| `try_acquire` 接管条件 | lease 过期即抢 | `lease 过期 且 旧 owner 失联或不在实例表` | 防「活着只是 renew 慢」被误抢 |
| 抢到后连接失败 | 释放（现状） | 释放 + backoff 30s 内不再抢同一 app_id | 防同 bot 循环抢占 |

> `IM_LEASE_STICKY=1` 开关（默认 1）；出现异常可 `=0` 退回现状先到先得，灰度可逆。

### 2.4 周期 reload 与迁移

- 每 5min reload 时**重算环**；对断言归属仍属自己的 bot 继续 renew；对归属迁移给别的实例的 bot，若已持 lease，**保留到本地 lease 自然过期并在过期时不续约**（优雅让渡），不强制立刻断开 → 迁移无缝、无断连抖动。

### 2.5 重连原因账（reconnect ledger）+ WS keepalive（可选）

> 说明边界：方向 2 只能消除 **lease 驱动的重连**；Feishu 传输层主动断开 / 网络抖动 / 实例进程重启仍会产生 `connecting→connected`。为区分"哪些重连是被 lease 修掉的、哪些属于残留"，需先记账。

**A. 断开原因标注（run_bot 断连点）**
- `run_bot`（feishu_async.py WS 循环）在每处「关闭/异常退出」路径按类型标记：
  - `reason=lease`：由宽限期/优雅让渡主动断开（方向 2 接管）；
  - `reason=ws_transport`：Feishu 返回的断连/网络异常/心跳超时；
  - `reason=shutdown`：stop_bot / IM WS 停止 / 进程将退出。
- 落到 `ctx.metrics.disconnect_reasons[reason] += 1` + 一条 `[im-ws] bot=<id> disconnect reason=... held_secs=<s>` 日志。

**B. 重连账本（flap 统计）**
- `BotContext.metrics` 增加 `reconnect_count / disconnect_reasons / last_connected_at`；
- `BotManager.health_check()`/observability 聚合：`total_reconnects`、`flap_by_cause={lease,ws_transport,shutdown}`、各 bot `held_secs` 分布；
- 上线后按账本判定主导原因：若 `ws_transport` 占比高 → 走 **C**；若 `lease` 归零 → 方向 2 已生效。

**C. WS keepalive（可选，按账本按需启用）**
- Feishu WS 长连接投心跳：依据账本 `ws_transport` 占比或配置 `IM_WS_KEEPALIVE=1` 启用；
- 实现：基于现有 WS 消息流隙发 ping（或 lark_oapi 的 heartbeat 配置），超 `IM_WS_IDLE_TIMEOUT`（默认 60s）未收到任何帧则主动 `reconnect`（计入 `ws_transport`）；
- 降低"长任务静默窗口内被 Feishu 服务端回收"的机会。

---

## 三、方向 4：通知解析/发送可观测（静默 → 可观测）

### 3.1 `_resolve_user_chat_bot`（issues_center.py:690）

- 命中：`log.info("[im-notify] resolve user=%s bot=%s ok", user_id, app_id)`
- 未命中，**区分原因并 WARNING**：
  - `no_bm`：bot_manager 未就绪；
  - `no_session_row`：chat_agent_session 无该用户 im_app_id；
  - `bot_not_in_memory`：会话记录存在但 app_id 不在本实例 `get_all_bots()`（**本次 #9708 场景**，标注 `hint=sticky-lease`）；
  - `bot_error`：异常。
- 同一 `(user_id)` 短窗口聚合防刷（log.warning 每 60s 至多 1 条）。

### 3.2 三条 `_run_*_notify`（issues_center.py `_run_notify/_run_awaiting_notify/_run_pending_review_notify`）

统一在最前打一条结构化开始日志 + skip 原因：

```
[im-notify] type=pending_review issue=#284 user=38 open_id=<ou...|> ctx=cli_aa1c0e93|none reason=ok|no_ctx|no_openid|send_failed
```

- `ctx=none` 时 reason=`no_ctx`（WARNING）——这正是 #9708 曾经的静默点；
- `open_id` 空 → `no_openid`（WARNING）；
- `send_card_to_user` 结果补 ok/info 日志（与 `send_card_to_chat` 对齐，feishu_async.py 当前 user 卡不记发送成功）。

### 3.3 计数器（体检/指标可观测）

- `BotContext.metrics`（或模块级 `dict`）增加：`notify_attempted / notify_skipped_no_ctx / notify_skipped_no_openid / notify_sent_ok / notify_sent_failed`；
- `BotManager.health_check()`（feishu_async.py:1666）与 observability 暴露聚合值；日志轮转文件 `data/logs/devops-bot.log` 以 `[im-notify]` 前缀可直接 grep 联排。

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/db_setup.py` | 建表 `grape_im_instance`（+ 迁移兼容） |
| `src/im/bot_lease.py` | `try_acquire` 增加「旧 owner 失联校验 + 领后可冷却」；参数化 TTL/观察窗/宽限期；`is_owner_alive(app_id)` helper |
| `src/im/feishu_async.py` | `lease_sweep_loop`：实例心跳上报 + 一致性哈希 `owner(app_id)` 过滤 + 优雅让渡（§2.4）+ 3.3 计数器；`health_check` 暴露；**`run_bot` 断连原因标注 + 重连账本（§2.5-A/B）+ 可选 keepalive（§2.5-C）** |
| `src/config.py` | `IM_LEASE_STICKY`、`IM_INSTANCE_TIMEOUT`、`BOT_LEASE_TTL=180`、`BOT_LEASE_RENEW_INTERVAL=30`、`BOT_LEASE_GRACE=90`、`BOT_LEASE_RENEW_FAIL_THRESHOLD=3` |
| `src/api/issues_center.py` | `_resolve_user_chat_bot` 命中/未命中日志（3.1）；三条 `_run_*_notify` 开始/skip/结果日志（3.2） |
| `src/im/feishu_async.py`（send） | `send_card_to_user` 成功/失败结果日志（3.2） |
| `tests/` | 见 §六 |

---

## 五、影响面 / 边界

| 项 | 说明 |
|----|------|
| 单机部署 | 实例表只有 1 行、环上 owner 恒为自身 → 行为等价现状（`IM_LEASE_STICKY` 可关） |
| bot 迁移 | 优雅让渡利用「本地 lease 自然过期不再续约」，期间持有者照常收发 → 无感知断流 |
| MySQL 抖动 | TTL 180s + 3 次观察 + 90s 宽限：单次/短时阻塞不再触发断连（本方案核心收益） |
| 接管正确性 | 只有「失联实例」的 lease 才可被他人接管；实例存活时他人无法 `try_acquire`（防活体误抢） |
| **重连边界** | 方向 2 只消除 **lease 驱动**重连；`ws_transport`/`shutdown` 仍会有重连——账本（§2.5）区分三类原因，`ws_transport` 主导时按需启用 keepalive（§2.5-C） |
| 竞技状态 | 通知发送本就 REST（不依赖 WS），方向 2 解决「解析时 bot 不在内存」；方向 4 让此类情况有日志可查 |
| 数据 | `grape_im_instance` 仅心跳元数据，无业务敏感字段 |

---

## 六、验证

| 用例 | 断言 |
|------|------|
| UT-01 `_resolve_user_chat_bot` 无 bot_manager / 无会话 / bot 不在内存 → 对应 WARNING 且 `reason` 正确 | 方向 4 |
| UT-02 `_run_pending_review_notify` ctx=None → `[im-notify] reason=no_ctx` 日志 + `notify_skipped_no_ctx+1` | 方向 4（对应 #9708） |
| UT-03 `try_acquire`：lease 过期但 owner 心跳存活 → 拒绝接管；owner 失联 → 接管 | 方向 2 |
| UT-04 `owner(app_id)` 一致性哈希：同实例集合两次计算相同；增/减一个实例只改变该 bot 环段内集合 | 方向 2 均匀/粘性 |
| UT-05 renew 连续 2 次失败（<阈值）→ 不断连；第 3 次失败 → 进宽限期并按新时长断开 | 抗抖 |
| UT-06 优雅让渡：环迁移后持有者不续约、过期自动释放，期间 keepalive 正常 | 迁移无断流 |
| UT-07 `run_bot` 断开按 `lease/ws_transport/shutdown` 归因并计数（mock 各退出路径） | 重连账本 |
| UT-08 keepalive：空闲 > 阈值未收帧 → `ws_transport` 重连计数 +1 且重连（mock WS） | keepalive 可选 |
| 回归 | 单机 lease 关闭路径、`send_card_to_user`/`send_card_to_chat` 行为不变 |

---

## 七、落档修订说明

- 本次为**服务端**修：方向 2 治「bot 抖动/被抢」根因（均匀 + 粘性 + 抗抖），方向 4 治「静默无痕」（通知解析/发送全链路可观测）。
- 方案 1（通知与 bot 生命周期解耦、按 im_app_id+secret 直接发）与方案 3（待发补偿队列）不在本次范围，列出供后续。
- 键值全部 env 可调并按单机兼容，灰度回退开关 `IM_LEASE_STICKY`。
- 部署后需全节点重启 grape（新表 + 新循环），并在集群主节点核对 `BotManager.health_check` 的 bot 分布、flap 计数与**重连账本**（`lease/ws_transport/shutdown`）；`ws_transport` 占比高时再启用 keepalive（§2.5-C）。