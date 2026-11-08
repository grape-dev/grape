# Spec: Issue #1495 — task_messages 活跃度检查改用统一时间口径

> 来源：Issue #1495 官方 Spec（Issue 评论 [issue agent] Spec Document）| 评级 ready | 目标分支：`weekly-10`

## Objective
- 任务回收/回炉链路对 `task_messages` 的活跃度判定从「时间字符串逐字节比较」改为「解析为 epoch 后比较」，消除对写入格式的隐含依赖。
- 价值：避免活跃任务（长命令静默如 terraform apply、长编译）因时间格式漂移（空格格式行 `0x20<0x54` 恒早于 cutoff；无微秒行同秒 `0x2B<0x2E` 窗口塌陷）被误判死任务 → 误杀旧进程 + attempts+1 → 三次后强制 failed。

## Scope

### In Scope
- 新增 `src/runtime/task_activity.py`：统一活跃度判定 helper（入参 task_id + 窗口秒，内部解析 `created_at` 为 epoch 比较）+ 窗口常量单点定义（claim=600s、recover_orphans=1200s）。
- 改造两个调用点：`src/runtime/service.py`（claim 兜底回收）、`src/api/routes/runtime.py`（`recover_orphans`），全部改走 helper，不再各自拼 cutoff 字符串。
- 修正 `service.py` 活跃度检查处「最近 60s」错误注释。
- 新增/回归测试。

### Out of Scope
- 不修改 `task_messages.created_at` 表默认值（schema 迁移另立 follow-up）。
- 不统一写入侧时钟口径。
- 不调整 600s/1200s 窗口数值。
- 不在 SQL 中对 `created_at` 施加任何函数运算（`task_id` 索引必须保持有效，4.8M 行表不可退化）。

## Technical Approach

### 方案概述
helper 以 `task_id`（索引列）查询该任务消息行的 `created_at`，取回后在 Python 侧宽容解析为 epoch（对齐 `health_scorer.py` 解析约定 + `health.parse_dt` 双后端口径，兼容 ISO 微秒、ISO 无微秒、空格分隔三种现网形态），与 `now_utc - window` 比较判定活跃。

### 实现路径
1. 新建 `src/runtime/task_activity.py`：定义 `CLAIM_ACTIVITY_WINDOW_SECONDS = 600`、`RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS = 1200`；实现 `has_recent_activity(cursor, task_id, window_seconds) -> bool`：SQL 仅 `SELECT created_at FROM task_messages WHERE task_id=? ORDER BY id DESC LIMIT N`（索引友好），Python 逐行解析，任一行 epoch ≥ cutoff 即活跃；解析失败的行不计入活跃（维持「宁可误回收也不留死任务」既有取舍）。纯内存计算，无额外 IO，须在既有 `db_lock` 内被调用。
2. `service.py` claim 兜底处（现 :1335 附近）改调 helper，常量取自模块定义，注释修正为 600s。
3. `runtime.py` `recover_orphans`（现 :1149 附近）改调 helper。
4. 测试：两调用点 × {空格格式、无微秒 ISO} 用例（窗口内不回收/不判死）+ 真死任务仍回收 + 现有 recover/claim 回归。

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| helper 落位 | task_activity.py 新增 / 并入 health_scorer | 新增 task_activity.py | 职责单一，无命名冲突（已核验分支上不存在） |
| 窗口数值 | 统一为单值 / 保留 600+1200 两常量 | 保留两常量、单点定义 | 改窗口改变回收行为，超范围；「一处定义」指每值单点 |
| 解析失败行 | 计活跃 / 不计 | 不计（保守） | 延续既有「宁可误回收」取舍 |
| SQL 形态 | SQL 内函数运算 / 索引过滤+Python 解析 | 后者 | task_id 索引必须有效 |
| epoch 解析口径 | 自研 / 复用 health.parse_dt | 复用（`_epoch_of` 同源） | 双后端 naive 语义（mysql=CST/sqlite=UTC）已收敛，避免第三种口径 |

## Affected Modules

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| src/runtime/task_activity.py | 新增 | 活跃度 helper + 窗口常量 |
| src/runtime/service.py | 修改 | claim 兜底改走 helper + 注释修正 |
| src/api/routes/runtime.py | 修改 | recover_orphans 改走 helper |
| tests/test_issue_1495_task_activity_format.py | 新增 | 异形格式覆盖 + 回归 |

## Acceptance Criteria
- [ ] 新增用例覆盖「空格格式（表默认值形态）」「无微秒 ISO」两类 `task_messages` 行：活跃窗口内任务不回收、不判死；claim 与 recover_orphans 两个调用点各覆盖。
- [ ] `grep -rn "task_messages WHERE task_id=? AND created_at >" src/` 结果为 0。
- [ ] 600/1200 仅有 `task_activity.py` 一处定义；「最近 60s」注释已修正为 600s。
- [ ] 现有 recover/claim 回归全绿；真死任务（窗口内无任何可解析产出）仍在窗口后被回收。
- [ ] 两处判定仍在 `db_lock` 内完成，helper 无新增网络/磁盘 IO。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| claim 循环内逐任务解析的 CPU 开销 | Low | task_id 索引过滤，单任务消息行极少；仅解析最近 N 行 |
| 双后端（MySQL/SQLite）兼容 | Low | SQL 形态不变（仅 task_id 过滤），解析全在 Python |
| 行为回归（回收口径漂移） | Med | 窗口数值与判定语义不变；真死任务回收用例兜底 |
