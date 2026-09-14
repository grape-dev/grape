# Plugin 模式健康分增强方案

## 一、背景与问题

### 1.1 当前 Plugin 模式分发机制

Plugin 模式下 `pick_engine()` 统一返回 0（广播），不选具体引擎。任务创建后写入 `pool_engine_ids` 限定范围，worker 端通过 `claim_by_tag` 竞争认领（先到先得）。健康分过滤仅在 legacy 模式的 `pick_engine` 选机阶段生效，**plugin 模式下完全不参与分发决策**。

### 1.2 实际运行数据暴露的问题

近 3 天任务统计显示：

| 引擎 | 总任务 | 完成 | 失败 | 失败率 | 连续失败 | 健康分 |
|------|--------|------|------|--------|---------|--------|
| 1079 opencode@master-002 | 60 | 55 | 4 | 6.7% | 32 | ~93 |
| 2639 hermes@lvjuntao | 59 | 41 | 9 | 15.3% | 12 | ~85 |
| 2631 hermes@liufei-build-005 | 10 | 0 | 10 | 100% | 10 | 0 |
| 2632 opencode@grape-singapore | 10 | 2 | 8 | 80% | 8 | 0 |
| 2634 codex@grape-singapore | 8 | 0 | 8 | 100% | 8 | 0 |
| 2630 codearts@liufei-build-005 | 6 | 0 | 6 | 100% | 6 | 0 |
| 2619 opencode@hellob | 6 | 0 | 6 | 100% | 6 | 0 |

**核心问题：**
- 引擎 2631/2632/2634/2630/2619 失败率 80%-100%，健康分应为 0，但仍然在认领任务并不断失败
- 引擎 1079 承接了最多任务（60 个），连续失败 32 次但健康分仍有 ~93（因为总基数大拉低了失败率），大量失败任务浪费资源
- 健康分 = 0 的引擎和健康分 = 100 的引擎在 claim_by_tag 竞争中**完全平等**，先到先得
- 一个健康分 0 的引擎抢到任务后大概率失败，任务被回收重试，浪费 2-3 倍时间

### 1.3 预期收益

在 plugin 模式的 `claim_by_tag` 中引入健康分维度后：
- 健康分 0 的引擎不参与认领（或降权），减少无效认领和任务失败重试
- 优先让健康分高的引擎认领，提升整体任务成功率
- 健康分低的引擎在问题修复前自然被"冷却"，修复后健康分回升自动恢复

---

## 二、方案设计

### 2.1 总体思路

在 `claim_by_tag` 的任务认领逻辑中增加健康分检查层，作为现有 `pool_engine_ids` / scope / tag 能力检查之后的额外过滤。不改变广播协议和任务创建流程，仅在认领决策点插入健康分维度。

### 2.2 健康分检查点

```
worker 调用 claim_by_tag(tag, runtime_id)
  ↓
现有检查链：
  1. tag 能力匹配（engine_capabilities）
  2. 并发槽检查（busy_slots < max_concurrent）
  3. scope 匹配（repo/user/global）
  4. pool_engine_ids 限定检查
  ↓
新增检查（本方案）：
  5. 健康分检查 ← 新增
  ↓
标记 dispatched + 返回任务
```

### 2.3 健康分检查规则

在 `claim_by_tag` 函数中，当找到一个候选任务（通过所有现有检查后）准备标记 dispatched 之前，插入健康分检查：

```python
# === 健康分检查（Plugin 模式增强层）===
# 引擎自身健康分过低时不允许认领新任务，避免低质量引擎抢任务后失败
from src.runtime.health_scorer import is_healthy as _is_engine_healthy
if _is_engine_healthy(runtime_id):
    # 健康分达标 → 正常认领
    pass
else:
    # 健康分过低 → 拒绝认领
    log.info(f"claim_by_tag: engine {runtime_id} health score too low, skip claiming")
    return None
```

**关键设计决策：**
- 健康分检查在所有现有检查之后、dispatched 标记之前
- `is_healthy()` 返回 False 时直接返回 None（不认领），worker 端会继续轮询或等待
- 使用 `is_healthy()` 而非 `get_health_score()`，避免每个 claim 请求都做完整计算（is_healthy 有 60s 内存缓存）

### 2.4 防饿死机制

**问题：** 如果所有候选引擎健康分都低，任务会一直 queued 没人认领。

**方案：** 在现有的 `_check_pool_engines_reachable` 逻辑中增加健康分维度。当 pool 中所有引擎都健康分过低时，视为"池不可达"，清空 `pool_engine_ids` 让任意引擎认领（包括低健康分的，至少能尝试执行）：

```python
# service.py _check_pool_engines_reachable() 增强
def _check_pool_engines_reachable(engine_ids, tag, repo_key, task_user_id):
    # ... 现有检查（status/healthy/probe/tag/scope/slots）...
    
    # 新增：检查是否有健康分达标的引擎
    from src.runtime.health_scorer import is_healthy as _is_engine_healthy
    _has_healthy = False
    for eid in engine_ids:
        if _is_engine_healthy(eid):
            _has_healthy = True
            break
    
    if not _has_healthy:
        # 池中引擎全部健康分过低 → 清空 pool_engine_ids 放行
        log.info(f"pool_engine_ids {engine_ids} all low health score, clearing for task {task_id}")
        return False  # 触发清空逻辑
```

**但更精确的做法是分层处理：**

| 场景 | 处理方式 |
|------|---------|
| 池中有健康引擎 | 正常认领，低分引擎被挡 |
| 池中全不健康但引擎在线 | 等待 N 秒后降级，允许低分引擎认领（防饿死） |
| 池中全不健康且引擎离线 | 清空 pool_engine_ids 广播（现有逻辑） |

### 2.5 健康分计算优化（配合本方案）

当前 `consecutive_failures` 的计算有个问题：引擎 1079 总任务 60 个，连续失败 32 次，但因为总基数大，失败率只有 6.7%，健康分仍为 ~93。连续失败 32 次应该直接判定不健康。

**优化：** `is_healthy()` 已经有 `consecutive_failures >= _MAX_CONSECUTIVE_FAILURES（默认 5）` 的检查，但当前连续失败的计算逻辑可能有 bug（1079 连续失败 32 次但仍在认领任务说明 is_healthy 没在 plugin 模式生效）。

**修复：** 确认 `consecutive_failures` 计算正确后，本方案会让 `is_healthy()` 在 claim_by_tag 中生效，连续失败 >= 5 次的引擎自动停止认领。

---

## 三、代码修改清单

### 3.1 service.py — claim_by_tag 增加健康分检查

文件：`src/runtime/service.py`，函数 `claim_by_tag`

修改位置：在 `# 标记 dispatched` 之前（约第 1060 行）

```python
# === 健康分检查（Plugin 模式增强层，Issue #134 扩展）===
# 引擎健康分过低或连续失败过多时拒绝认领，避免低质量引擎抢任务后失败
try:
    from src.runtime.health_scorer import is_healthy as _is_engine_healthy
    if not _is_engine_healthy(runtime_id):
        log.info(f"claim_by_tag: engine {runtime_id} unhealthy (low score/consecutive failures), skip claiming task {task_id}")
        return None
except Exception as _he:
    log.warning(f"claim_by_tag: health check failed: {_he}, skip check")
    # 健康分模块异常时不阻塞认领（降级为原有行为）

# 标记 dispatched（原有代码）
c.execute(
    "UPDATE agent_tasks SET status='dispatched', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
    ...
)
```

### 3.2 service.py — _check_pool_engines_reachable 增加健康分维度

文件：`src/runtime/service.py`，函数 `_check_pool_engines_reachable`

修改位置：在现有检查循环中增加健康分检查

```python
def _check_pool_engines_reachable(engine_ids, tag, repo_key, task_user_id):
    # ... 现有检查 ...
    _has_healthy = False
    for eid in engine_ids:
        # ... 现有 status/healthy/probe/tag/scope/slots 检查 ...
        if _all_existing_checks_pass:
            # 新增：健康分检查
            try:
                from src.runtime.health_scorer import is_healthy as _is_engine_healthy
                if _is_engine_healthy(eid):
                    _has_healthy = True
                # 注意：即使健康分低也不断言 reachable
                # 因为现有检查已通过说明引擎"物理可达"
                # 健康分低只是质量差，不是不可达
            except Exception:
                _has_healthy = True  # 模块异常时不影响可达性判定
    
    # 如果所有引擎物理可达但健康分全低 → 仍然返回 True（不清空 pool）
    # 但 claim_by_tag 中的健康分检查会阻止它们认领
    # 防饿死由下方的超时降级机制处理
    return _has_reachable
```

### 3.3 防饿死：queued 任务超时降级

文件：`src/runtime/service.py`，新增函数

```python
def _check_stale_queued_tasks():
    """定期检查 queued 超时的任务，降级清空 pool_engine_ids 放行。
    
    当任务 queued 超过 STALE_QUEUED_THRESHOLD（默认 120 秒）时，
    可能是因为所有候选引擎健康分过低导致无人认领。
    此时清空 pool_engine_ids，让任意在线引擎认领（包括低健康分的）。
    """
    STALE_THRESHOLD = 120  # 秒
    try:
        with db_lock:
            c = db.cursor()
            c.execute("""
                UPDATE agent_tasks 
                SET pool_engine_ids='' 
                WHERE status='queued' 
                  AND pool_engine_ids!='' 
                  AND created_at <= datetime('now', ?)
                """, (f"-{STALE_THRESHOLD} seconds",))
            recovered = c.rowcount
            db.commit()
            if recovered:
                log.info(f"stale queued: cleared pool_engine_ids for {recovered} tasks (no healthy engine claimed within {STALE_THRESHOLD}s)")
    except Exception as e:
        log.warning(f"_check_stale_queued_tasks failed: {e}")
```

在现有的 `recover_stale_dispatched` 调用点旁边调用此函数（通常在 heartbeat / register 时触发）。

---

## 四、影响分析

### 4.1 正面影响

| 指标 | 预期改善 |
|------|---------|
| 任务首次成功率 | 提升 20-30%（低分引擎不再抢任务） |
| 平均任务完成时间 | 减少 30-50%（减少失败重试等待） |
| 无效资源消耗 | 降低（低分引擎不再消耗 API 调用配额） |
| 卡死/超时任务 | 减少（避免低分引擎认领后卡住） |

### 4.2 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 所有引擎健康分都低时任务饿死 | queued 超时 120 秒后降级清空 pool_engine_ids |
| 健康分计算延迟导致误判 | is_healthy 有 60s 缓存 + 任务终态即时刷新 |
| 健康分模块异常导致全部拒绝 | try/except 降级为原有行为（不检查健康分） |
| 引擎临时波动被误判不健康 | 连续失败阈值默认 5 次，允许偶尔失败 |

---

## 五、验证方案

### 5.1 单元验证

1. 模拟一个健康分 = 0 的引擎调用 `claim_by_tag`，确认返回 None
2. 模拟一个健康分 = 100 的引擎调用 `claim_by_tag`，确认正常认领
3. 模拟所有 pool 引擎健康分 = 0，确认 120 秒后 pool_engine_ids 被清空

### 5.2 线上验证

1. 观察引擎 2631/2632/2634/2630/2619（当前健康分应为 0）是否停止认领新任务
2. 观察 failed 任务数量是否下降
3. 观察 queued 任务的平均等待时间是否在合理范围内（< 120 秒）
4. 确认健康分恢复后的引擎能自动恢复认领

---

## 六、实施计划

| 步骤 | 内容 | 预计工作量 |
|------|------|-----------|
| 1 | claim_by_tag 增加健康分检查 | 1h |
| 2 | _check_pool_engines_reachable 增加健康分维度 | 1h |
| 3 | queued 超时降级机制 | 1h |
| 4 | 健康分计算逻辑验证（consecutive_failures 修复） | 1h |
| 5 | 线上灰度验证 | 2h |
| 合计 | | 6h |
