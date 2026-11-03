# Plan: obs_runtime_samples 单写者租约（Issue #702）

## 实现顺序（依赖驱动）

### 切片 1（红→绿）：租约模块 `src/observability/sample_lease.py`
- 先写失败测试 `tests/test_obs_sample_lease.py::TestSampleLease`
  - acquire 首次成功；同进程重复 acquire 幂等续期
  - 他人持有未过期 → acquire False
  - 他人持有已过期（回拨 expires_at）→ acquire True（接管）
  - release 后他人可 acquire
  - lease_status 返回 holder/expires_at
  - 锁表异常（DROP distributed_locks）→ acquire 降级 False（fail-safe，不抛出）
- 实现：薄封装 `src/lock.py`（acquire_lock/release_lock），固定 key `obs_runtime_sampler`、TTL 60s

### 切片 2（红→绿）：清理职责拆分 `rollup.py`
- 失败测试：
  - `snapshot_runtimes()` 执行后，**超龄（8 天）样本仍存在**（不再顺带 DELETE）——现行为会清掉，红
  - `cleanup_expired_samples()` 单独调用后超龄样本被清、7 天内保留
- 实现：`snapshot_runtimes()` 移除 DELETE 段；新增 `cleanup_expired_samples() -> {deleted: n}`（复用 RETENTION_DAYS 与 db_lock 模式）

### 切片 3（红→绿）：多主单写语义（main.py 循环改造）
- 失败测试：模拟「他主持有锁」时执行一轮循环体逻辑 → 断言 0 INSERT、0 DELETE（不写不清）；持锁时正常采样
  - seam：将循环体抽为可测函数 `_run_sampling_tick()`（src/api/main.py 内定义，测试以 `main._run_sampling_tick` 导入——受 conftest app 测试保护，改用模块级函数避免事件循环）
  - 实测发现 main.py 导入重（FastAPI app），改为在 `src/observability/rollup.py` 暴露 `sample_once_under_lease()`：**抢租约 → 采样+清理 / 未抢到跳过**，返回 dict（inserted/deleted/leased/skipped）。main.py 循环只调它——循环体薄到不值得单测，逻辑全在可测函数里
- 实现：
  - `rollup.sample_once_under_lease()`（编排：acquire_lease → snapshot_runtimes + cleanup_expired_samples → 不主动 release【长持有，靠 TTL 续期，异常退出自然过期被接管】）
  - `main.py _obs_sample_loop()`：每轮 `await asyncio.to_thread(_obs_rollup.sample_once_under_lease)`，skip 时 `loops` 打点 `obs_sample_skip`

### 切片 4：回归 + 文档
- 跑 `tests/test_obs_sample_lease.py tests/test_obs_rollup.py tests/atomic -q` 及 obs 相关全量
- docs/issue-702/plan.md / task.md / spec.md 随 PR 提交

## 验证检查点
- 每切片 GREEN 后才进下一片
- 最终：全 obs 域测试 + atomic 套件绿

## 风险与缓解
- 锁表被外部 DROP/损坏 → acquire 降级 False（采样暂停、不放大写入；读侧无损）——切片 1 测试覆盖
- 持锁主控慢一轮（>60s TTL）→ 下一轮 acquire 重新抢（幂等），最坏抖动一轮；TTL=60s 足够（采样实测毫秒级）
- 网络分区下双主同时认为持有 → distributed_locks 单行 UPDATE 原子性 + holder 校验兜底（仓库既有语义）
