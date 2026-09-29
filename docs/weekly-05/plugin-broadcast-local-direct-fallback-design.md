# 方案：plugin 广播任务回落本机 direct 引擎（本机自执行）

> 状态：技术方案（2026-09-29）｜目标分支：weekly-05（实际部署 weekly-04-lyz）
> 关联：worker 已删除后，`run_pipeline` 产生的 plugin 广播任务（engine_id=0, pool_tag=agent）
> 无 worker `claim_by_tag` 认领 → 永久 queued、无实时日志（Seq #2206 实证：issue-cli 卡 queued）。

---

## 1. 现象

- 本机 `agent_runtimes` 有 2 个**本机 direct 引擎**（id=24 opencode / id=25 codearts，`credential_id=0`、`agent_tags=["all"]`、online），仓库 `devops_agent_session` 各 agent 均绑定 runtime_id=24。
- 但 webhook 正常派发（`run_pipeline` → `_enqueue_pipeline_task`）在 **plugin 模式**下 `pick_engine` 返回 0 → 广播（`pool_tag=agent`，engine_id=0）→ **等远端/本机 worker 认领**。
- worker 已删除 → 无人 `claim_by_tag` → task `queued` 永不执行 → 无 `step_start`/`report_messages` → 无实时日志、事件 running 不收敛。
- VOD 2205 能跑仅因 redispatch 显式 rid=24 + `start_direct_agent` 走了本机 direct 旁路；正常广播路径无此处理。

## 2. 根因

**plugin 广播认领依赖 worker 进程**（`src.runtime.agent` 的 `claim_by_tag`）。本机引擎（24/25）是**服务端注册的表记录**，本身不认领任务；没有 worker 进程 → 广播任务悬空。

**切入点确认**（代码定位）：
- `_enqueue_pipeline_task`（event_pipeline.py）L2010 `enqueue_task(...)` 广播入队后**无本机执行触发**
- `start_direct_agent(_execute_agent, (...))` + `_create_direct_task` 已有本机 direct 执行能力（redispatch/QA 分支复用）
- 判断「仅本机 direct」依据：`get_scene_pool_engine_ids(scene_id, agent_type, ...)` 返回的引擎 id 的 `credential_id` 是否全为 0（本机 direct）

## 3. 修复目标（方案 B）

- plugin 广播任务入队后，若池内候选引擎**仅本机 direct 引擎**（credential_id=0 且 online+healthy），由**服务端本机直接执行**（`start_direct_agent`），替代等 worker。
- 远端 worker 存在时**不回落**（保持广播认领，worker 多机负载）。
- 不改变 legacy 模式与已绑定 runtime_id 的确定性路由。

## 4. 方案设计

### 4.1 判断「仅本机 direct」辅助函数

在 `event_pipeline.py` 增加：

```python
def _pool_only_local_direct(runtime_id: int, repo_key: str, agent_type: str,
                            scene_id: str = "", user_id: int = 0) -> int | None:
    """判断广播池是否仅本机 direct 引擎；是则返回候选 runtime_id（本机 self-claim）。

    依据：池内候选引擎（scene pool_engine_ids → 在线+健康+tag 匹配）的 credential_id 全为 0
    （本机 direct）。纯远端 worker 池（有 credential_id>0 引擎）→ 返回 None（保持广播）。
    """
    if runtime_id:
        return None                        # 已确定性路由，无需回落
    from src.runtime.engine_select import get_scene_pool_engine_ids
    from src.session_manager import db, db_lock
    _ids = get_scene_pool_engine_ids(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
    if not _ids:
        _ids = [24]                        # 兜底：无池配置时空闲判定略（保守）
    with db_lock:
        c = db.cursor()
        _valid = []
        for _eid in _ids:
            try:
                c.execute(
                    "SELECT credential_id FROM agent_runtimes WHERE id=? AND status='online' AND healthy=1",
                    (_eid,))
                _row = c.fetchone()
            except Exception:
                _row = None
            if _row:
                _valid.append((_eid, _row[0] or 0))
    if _valid and all(cid == 0 for _eid, cid in _valid):
        return _valid[0][0]                # 全本机 → 首个 direct 引擎
    return None
```

（注：`_ids` 兜底需谨慎——应按实际池配置；若空，表示无池引擎，应保留广播或按 `pool_engine_ids` 判定。）

### 4.2 回落入队端点

`_enqueue_pipeline_task` 在 `enqueue_task` 成功后（L2036 前）补：

```python
# 方案 B：plugin 广播且池仅本机 direct → 本机直接执行（不等 worker）
if not runtime_id and is_plugin_mode() and _pool_agent_type:
    _local_rid = _pool_only_local_direct(runtime_id, repo_key, agent_type,
                                          scene_id, user_id)
    if _local_rid:
        log.info(f"Pipeline enqueue: pool only local direct, self-run seq={seq} rid={_local_rid}")
        try:
            from src.api.event_pipeline import _execute_agent  # 模块内已引入
            from src.runtime.engine_select import engine_name
            from src.repo_key import from_repo_key
            from src.api.event_pipeline import start_direct_agent
            _be = engine_name(_local_rid) or backend or ""
            _evt2 = dict(evt)
            _evt2.setdefault("repository", from_repo_key(repo_key))
            start_direct_agent(_execute_agent, (
                repo_key, seq, agent_type, _be, skill_name, _evt2, _local_rid, tid,
            ), name=f"self-{seq}")
        except Exception as e:
            log.warning(f"Pipeline enqueue: local self-run failed for seq={seq}: {e}")
```

> 要点：`_execute_agent` 是异步协程 → `start_direct_agent` 包装执行；`_execute_agent_inner` 里我们已有方案 A 的 direct 双信号收敛，task 正常完成 → structured_output 落库。

### 4.3 scene_id / user_id 传入

`_enqueue_pipeline_task` 已有 `evt`；scene_id 可从 `evt`/现有逻辑取（`get_pipeline_config` 同源）。user_id 用现有 `user_id` 参数。

## 5. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/event_pipeline.py` | 新增 `_pool_only_local_direct` + 改 `_enqueue_pipeline_task` enqueue 后回落 | 广播→本机 direct 自执行 |
| `src/runtime/engine_select.py` | （辅助，可选）暴露 `get_scene_pool_engine_ids`（已存在） | 复用 |
| `tests/test_local_direct_fallback.py`（新增） | 测试 | 广播仅本机→self-run；有远端引擎→不回落 |

## 6. 测试

- UT-01 `_pool_only_local_direct`：池内 credential_id 全 0 → 返回 rid（本机）；含 >0 → None
- UT-02 `_enqueue_pipeline_task` 广播 + 仅本机 → 触发 `start_direct_agent`（mock），task 被本机执行
- UT-03 远端 worker 池（credential_id>0）→ 不回落（保持 enqueue 广播）
- 回归：现有 vod/issue direct（2205 方案 A）不受影响；worker 模式蓝本不动

## 7. 验收对照

| 项 | 结果 |
|----|------|
| 本机 direct 引擎参与广播收发（无 worker 也执行） | ✅ 4.2 |
| 广播任务不再永久 queued、有实时日志 | ✅ 4.2（self-run 触发 report_messages） |
| 远端 worker 池不回归（保持多机认领） | ✅ 4.1/6-UT03 |
| 与方案 A（direct 双信号收敛）协同，事件正常 completed + 结论落库 | ✅ 4.2 尾部 |