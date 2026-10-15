# 事件 Seq#<N> 卡死定位与修复方案（plugins/direct 混合下广播任务悬空）

> 状态：根因已定位，**方案 A 已实施**（2026-10-13）｜分支：weekly-07-lyz
> 现象：`<OWNER>/ai-campus-event-planner` 仓库 issue_opened（vod，Seq#<N>）页面持续 5s 自动刷新、事件停留 running、`agent_logs=[]` 零运行日志。
> 实施：`_pool_only_local_direct` 判定收敛为「本机 global direct 引擎」；`test_local_direct_fallback` 反转 UT-02 + 新增 UT-05~08；全量回归零新增失败、并修复 1 个既有失败（test_pool_only_local_direct_all_local）。

---

## 1. 现象

- 事件详情页对 **status=running** 事件做 5s 自动轮询（`feat: 事件详情页 running 状态 5s 自动轮询`），事件不终态 → 页面一直刷新（服务端日志每 5~10s 一条 `GET /api/events/detail/2310` + `GET /api/events/2310/thread`）。
- `agent_logs=[]`、`result=''`、`completed_at=''` —— agent 从未产出任何输出。
- 同类历史遗留：Seq#<N>（qa，2026-10-12 12:35）同样 `queued` 卡死。

## 2. 数据事实（DB 快照 2026-10-13）

### 2.1 agent_tasks

| 字段 | Seq#<N> | 含义 |
|------|---------|------|
| status | `queued` | 从未开始执行 |
| runtime_id | `0` | 未绑定任何引擎 |
| started_at | `None` | 未启动 |
| runtime_session_id | `''` | 无引擎会话 |
| skill | `grape-vod-cli` | CLI 技能（`grape tasks complete` 收尾）|

### 2.2 关键调度日志（16:54:12）

```
Pipeline: pick_engine returned engine_id=0 for repo=gitcode:<OWNER>/ai-campus-event-planner agent=vod seq=2310
Pipeline: enqueued task=t_023b9cdeb2fd seq=2310 agent=vod engine=0 mode=auto dispatch=plugin pool_tag=vod
```

- `mode=auto dispatch=plugin` → 系统处于 **plugin 广播模式**，`pick_engine` 直接返回 0（engine_select.py:333-336）。
- 任务以 `pool_tag=vod` 写入广播队列，等待 worker 通过 `claim-by-tag` 认领。

### 2.3 本机/远端引擎分布

| runtime_id | 引擎 | credential_id | scope | owner/repo | healthy |
|-----------|------|---------------|-------|-----------|---------|
| 24 | opencode | **0**（本机 direct）| global | — | ✅ |
| 25 | codearts | **0**（本机 direct）| global | — | ✅ |
| 38 | opencode | **34**（远端 worker）| repo | `gitcode:Sinkear/AgentLens` | ✅ |
| 39 | codearts | **34**（远端 worker）| repo | `gitcode:Sinkear/AgentLens` | ✅ |
| 40 | hermes | **34**（远端 worker）| repo | `gitcode:Sinkear/AgentLens` | ✅ |

credential_id=34 = `AgentL_10131545`：gitcode 用户 **Sinkear（昵称 单佳豪）**，仓库 `Sinkear/AgentLens`，机器 `<WORKER_HOST>`，**2026-10-13 07:45 注册**、持续心跳（active）。

> 说明：本服务端是多租户共享控制面（runtime_credentials 表含多位用户的 worker）。Sinkear 的外部 worker 挂到本机认领 AgentLens 仓库任务。

## 3. 根因链

```
Seq#<N> enqueue（plugin broadcast，pool_tag=vod，runtime_id=0）
   ↓
等待 worker claim-by-tag 认领
   ├─ 本机 direct 引擎 24/25（credential_id=0）: 不会作为独立 worker 调 claim_by_tag，
   │   依赖服务端 "方案B self-run" 兜底（event_pipeline._pool_only_local_direct）
   └─ 远端 worker 38/39/40（credential_id=34，scope=repo）: 只认领 owner=Sinkear/AgentLens 的任务
        （service.claim_by_tag repo-scope 过滤），永远不会认领 <OWNER> 仓库任务
   ↓
_pool_only_local_direct（event_pipeline.py:2177）:
   SELECT credential_id FROM agent_runtimes WHERE status='online' AND healthy=1
   → 包含 38/39/40（credential_id=34 ≠ 0）
   → all(cred==0) == False
   → return None（放弃本机 self-run，认为"有远端 worker 会接单"）
   ↓
服务端不自跑 + 远端 repo worker 不认领别人的仓库 → 任务永久 queued
   ↓
事件 status=running 不终态 → 前端 5s 轮询 → 页面不停刷新
```

**一句话根因**：`_pool_only_local_direct` 判定"全本机"时把**全库所有在线 healthy 引擎**（含其他租户的 repo-scope 远端 worker）纳入 `all(cred==0)` 判断；只要存在任意远端 worker（哪怕与当前任务、当前仓库无关），本机就放弃 self-run。而这些远端 worker 的 claim 又被 repo-scope 限定只认领自己的仓库 → **既不本机跑、也不远端认领 → 广播任务悬空**。

## 4. 对比佐证（为何之前能跑 / 其它仓库能跑）

| Seq | 仓库 | 结果 | 机制 |
|-----|------|------|------|
| 2306 | acpws (vod) | completed，runtime_id=0，self_reported=1 | **self-run 兜底生效**（当时 AgentLens worker 尚未活跃上线）|
| 2307-2309 | acpws (issue/coding/qa) | 首轮 broadcast failed → **重派到 runtime_id=24**（本机）→ completed | 重派机制才拉回本机 |
| 2310 | ai-campus-event-planner (vod) | 永久 queued | **无重派** + self-run 被远端 worker 破坏 |

> 2307-2309 也佐证了"本机不再自跑"：它们靠重派到 24 才完成，说明在 AgentLens worker 上线后广播路径已不可靠。

## 5. 非根因排除

- ❌ 与 P2（VCS token 注入移除）无关 —— 任务从未启动，无 agent 环境参与；Seq#<N>（部署 P2 前）即已 queued。
- ❌ 与分支部署（weekly-07-lyz / main-opensource）无关 —— 调度逻辑未变。
- ❌ 非"没有引擎" —— 本机 24/25 online+healthy、busy_slots=0、agent_tags=["all"]。

## 6. 修复方案

### 方案 A（推荐）：`_pool_only_local_direct` 修正为本仓库可执行范围

把"全库引擎全为本机"判定改为"**当前仓库执行链上存在可用的本机 direct 引擎即自跑**"。

```python
# event_pipeline.py _pool_only_local_direct(repo_key=None)
def _pool_only_local_direct(repo_key: str = "") -> int | None:
    """方案 B 修正：广播无 worker 认领时，若当前仓库执行链上有本机 direct 引擎则 self-run。

    旧实现判"全库所有 online+healthy 引擎 credential_id 全为 0"——会被其他租户的
    repo-scope 远端 worker（credential_id>0）破坏，导致本机不自跑且远端不认领。
    新实现：只看"本仓库/全局链路上可执行"的本机直连引擎。
    """
    # scope=global 且 credential_id=0（本机直连）→ 对本仓库广播可自跑；优先 opencode
    rows = SELECT id, name FROM agent_runtimes
           WHERE status='online' AND healthy=1
             AND credential_id=0 AND scope='global'
           ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END
           LIMIT 1
    return rows[0][0] if rows else None
```

- 判定基准从"全库引擎"改为"**本机 global direct 引擎**"，其他租户 repo worker 不再影响。
- `_enqueue_pipeline_task` 中调用处改为 `_pool_only_local_direct()`（无参即可，参数仅供扩展）。
- 风险：若同一服务端还有**本机直连 global 引擎专供其它仓库**，逻辑不变（入口一致）；本机引擎属于部署方，自跑其它仓库任务不越权限（token 在服务端按仓库解析）。

### 方案 A 测试用例设计

现有测试文件 `tests/test_local_direct_fallback.py`（weekly-05 plugin-broadcast fallback）：
- **UT-01** `test_pool_only_local_direct_all_local`：全本机 cred=0 → 返回 rid（保持 ✓）
- **UT-02** `test_pool_only_local_direct_with_remote_returns_none`：存在任意 cred>0 → None（**需反转**，见下）
- **UT-03** `test_pool_only_local_direct_no_online_none`：无在线引擎 → None（保持 ✓）
- **UT-04** `test_enqueue_broadcast_self_run_when_only_local`：广播+全本机 → self-run（外 patch rid=24，保持 ✓，语义不变）

#### 变更/新增用例

| # | 用例 | 输入（seed） | 断言 |
|---|------|-------------|------|
| UT-02'（**反转**）| 存在**repo-scope 远端 worker**（cred>0）+ 本机 global direct（cred=0）| `[(2003,0,'online',1,'global'), (2004,99,'online',1,'repo')]` | `_pool_only_local_direct()` **返回 2003**（不再为 None）——本机 global 引擎可 self-run |
| UT-05（**新增**）| **仅 repo-scope 远端 worker**（无本机 global direct）| `[(2005,99,'online',1,'repo')]` 或 `[(2005,0,'online',1,'repo')]` | 返回 None（无本机 global 引擎可自跑，保持等远端认领）|
| UT-06（**新增**）| **本机 local（非 global）direct 引擎** + global 引擎均 cred=0 在线 | `[(2006,0,'online',1,'local'), (2007,0,'online',1,'global')]` | 返回 global 引擎（2007），优先 scope='global' |
| UT-07（**新增**）| **本机 global direct 两枚**（opencode+codearts）| `[(2008,0,'online',1,'global','opencode'), (2009,0,'online',1,'global','codearts')]` | 返回 opencode（2008），按 name 优先级排序 |
| UT-08（**新增**）| repo-scope 远端 worker + 本机 global direct，**但远端 worker 更早在线** | 混合多行 | 仍返回本机 global direct（与并发/时序无关）|

> 语义约定：`_pool_only_local_direct` 仅需**本机 global 直连引擎**即可触发 self-run；repo-scope 远端 worker（无论 cred=0/99）**不再**阻止本机执行。`scheme global` 判定优先于 name 优先级。
>
> `_seed_runtimes` 需扩展支持 `scope`/`name` 字段（当前只插 `(id,cred,status,healthy)`，见 §6 落档备注），新增用例 `INSERT` 补 scope/name 列。

#### 回归风险
- UT-02 反转是本次修复的核心行为变更点，必须同步。
- UT-04 通过 mock `_pool_only_local_direct=24` 隔离，不受重构影响。
- 其余 `agent_runtimes` 相关测试（`test_runtime_agent` / `test_engine_select` 等）不受影响——`_pool_only_local_direct` 仅此一处使用。

### 方案 B：为本机 global 引擎增加独立广播 claim 循环

让服务端自身以 worker 身份轮询 `claim-by-tag`（复用现有 `POST /api/runtime/{id}/tasks/claim-by-tag/{tag}` 链路的服务端侧实现），使本机 direct 引擎（24/25）也能认领 global 广播任务，与远端 worker 对等。

- 优点：彻底统一广播消费模型（本机/远端一视同仁）。
- 成本：新增后台线程/定时器 + 与 `claim` 的锁/并发协调；改动面大于方案 A。

### 方案 C（临时止血）：重派队列

对 `queued` 超过阈值（如 10min）的广播任务，定期扫描并强制重派到本机 global direct 引擎（若存在）。可复用 `events.py` 的 redispatch 逻辑（`POST /{seq}/redispatch`）做定时自动触发。

--- 建议先做 A（改动小、语义对），B 作中期统一，C 作兜底防御 ---

## 7. 验证

### 7.1 单元测试（tests/test_local_direct_fallback.py）

- UT-01 / UT-03 保持。
- **UT-02 反转**为"repo-scope 远端 worker 存在 + 本机 global direct → 返回本机 rid"。
- 新增 UT-05/06/07/08（见 §6 方案 A 测试用例设计表）。

### 7.2 端到端验证

| 用例 | 步骤 | 断言 |
|------|------|------|
| 存在远端 repo worker 时，本仓库广播任务 self-run | 构造：本机 2001(cred0,global) + 远端 2004(cred99,repo)；触发插件广播入队 | 任务由本机 global 引擎执行并 completed，事件终态 |
| 远端 worker 存在时，仍不抢它仓库的任务 | AgentLens 仓库保持现配置 | 该仓库任务仍由 scope=repo worker（38/39/40）认领，行为不变 |
| 重跑 2310/2303 | 手动 `POST /api/events/2310/redispatch` 与 `.../2303/redispatch` | 任务被本机 24 执行并终态；页面停止轮询刷新 |
| 回归全量 | `pytest -m "not e2e_ui"` | 失败节点与基线一致（无新增失败）|

## 8. 落档

- 结论：广播任务悬空是本机 self-run 兜底被"跨租户远端 worker"破坏所致；修复方案 A（推荐）：`_pool_only_local_direct` 判定收敛到"本机 global direct 引擎"。
- 备注：`tests/test_local_direct_fallback.py::_seed_runtimes` 目前只插 `(id,cred,status,healthy)` 四列，方案 A 新增用例需扩展为支持 `scope` 与 `name` 列（`INSERT INTO agent_runtimes (id,name,status,credential_id,healthy,...,scope,agent_tags)`）。
- 运维提示：生产为多租户共享控制面，任何仓库接入远端 worker 都会影响"全本机"类判断；后续新增此类判断需以"本仓库执行范围"为准。