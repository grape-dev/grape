# Spec: obs_runtime_samples 单写者租约（Issue #702）

> 开发分支 `feat/issue-702` · 基线 `local-liufei` · 上游 Spec：Issue #702 评论 197674249

## Objective

为设备资源采样引入**全局单写者互斥**：四主控独立运行 `_obs_sample_loop()` 导致同一分钟同 runtime 入库 4 条（日增 ~107 万行 = 设计容量 4 倍）。收敛为：**全局仅一台主控（租约持有者）执行采样 + 过期清理**，持锁者失联（lease 过期）后其余主控下一轮自动抢占接管。

成功标准（部署后可独立验证）：
- 24h 增量 ≈ 27 万行/天（192 runtime × 1440 min）
- 任意一分钟同 runtime_id 至多 1 条
- 停持锁主控 10min 内接管、曲线无断点
- 前端资源曲线与 SLO 不受影响

## 关键决策（相对上游 Spec 的落地选择）

| 决策 | 上游选项 | 落地选择 | 理由 |
|------|---------|---------|------|
| 锁实现 | GET_LOCK 优先 / 心跳登记表 lease 兜底 | **distributed_locks 表 TTL lease**（复用 `src/lock.py`） | ① 仓库既有原语，`db_setup` 建表、SQLite/MySQL 统一语义（测试后端强制 sqlite，GET_LOCK 不可测）；② GET_LOCK 在本仓库有池化连接持命名锁泄漏→重启卡 210s 的事故记录（`src/mysql_compat.py:841`），需专用连接+池外管理，复杂度反而更高；③ 语义即「心跳登记表 lease」，与上游兜底方案同形态 |
| 锁粒度 | 全局单锁 / per-runtime | 全局单锁 `obs_runtime_sampler` | 采样是批量循环，per-runtime 开销无必要 |
| TTL | ≤ 60s | `SAMPLE_LEASE_TTL = 60`（= 一轮采样周期） | 失联后至多 60s + 一轮周期即可接管，远优于 10min 要求 |
| 续期 | 持有者续期 | 每轮采样前 `acquire`（内部含「未过期且是自己 → 续期」语义） | `acquire_lock` 幂等：持有者重复 acquire 即续期（UPDATE 路径），无需独立 renew 分支 |
| 接管 | 失联后抢占 | 非持有者每轮 acquire 失败即跳过；lease 过期后自然抢到 | 无需「检查 + 抢占」两步，单原语闭环 |
| 清理绑定 | 与采样锁同锁 | **（A）** DELETE 清理移出 `snapshot_runtimes()`，仅持锁分支调用新的 `cleanup_expired_samples()` | 消除「多主各自清理」4 倍放大 |
| maybe_cleanup | 与采样锁解耦 | **（B）** 日志留存清理保持锁外（每轮照旧） | 上游 Spec Out of Scope：按小时限流，不误伤其节奏 |
| loops 埋点 | — | 采样循环埋 `obs_sample`（照旧 beat）；新增 `obs_sample_skip` 打点 | 接管静默期可观测（谁在跳过、跳了多少轮） |

## 改动锚点

| 文件 | 改动 |
|------|------|
| `src/observability/sample_lease.py`（新增） | 租约封装：`acquire_lease() / release_lease() / lease_status()`，含锁失败降级语义（锁表异常 → fail-open，采样暂停但读侧不受影响） |
| `src/observability/rollup.py` | `snapshot_runtimes()` 拆分：纯采样；新增 `cleanup_expired_samples()`（DELETE 留存清理，从 snapshot 中移出） |
| `src/api/main.py` | `_obs_sample_loop()` 重构：每轮先抢租约 → 抢到才采样+清理，未抢到跳过写入 |
| `tests/test_obs_sample_lease.py`（新增） | 覆盖：锁获取/互斥/续期/失联接管/清理绑定/降级 |

## 测试策略

- 框架：pytest + 仓库统一 SQLite 测试库（`tests/conftest.py` 隔离）
- 路径：`tests/test_obs_sample_lease.py`（与既有 `tests/test_obs_rollup.py` 同风格）
- **测试 seam（公开接口）**：`sample_lease.acquire_lease / release_lease / lease_status`、`rollup.snapshot_runtimes / cleanup_expired_samples`——不测 `main.py` 循环内部（异步循环已由 loops 埋点覆盖其健康面）
- 多主模拟：直接操作 `distributed_locks` 行（伪造另一 holder / 回拨 expires_at）模拟「他主持有 / 失联过期」，避免真起多进程
- TDD：先提交失败测试（红），再提交实现（绿）

## Boundaries

- Always：红→绿分提交；每提交原子；跑通相关测试后提交
- Ask first：无（不动 schema，复用既有 `distributed_locks` 表）
- Never：不改 `runtime_history()` 读侧；不动 `maybe_cleanup` 节奏；不做历史数据回溯清理；不直推基线分支

## Success Criteria

1. `pytest tests/test_obs_sample_lease.py tests/test_obs_rollup.py tests/atomic -q` 全绿
2. 「多主模拟」用例断言：同一分钟同 runtime_id 仅 1 条 INSERT（模拟第二主控 acquire 失败跳过）
3. 失联接管用例：回拨 expires_at 过期后，另一 holder 可 acquire 成功
4. 清理绑定用例：`snapshot_runtimes()` 不再执行 DELETE；DELETE 仅在 `cleanup_expired_samples()` 且由持锁分支调用
