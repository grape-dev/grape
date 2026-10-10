# Spec: Issue #253 — 场景引擎绑定升级为硬约束（宁失败，不跑飞）

> Owner: Neko | 2026-10-09 | 状态：待主人审批

## 0. 一句话

场景节点配置了非空 `engine_runtime_ids`（显式绑定）时，任务**只能**由绑定集合中的引擎执行。
绑定引擎不可用（离线/失联/健康分低）→ 任务**快速失败并给出人话原因**，绝不降级放行池外引擎。

依据：明亮在 issue #253 的预期 + 主人决策（方案 A：低健康分限时等待，超时失败）。
设计哲学对齐：失败不兜底，终止比浪费资源好。

## 1. 背景与根因（对账自本地代码，行号为当前 HEAD）

事故链（3 次派发全部失败，hermes 全局 worker 抢走 codex 专属任务）暴露出绑定是"尽力而为"而非"承诺"：

| # | 漏洞点 | 代码位置 | 现行为 |
|---|--------|----------|--------|
| ① | 重派重建新任务不带 pool_engine_ids | routes/events.py:765-786（enqueue_task 未传该参数） | 新任务成无限制广播，凭查询瞬间运气定归属 |
| ② | 认领时绑定引擎"全不可达"→清空 pool 放行 | service.py:1015-1035（claim）、1287-1305（claim_by_tag） | 撕证后任意引擎可领（含 all 标签的全局 worker） |
| ③ | 排队超时防饿死→清空 pool 放行 | service.py:2438-2487（_check_stale_queued_tasks） | 同上，120s 后证被撕 |
| ④ | direct 模式自动选机不看绑定 | event_pipeline.py:1822-1849（全站 online/healthy 查询） | hermes 等池外引擎可被选中 |
| ⑤ | 低健康分死区 | claim_by_tag 健康分门禁 × "物理可达不清空"组合 | 引擎活着但低分：领不了 + 不撕证 = 永久 queued（实测挂 28 分钟） |

首帧旧名（events.js:156 硬编码 'OpenCode' 兜底）属问题 2，本 spec 不覆盖，另行处理。

## 2. 改动方案

总原则：**证（pool_engine_ids）随任务走全生命周期；每一道关口的"放行降级"改为"诚实失败"。**
空绑定（未配置 engine_runtime_ids）语义完全不变：全站广播，一切照旧。

### 改动 1：重派贴证（堵漏洞①）

文件：`src/api/routes/events.py`（终态→快照重建分支，657 行起）

- 建新任务前，从 issue 场景**重读**绑定（不抄旧任务快照——中间代任务的证可能已被写空）：
  scene_id/scene_user_id 取自 issue_state（回退 issue 表），调 `get_scene_pool_engine_ids(scene_id, agent_type, repo_key, user_id)`。
- `get_scene_pool_engine_ids` 返回 None（绑定全灭）→ 不建任务：**API 响应直接带回人话原因**
  （重派是用户主动触发，提示即时可见），同时事件置 failed 持久化原因
  （复用 #259 的 `_fail_scene_all_offline`：`绑定的 N 台引擎（xxx）全部离线，已停止派发`）。
  **早退必须发生在函数末尾 `reset_event_terminal(seq)`（934 行）之前**：提前 return，
  事件保持 failed（含原因）；若写在复位之后，事件会被掰回 running 然后永远悬挂。
  API 返回 409 + 人话原因（与本函数现有 747 行"无可用引擎，无法重派"同一惯例）。
- `enqueue_task(...)` 补传 `pool_engine_ids=<JSON>`。
- **守卫：`_rid=0` 时禁止触发 direct 执行**。现有 `runtime_credential_id(0)` 返回 None，
  `None or 0` → `_cid=0` → `_is_local_direct=True` → 788 行会用空引擎（engine_id=0）启动直跑。
  本改动使 `_rid=0`（绑定引擎活着但暂不可选 → 贴证广播）成为合法结局，必须改为
  `_is_local_direct and _new_task_id and _rid > 0` 才触发直跑；`_rid=0` 只入队等绑定引擎认领。
- 选机查询（731 行）选不到具体引擎且 pool 非空 → 允许 `runtime_id=0` 广播（范围已被 pool 限定，
  只有绑定引擎能领，属合法路径，不必失败）。746 行的 409 兜底维持不变（仅 legacy 模式）。

### 改动 2：认领处撕证 → 失败（堵漏洞②）

文件：`src/runtime/service.py`，两处对称修改：

- claim（1015-1035）与 claim_by_tag（1287-1305）中 `_check_pool_engines_reachable` 返回 False
  （绑定引擎全部物理不可达：离线/心跳过期/无 tag/scope 不符/满槽）时：
  - 现行为：`UPDATE pool_engine_ids=''` + `break`（放行）
  - 新行为：任务置 `failed`，`errors` 写人话原因（列出绑定引擎名），同步事件 failed
    （复用 `update_event_status`，模式同 937-945 行的回收终态同步），**保留 pool_engine_ids 不清**。
- 失败动作在持有 db_lock 的事务内完成，遵守 #259 的 Lock contract 注释。

### 改动 3：排队超时撕证 → 限时等待后失败（堵漏洞③⑤，方案 A）

文件：`src/runtime/service.py` `_check_stale_queued_tasks`（2438-2487）

超时阈值复用 `_STALE_QUEUED_THRESHOLD`（默认 120s，env `HEALTH_STALE_QUEUED_THRESHOLD` 可调）：

| 情形 | 现行为 | 新行为 |
|------|--------|--------|
| 池内有健康引擎 | continue（等待） | 不变（正常路径，引擎马上会领） |
| 引擎不健康但物理可达（漏洞⑤死区） | continue（**无限**等待） | 有界等待：超时后任务 failed，原因`绑定引擎健康分不足，等待 Ns 未恢复` |
| 引擎既不健康又不可达 | 清空 pool 放行 | 任务 failed，原因`绑定引擎失联/离线` |

28 分钟静默挂死 → 120 秒明确失败，用户拿到原因自行决定重试或换绑定。

### 改动 4：direct 模式选机尊重绑定（堵漏洞④）

文件：`src/api/event_pipeline.py`（1822-1849 自动选机段）

- `_pool_engine_ids` 非空（1779 行已取，None 已在上游 return）→ 1831 行查询追加
  `AND id IN (绑定列表)`，且排序仅在同绑定集合内比较。
- 绑定集合内无在线健康引擎 → 不走全站兜底：事件 failed + 人话原因（同改动 1 格式）。
- `_pool_engine_ids` 为空 → 现行为不变（全站自动选机）。

### 不改的部分（明确出界）

- grape-worker 侧（grape-worker/service.py:673-684）：只跳过不撕证，已合规，零改动。
- 空绑定 = 广播语义：全部改动都以"pool 非空"为前置条件。
- 问题 2（前端旧名）：另立 spec。
- 事件回填/健康分门禁/claim 主查询：不动。
- local_only=True 分支（同机重试，用户明示覆盖引擎选择）：不动。
- 无关联任务走 run_pipeline 的分支（首次派发侧 #259 已把守）：不动。
- 早退路径跳过 `redispatch:{seq}` 幂等戳记 → 失败后可立即重点重试（读+失败幂等，无害）。

## 3. 影响面

- 文件：`routes/events.py`、`runtime/service.py`、`event_pipeline.py`（3 个文件，预计 ~120 行净变化）
- 行为变化（用户可见）：
  - 绑定引擎不可用的任务从"静默挂死/被陌生引擎接走"变为"≤120s 内 failed + 明确原因"
  - hermes 等池外全局 worker 永远无法认领有显式绑定的任务
- 风险：绑定引擎长时间不可用时任务不再有人跑（预期内——用户明示绑定即授权范围）；
  空绑定路径零变化，广播调度不受影响。

## 4. 验收标准

- [ ] 场景唯一绑定 codex；codex 被取消后立即重派 → hermes（在线、all 标签）无法认领，任务保持限定
- [ ] 重派时 codex 离线 → 新任务不创建，**API 响应含人话原因**，事件 failed，原因含"绑定的 1 台引擎（codex-xxx）全部离线"
- [ ] codex 健康分低 + 物理可达 → 任务 120s 后 failed（原因注明健康分），不再无限挂死
- [ ] 任务排队中 codex 心跳丢失 → 任务 failed 而非被清空绑定放行
- [ ] direct 模式 + 绑定非空 → 只在绑定集合内选机；集合全灭 → failed 不兜底
- [ ] 空绑定场景：广播、认领、超时行为与现状完全一致（回归保护）
- [ ] 失败原因人话格式与 #259 现有产出一致（含引擎名、台数）

## 5. 测试计划（只跑相关文件）

- `tests/` 中 issue #259 相关 + claim/pool_engine_ids 相关测试文件增量验证
- 新增用例覆盖验收标准 6 条场景（含空绑定回归）
- 不跑全量（主人规矩）

## 6. 上线注意

- 服务端与两 worker 的 `RUNTIME_TASK_TIMEOUT` 契约不受影响（本改动只动派发/认领判定，不动执行超时）
- 改动后需重启 Grape 服务（pkill src.api.run + 复验运行态契约）才进契约
