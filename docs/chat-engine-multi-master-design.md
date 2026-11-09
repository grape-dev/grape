# chat 引擎多主可见性与每用户随机默认引擎（2026-10-27）

## 现象与根因

双主（master-001/master-002）部署下：

1. **chat 前端引擎下拉只显示 master-001 的 3 个直连引擎**，002 的 hermes/opencode 不可见。
2. **未配置引擎的用户全部滑向同一个默认引擎**（master-001 的 hermes，id 最小），单引擎压力集中。

链路上有**三处**独立缺陷，任何一处不修都不生效：

### 缺陷 1：save_agent_runtimes 去重 SQL 缺 host_name 分组（src/user/db.py）

本机直连行按 host_name 区分主控机（engine-selection-design.md §4.3），但函数末尾的
幂等去重 DELETE 分组键缺 host_name：

```sql
-- 修复前
GROUP BY lower(name), scope, owner_id, COALESCE(org_id, 0)
-- 修复后
GROUP BY lower(name), scope, owner_id, COALESCE(org_id, 0), COALESCE(host_name, '')
```

后果：002 的探测循环每 10 分钟 INSERT 的行（id 更大）被当 001 同名行的重复删掉。
002 日志一直 "refreshed 2 local engine(s)" 看似正常，库里永远没有 002 的行。

### 缺陷 2：engine_unique_key 不分主控机（src/runtime/engine_select.py）

内存候选去重键 `local:{engine_type}` 不带机器，即使库里留着 002 的行，
_apply_filters → dedupe_engines 也会把它合并进 001 的同名行。
修复：`local:{engine_type}@{host_name}`（host_name 缺失回退旧行为）。
同时 query_engines 的 local 查询补 host_name 列并透传到候选 dict。

### 缺陷 3：_chat_dedupe_backends 不分主控机（src/api/routes/chat.py）

前端下拉 (_backends) 的去重键 `local:{name}` 同样把双主同名引擎合并成一条。
修复：本机直连键加 runtime_id（`local:{name}#{runtime_id}`）。
引擎列表查询同时带出 host_name，显示名标注归属机
（`hermes v...（master-001 直连）`），owner 字段存 host_name。

## 每用户随机默认引擎（方案 A）

resolve_local_engine 增加 user_id 参数：

- **user_id > 0**（chat 场景）：候选先过滤到**当前这台主控机**的直连引擎
  （host_name == 本机 hostname；跨主机的直连行无法本机拉子进程，只留显示不留执行），
  再按 user_id 稳定取模挑选（`sorted_ids[user_id % len]`）——
  同一用户在同一台机器上恒定同一引擎（会话连续），用户间均匀散开，
  避免 ELB 分流后全员滑向同一引擎。
- **user_id = 0**（IM 等无用户上下文兜底）：维持旧行为取最小 id。
- 缓存跳过条件加 `and not user_id`（按用户的挑选不走缓存短路）。

调用点全部传 user_id：chat.py 4 处（send/stream/_enqueue/_sync）、
session_manager._chat_config_with_local_backend（get_chat_config 透传）、
im/handler_async.py（_uid）。

语义：**已显式配置引擎的用户（sess-cfg 行 runtime_id 有效）保持原配置不动**；
仅未配置用户走随机默认；get_chat_config 物化默认行时按用户写入。

## 验证（2026-10-27 双主实测）

- 001/002 直连行共存（1202/1203/2527 + 16909/16910），各 10 分钟探测刷新不再互删。
- 001 上 user 1→1203、2→2527、3→1202；002 上 user 1→16910、user 2→16909（散开且只选本机）。
- user 93（已配置 1202）读取保持 1202 不变。
- 前端 /api/chat/config 返回 5 个本机直连引擎（双主各标注归属机）。

## 风险与边界

- 跨主机直连行出现在候选列表中（引擎下拉可见），但执行侧 host 过滤保证不会
  被本机 direct 拉起（选不到就回退全局队列引擎）。
- engine_unique_key 变更影响 pick_engine 单选语义：多主同名直连引擎现在算
  不同实例，广播场景下两台都可能被 claim——与"本机直连是独立受管资源"设计一致。
