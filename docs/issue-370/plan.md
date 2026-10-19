# Plan: Issue #370 — 实施方案设计

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/api/routes/runtime.py` | `recover_orphans` 增加 `RECOVER_GRACE_SECONDS` 常量、SELECT 扩展 `started_at/created_at`、循环内年龄闸门（早于活跃度检查）、响应增加 `skipped_young` |
| `tests/test_recover_orphans_slots.py` | 扩展 `_seed` 支持 `started_seconds_ago`；新增 UT-4/UT-5/UT-6 三条用例 |

## 实现细节

### 1. 常量定义（对齐 `RECOVER_ACTIVITY_WINDOW` 风格）

```python
RECOVER_GRACE_SECONDS = int(os.environ.get("RECOVER_GRACE_SECONDS", "300"))
```

### 2. SELECT 扩展

```sql
SELECT id, attempts, runtime_id, started_at, created_at
FROM agent_tasks WHERE runtime_id=? AND status IN ('dispatched','running')
```

### 3. 年龄闸门（在活跃度检查之前）

```python
_now = datetime.now(timezone.utc)
_grace_cutoff_dt = _now - timedelta(seconds=RECOVER_GRACE_SECONDS)
...
for tid, attempts, rid, started_at, created_at in rows:
    age_base = started_at or created_at
    if age_base:
        try:
            _age_dt = datetime.fromisoformat(age_base)
            if _age_dt.tzinfo is None:
                _age_dt = _age_dt.replace(tzinfo=timezone.utc)
            if _age_dt > _grace_cutoff_dt:
                skipped_young.append(tid)
                continue
        except Exception:
            pass  # 解析失败保守跳过闸门，继续后续活跃度检查（保持回收行为）
    # 活跃度检查（原有逻辑）...
```

### 4. 响应字段

返回 dict 增加 `skipped_young` 列表；log.info 增加 `skipped_young=` 段。

## 测试矩阵

| 用例 | started_at | created_at | 消息 | grace | 预期 |
|------|-----------|-----------|------|-------|------|
| UT-1 force-fail | 600s ago | 600s ago | 无 | 300 | attempts+1=3 → failed |
| UT-2 requeue | 600s ago | 600s ago | 无 | 300 | queued + runtime_id=0 + pool_agent_type 回填 |
| UT-3 active | 600s ago | 600s ago | 30s ago | 300 | skipped_active |
| UT-4 young | 30s ago | 40s ago | 无 | 300 | skipped_young，attempts 不变 |
| UT-5 NULL→created | NULL | 30s ago | 无 | 300 | skipped_young（兜底 created_at） |
| UT-6 env override | 30s ago | 40s ago | 无 | 60 | 30s > 60s? No → 超龄 → requeue |

UT-6 验证环境变量覆盖：grace=60s 时，30s 前的任务（< 60s）仍 skipped_young；
另有 grace=60s 时 90s 前任务（> 60s）走回收路径。

## 回滚开关

- `RECOVER_GRACE_SECONDS=0`：禁用宽限期（所有任务立即走原回收流程）
- 不设置：默认 300s

## 风险

- 改动集中在单函数 + 单 SELECT 扩展，影响面小
- 现有 UT-1/UT-2/UT-3 时间基准 600s > 300s grace，行为不变
