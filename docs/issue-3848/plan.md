# Issue #3848 方案设计：启动期端口依赖工作收敛到 bind 之后

## 时序（改前 / 改后）

```
改前（缺陷）
  进程启动
    └─ import src.domains.mgmt.user.db
        └─ init_db()                        ← db.py:3831 模块导入期
            └─ _cleanup_usage_log_retention()
                └─ cleanup_expired_retention(force=True)
                    └─ _tokens.credential_usage_log_prune(...)   ← tokens 未绑 → PortNotBound
                        except Exception → log.warning("...cleanup failed") + return 0
  ...
  init_database() / lifespan → contracts_wiring.bind_all()        ← 端口此刻才绑

改后（收敛）
  进程启动
    └─ import src.domains.mgmt.user.db → init_db()
        └─ _cleanup_usage_log_retention() → defer_startup_retention_cleanup()
             ├─ tokens 已绑 → 立即跑一次（幂等闸）
             └─ tokens 未绑 → 什么都不做（钩子已在 credential_usage_log 导入时注册）
  ...
  bind_all() → _Registry.bind("tokens", ...)  → 锁外回调 on_bind 钩子
        └─ _on_port_bound("tokens") → 跑一次 force=True 清理（once 标志）
```

## 关键设计

### 1. `credential_usage_log`：异常分类 + deferred 初始化器

- 新增 `_PortNotBound` 导入（`src.kernel.ports._base`，kernel→kernel，无分层违规）。
- `cleanup_expired_retention`：
  - `except PortNotBound` → `log.debug("... deferred (kernel port not bound yet)")` + `return 0`
    ——**预期时序，不是故障**；不再污染 WARNING 巡检面。
  - `except Exception` → 保持 `log.warning("usage_log retention cleanup failed: ...")`。
  - 节流状态（`_cleanup_last_mono`）只在**真正执行成功**后刷新，未绑路径不推进节流。
- 新增：
  - `_on_port_bound(name)`：`on_bind` 钩子；只认 `"tokens"`（其余 8 个端口绑定与清理无关，直接返回）。
  - `_run_startup_prune_once()`：once 闸 + `cleanup_expired_retention(force=True)`，
    删除行数 > 0 时 `log.info("usage_log retention cleanup: N rows")`。
  - `defer_startup_retention_cleanup()`：启动链唯一入口（已绑 → 立即跑；未绑 → 等钩子）。
  - `reset_startup_prune_state()`：测试复位（once 标志 + 节流状态），供 `clear_bindings()` 后的用例复用。
- 注册方式沿用 `session_manager` 先例：模块导入期 `on_bind(_on_port_bound)` 注册一次
  （`_hook_registered` 闸防重复注册），并在 `defer_startup_retention_cleanup()` 里立即试跑一次
  ——「本模块早于装配导入」「晚于装配导入」两条路径都覆盖。

### 2. 启动调用点：布防而不是执行

`src/domains/mgmt/user/db.py::_cleanup_usage_log_retention` 只保留一条调用
（`defer_startup_retention_cleanup()`），docstring 更新为「布防」。四条启动路径
（init / worker / event / insight）因此一处生效，无需各自改 lifespan。

### 3. `card_sources`：端口未就绪 vs 真读失败

`PlatformCardSource.raws()` 顺序调整为：

```
try:  ready? → rows
except PortNotBound:  log.debug（端口未绑=启动窗口预期时序）+ startup_degraded(card_source, port_not_bound) + {}
except Exception:     log.warning("read failed, fallback to meta+file: ...") + startup_degraded(card_source, read_failed) + {}
```

- 端口在服务首个请求前已绑，正常路径不再进这两个分支；
- `degraded` 口径不变（只有契约来源设置 `degraded`），不引入新的装配降级语义。

### 4. `startup_degraded` 指标

新增 `src/kernel/startup_metrics.py`（口径与 `gsb/metrics.py`、`org_metrics.py` 一致）：

```python
def install(registry) -> None      # 幂等；未安装前 record 为 no-op
def record_startup_degraded(component: str, reason: str) -> None
```

- 指标：`grape_startup_degraded_total{component,reason}`（Counter）；
- `insight/observability/__init__.py` 启动时 `install(registry)`，`/metrics` 自动带出；
- 未启用 metrics（`METRICS_ADDR` 空 / worker 包无 prometheus_client）时零开销。

### 5. item 4（anyio 线程上限）——本分支不做

`src/kernel/db_pool_guard.py` 不存在于基线 `arch-weekly-11-ljt`（只存在于
`origin/arch-weekly-11` 的 #501 提交 `2e4c9465d`）。在本分支新建该模块等于与 #501 冲突，
且没有现成调用点可改；`_THREAD_ENV` 的取值（期望 24）由 #501 那条线负责。故本 Issue
只交付 1/2/3/5，PR 与 Issue 评论中明示该边界。

## 测试 seam

| 切片 | seam（公开接口/可观察行为） |
|------|--------------------------|
| 1 | `credential_usage_log.cleanup_expired_retention`（返回 0 + 日志级别）+ `_base._Registry.bind` 触发后的 `tokens.credential_usage_log_prune` 调用次数（spy） |
| 2 | `mgmt.user.db._cleanup_usage_log_retention()` 调用前后 prune 调用次数（未绑=0，绑后=1） |
| 3 | `card_sources.OwnerTableCardSource(read_rows=...)`（已存在的注入 seam）+ `caplog` 断言日志级别 + `startup_degraded` 计数 |
| 4 | `startup_metrics.install(fresh Registry)` 后 `record_*` 能从 registry 采样到 |

## 验收命令

```bash
python -m pytest tests/runtime/test_issue_3848_startup_order.py -q
python -m pytest tests/mgmt/test_n4_audit_coverage.py tests/runtime/test_platform_card_contract_failure.py \
    tests/runtime/test_issue_3632_platform_card_contract.py -q
python -m pytest tests/arch -q
```