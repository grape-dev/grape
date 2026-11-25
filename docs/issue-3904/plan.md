# Issue #3904 方案设计（Plan）

## 1. 同源清单（核心改动，`agents/hermes_acp.py`）

新增两个模块级常量，构建与自检都从这里取，禁止再各自硬编码：

```python
_OVERLAY_NOT_MIRRORED = frozenset({
    "state.db", "state.db-wal", "state.db-shm",
    "config.yaml", ".env", "active_profile",
    "gateway.sock", "gateway.sock.path", "gateway.lock",
    "gateway.pid", "gateway_state.json", "gateway-starts.log",
})
_OVERLAY_REQUIRED_ENTRIES = ("tools", "installs", "skills", "plugins", "auth", "profiles")
```

- 构建侧：`_build_overlay_home` 用 `_OVERLAY_NOT_MIRRORED` 跳过（原内联跳过元组 +
  网关控制文件元组合并为一个常量）。
- 自检侧：`_overlay_unusable_reasons(iso_home, src_home)` 遍历
  `_OVERLAY_REQUIRED_ENTRIES`，**先剔除 `_OVERLAY_NOT_MIRRORED` 中的条目**再判
  「源有则目标必有且非空」；`_overlay_looks_usable()` 退化为
  `not _overlay_unusable_reasons(...)`，签名与返回值保持不变（既有用例
  `test_worker_local_schema_and_probe.py::test_overlay_looks_usable_detects_missing_runtime_dir`
  与 `tests/runtime/test_issue_3511_windows_worker_fixes.py` 的可调用性断言零回归）。

`profiles` 的落点：加入 `_OVERLAY_READONLY_SYMLINK_ENTRIES` → 构建走只读资产三梯次
（Linux symlink / Windows junction / 都失败时有界递归复制）。为什么不建真实空目录：
自检对「源非空而目标为空」也判不可用，空目录只会把 bug 从 `missing` 变成 `empty`。

## 2. 回退可诊断 + 回退计数

- `_build_overlay_home` 返回 `list[str]` 构建备注（如
  `"skills:tier=mirror(...)"`、`"<entry>:build_failed(...)"`）。成功降级复制也留痕，
  便于区分「正常资产共享」与「本该 symlink 却退到复制」。
- 回退分支日志单行结构化，保留原有 `falling back to shared HERMES_HOME` 文案前缀便于
  既有 Loki 查询继续命中：

```
ACP_OVERLAY_FALLBACK total=3 tier=mirror reasons=profiles:missing;tools:missing build_notes=... — hermes ACP: overlay home unusable — falling back to shared HERMES_HOME=... (hermes tasks serialized)
```

- `tier` 取备注里出现的最深梯次（`symlink` < `junction` < `mirror`），无备注时 `-`。
- `overlay_fallback_total()` / `_record_overlay_fallback()`：模块级累计计数 + 锁，
  供日志计数与用例断言。
- 可用但有降级备注（例如仅 `mirror`）时打一条 `ACP_OVERLAY_DEGRADED` INFO。

## 3. 回退感知的用量采集（`engine/usage_collect.py`）

- `hermes_acp` 在真正回退时打标 `_mark_shared_home_fallback(cwd)`，暴露
  `shared_home_fallback_cwds()`（进程内集合 + 同一把锁）。
- 新增 `_hermes_state_db_paths(cwd)`：默认只有 overlay 候选；cwd 在回退标记集合里
  时追加 `$HERMES_HOME/state.db`。**不做无条件回退**——overlay 缺失也可能只是
  「本任务没跑过 hermes」，无条件读共享账本会把共享库里别的历史累计整段当成本次
  用量（重复计量），而按标记精确回退不会。
- `_hermes_rows()` 按候选顺序取第一个存在的库；都不存在 → `log.debug`（不再 WARNING）。
- 命中共享 home 时 `log.info` 记录 `usage_collect: hermes ledger from shared home`
  （回退事实可见，但不刷 WARNING）。
- `_open_sqlite_readonly` 的复制失败分支：`FileNotFoundError`（文件在探测与复制之间消失，
  即回退删 overlay 的竞态）降为 `log.info`；其它异常保留 `log.warning`。

## 4. 测试（TDD：先写失败用例）

新增 `tests/runtime/test_issue_3904_overlay_profiles_policy.py`：

1. `test_required_and_not_mirrored_are_disjoint` — 漂移守卫；
2. `test_profiles_present_in_src_builds_usable_overlay` — 核心复现（修复前必须 RED：
   `profiles` 被跳过 → 自检 False）；
3. `test_missing_required_entry_reports_reason` — 回退原因可诊断；
4. `test_runtime_and_install_missing_still_unusable` — fail-closed 不退化；
5. `test_overlay_fallback_counter_increments`；
6. `test_usage_collect_falls_back_to_shared_home_state_db` — 共享 home 账本可读；
7. `test_usage_collect_missing_ledger_is_silent` — `caplog` 断言无 WARNING（INFO/DEBUG 允许）。

## 5. 影响面（机械检索）

- `grep -rn "_build_overlay_home|_overlay_looks_usable|_hermes_iso_home"`：
  - `src/domains/runtime/agents/hermes_acp.py`（实现 + 唯一调用点 `stream_execute`）；
  - `tests/api/test_hermes_acp_overlay_gateway_ctl.py`、`tests/runtime/test_hermes_overlay_home_439.py`、
    `tests/runtime/test_worker_local_schema_and_probe.py`、`tests/runtime/test_issue_3511_windows_worker_fixes.py`；
  - `src/domains/runtime/engine/usage_collect.py`（路径解析）。
- `agents/sandbox/overlay.py::build_overlay_home` 有同样的 `profiles` 跳过项，但 `grep`
  确认**当前无调用方**（仅保留兼容），本条不改：它没有配套自检，不构成互斥。
- `config.yaml` 覆写、`.env` 处理、网关控制文件清理逻辑不变。

## 6. 风险

- `profiles` 以 symlink 进 per-task home：引擎若在该目录写 profile 级状态，会落到共享
  home。历史 `skills`/`plugins`（同样可能被写）已是该策略；per-task 隔离的关键是
  `state.db`/`cron`/`sessions` 等可写目录，`profiles` 属身份/配置资产。
  Windows 无 symlink 特权时走 junction（同一语义），再退复制（tier 留痕）。
- 不新增 `runtime_credentials` 指标列：`202611240006` 已盖章，往 `COLUMNS` 里加列在存量库
  不会生效（只对新库生效），会让心跳 UPDATE 撞 `Unknown column` 整条失败。计数器改走日志
  计数 + 进程内累计，达到「回退次数应为告警指标」的目的（Loki 侧 `|= "ACP_OVERLAY_FALLBACK"`）。
- 共享 `state.db` 已 1.03GB：修复只止血（不再新增会话涌入），历史膨胀仍需与 #3773 一并
  做一次归档清理（运维动作，不在本条代码范围）。