# Issue #3848 Spec：kernel port 未绑定即跑启动清理（启动次序收敛）

> 需求权威来源：Issue #3848 正文 + issue agent 已发布的 Spec（评论 202922454）。
> 目标合入分支：`arch-weekly-11-ljt`；开发分支：`feat/issue-3848`。

## 目标

把**依赖 kernel port 的启动期工作**收敛到端口绑定之后的唯一时序点：

1. 消除「每次启动必然空跑」的 `credential_usage_log` 保留期清理（`PortNotBound` 被
   `except Exception` 吞成 WARNING 并 `return 0`）；
2. 消除 `card_sources` 在启动窗口内的静默回退（端口未绑 → 回落 meta+file 的 WARNING）；
3. 启动降级从「只能 grep 日志」变成**可打点可巡检**（`startup_degraded`）。

成功标准（运维视角）：Loki 1h 内 `is not bound` / `retention cleanup failed` /
`repo_platforms table read failed` 从 35 / 17 / 16 条归零；启动期保留期清理**真的执行**
（删除行数非恒 0）；启动降级有指标可抓，不再依赖日志关键词。

## 范围

### In

1. `src/kernel/persistence/credential_usage_log.py`：`cleanup_expired_retention` 异常分类
   （`PortNotBound` → debug，其余保持 warning），并复用 `src/kernel/ports/_base.on_bind`
   注册一次性 deferred 初始化器（`bind("tokens")` 之后跑恰好一次 `force=True` 清理）。
2. 启动调用点（`src/domains/mgmt/user/db.py::_cleanup_usage_log_retention`）：不再在 bind
   之前直接跑清理，改为「布防」——一个调用点覆盖 init/worker/event/insight 四条启动路径。
3. `src/domains/runtime/vcs/card_sources.py`：`PortNotBound` 与真读失败**分流**
   （未就绪 → debug + `startup_degraded` 打点；真异常 → 保留 warning）并打点。
4. `startup_degraded` 指标：kernel 侧可安装收集器 + insight registry 安装（沿用
   `src/kernel/gsb/metrics.py` / `src/kernel/org_metrics.py` 口径）。

### Out

- 不改保留期 180 天 / 节流 30 天语义，不动 DB schema；
- 不重构 `contracts_wiring` 的绑定全集（不新增时序框架，复用现成 `on_bind`）；
- `db-pool-guard`（anyio 线程上限与池上限解耦）：**本基线不可实施**——证明见下文
  「基线核对：item 4 不在本分支代码上」；
- `#3651`（运行期 branch guard fail-open）同族不同场景，另单处理。

## 基线核对（编码前实测，2026-11-23）

| 事项 | 实测结论 |
|------|----------|
| 清理调用链 | `src/domains/mgmt/user/db.py:938` → `_cleanup_usage_log_retention()`（`:786`）→ `cleanup_expired_retention(force=True)`；`init_db()` 在 `db.py:3831` **模块导入期**执行 |
| bind 晚于清理 | 装配层 `bind_all()` 在 `db_setup.py:5578`，以及三个 server 的 lifespan（`grape_event_server.py:44`、`grape_worker_server.py:44`、`grape_insight_server.py:45`）——都晚于 `init_db()` 的导入期调用 |
| 卡片源 PortNotBound 来源 | `card_sources.py:190-200` 的 `except Exception` 吞掉的异常来自 `src/kernel/gsb/provider.py::_ensure_registered()` → `_pipeline.ensure_providers_registered()`（pipeline 端口未绑 → `PortNotBound`） |
| `on_bind` 时序 | `_Registry.bind()` 在**锁外**遍历 `_POST_BIND_HOOKS` 回调 port name（`_base.py:79-82`），已有先例 `session_manager._bootstrap_domain_schema`（注册 + 立即试跑一次，两条路径都覆盖） |
| 指标口径 | kernel 不能 import domains：指标收集器定义在 kernel，`insight/observability/__init__.py` 启动时 `install(registry)`；安装前 `observe()` 为 no-op |
| **item 4 基线缺失** | `src/kernel/db_pool_guard.py` **不存在**于 `arch-weekly-11-ljt`（`git ls-files` 无此文件，`grep -rn db-pool-guard` 零命中）；该文件只存在于 `origin/arch-weekly-11` / `-ch` / `-zj`（提交 `2e4c9465d`，Issue #501 的池僵死防护）。本分支若新建同名模块 = 与 #501 修复线冲突，且无调用点可改 |

## 验收标准

- [ ] 启动日志不再出现由 `kernel port 'tokens' / 'pipeline' is not bound` 触发的 WARNING（bind 前仅 debug）
- [ ] 单测：端口未绑时 `cleanup_expired_retention` 返回 0 且不告警；`bind()` 后钩子自动重跑，prune **恰好被调用 1 次**（`replace=True` 重绑不重复）
- [ ] 长无写入实例：仅靠启动钩子即完成保留期清理（删除行数可 >0，非恒 0）
- [ ] `card_sources` 端口未就绪时不再产生 `read failed, fallback to meta+file` WARNING，并产生 `startup_degraded{component="card_source"}` 打点
- [ ] `startup_degraded` 指标可注册进 registry（`grape_startup_degraded_total`），未安装时 record 为 no-op
- [ ] 现有测试全部通过 + 新增上述用例
- [ ] 不改保留期 / 节流语义与表结构

### 明确不在本分支交付（附证据与后续）

- **anyio 线程上限 24**：本基线无 `db_pool_guard`（见上表）。该项在 `arch-weekly-11`
  的 #501 修复里已有 `_apply_anyio_limiter/_THREAD_ENV`，本分支合入后由该处的
  `_THREAD_ENV` 取值负责；本 Issue 不做，PR 中明示，避免与 #501 线冲突。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 钩子在每次 port bind（9 个端口）都触发 | 重复 DELETE | 只认 `tokens` + once 标志；DELETE 条件幂等（`created_at < cutoff`）仅作兜底 |
| 清理被推迟到「首次请求」 | 审计滞后 | bind 完成即执行，不由请求驱动；且「布防即执行」分支覆盖已绑定的进程 |
| 测试 `clear_bindings()` 后 once 标志残留 | 用例串扰 | 提供 `reset_startup_prune_state()` 测试复位入口（测试 fixture 调用） |
| 指标在多请求路径重复累加 | 语义混淆 | 仅记录「端口未绑」（启动窗口）+「真读失败」两种 reason 的计数，按 reason 维度区分 |