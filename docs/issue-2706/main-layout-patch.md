# main 布局同型修复补丁（Issue #2706，PR base=arch-weekly-11-zj 不跨分支改码，本文档为 main 合入时的执行依据）

线上 #7415 的实测形态正是 **main 布局**：`/opt/grape/src/runtime/branch_guard.py:252` 的
`from src import lock`（三处：235/252/275）在部署树缺 `src/lock.py` 时抛
`ImportError: cannot import name 'lock' from 'src'`，所有 `grape git push` 被阻断。
本分支 PR 只改 arch-weekly-11-zj；main 线合入时按以下同型修复执行。

## 1. `src/runtime/branch_guard.py`（main 布局）

与 arch-weekly-11 分支 PR 的改法完全同型，仅模块名不同（`src.lock` 替代 `src.kernel.lock`）：

### 1.1 常量区（`HOOK_MARKER` 附近）新增

```python
# Issue #2706（第 8 次同类复发终修）：租约 lock 导入的单一收敛点与降级语义。
LEASE_LOCK_MODULE = "src.lock"
LEASE_DEGRADED_REASON = "guard_lock_unavailable"
```

### 1.2 新增收敛点（`load_lease_state` 之前）

```python
def _warn_lease_fallback(exc: Exception) -> None:
    """租约降级告警（Issue #2706）：可定位（模块名 + 异常）+ 可执行修复指引。"""
    reason = f"branch lease lock module unavailable: {LEASE_LOCK_MODULE} " \
             f"({exc.__class__.__name__}: {exc})"
    sys.stderr.write(f"\n[branch-guard] WARNING {reason}；本次按无租约放行（fail-open）\n")
    sys.stderr.write(f"[branch-guard] WARNING 修复指引: {HOOK_REPAIR_HINT}\n")
    try:
        log.warning(f"branch guard lease fallback (fail-open): {reason}；"
                    f"修复指引: {HOOK_REPAIR_HINT}")
    except Exception as e:
        # 日志通道不可用不吞已写出的 stderr 告警（静默 except 棘轮禁止 bare pass）
        sys.stderr.write(f"[branch-guard] WARNING lease fallback log write failed: {e}\n")


def _lock_module():
    """租约 lock 模块惰性导入 + fail-open 兜底（Issue #2706 终修收敛点）。"""
    try:
        return importlib.import_module(LEASE_LOCK_MODULE)
    except Exception as e:
        _warn_lease_fallback(e)
        return None
```

注意：main 布局的 `branch_guard.py` 尚无 `import importlib`，需在文件头补
`import importlib`；若无 `HOOK_REPAIR_HINT` 常量（main 模板较旧），补：

```python
HOOK_REPAIR_HINT = ("请重装/升级 worker 发行包（scripts/install-worker.sh）补齐 "
                    "src/lock.py 等关键模块后重试；仅在确认钩子守卫故障时"
                    "临时用 grape git push --no-verify 推送（分支租约/漂移校验会失效），"
                    "不可日常化使用，推送后必须尽快重装/升级 worker 包。")
```

### 1.3 三处租约函数同型改造

- `_lease_holder`（行 235 附近）：`from src import lock as _lock` →
  `_lock = _lock_module()`；`_lock is None` 时 `return None`；
- `acquire_branch_lease`（行 252 附近）：`from src import lock as _lock` →
  `_lock = _lock_module()`；`_lock is None` 时返回降级放行体
  `{"ok": True, "degraded": True, "lock_key": key, "holder": "", "reason": LEASE_DEGRADED_REASON, "message": "", "expected_remote_sha": <state>}`
  （state 照常走 `load_lease_state` / `record_remote_sha`，两者自身已 fail-open）；
- `release_branch_lease`（行 275 附近）：`from src import lock as _lock` →
  `_lock = _lock_module()`；`_lock is None` 时 `return`（打 WARNING 后静默，
  不向上抛——`_hook_main` 的 finally 不能被 release 异常顶掉真实拦截退出码）。

## 2. `src/worker_package.py`（main 布局 MANIFEST）

main 的 MANIFEST 含 `src/runtime/`（守卫入包）但**没有顶层 `src/lock.py` 条目**——
`src/runtime/` 覆盖不到顶层散模块，这正是 main 线坏包缺 lock 的根因。补一行：

```diff
 MANIFEST = [
     "src/__init__.py",
     "src/config.py",
+    # Issue #2706：pre-push branch_guard 租约路径依赖 src.lock（src/runtime/
+    # 覆盖不到顶层散模块）；漏列会让 worker 部署树缺 lock → 所有 push ImportError。
+    "src/lock.py",
     "src/repo_key.py",
```

## 3. `scripts/install-worker.sh`（main 布局冒烟）

main 的 `_smoke_check` 只校验 `import src.config; import src.runtime.agent`，
锁模块缺失（懒加载依赖）探测不到。对齐 #2292 在 arch-weekly-11 的做法：

```diff
-  if "$VENV/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.config; import src.runtime.agent" >/dev/null 2>&1; then
+  _smoke_imports="import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.config; import src.runtime.agent; import src.lock; import src.runtime.branch_guard"
+  if "$VENV/bin/python" -c "$_smoke_imports" >/dev/null 2>&1; then
     log "New code import smoke check passed"
     return 0
   else
-    _smoke_err="$("$VENV/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import src.config; import src.runtime.agent; import src.observability.otel" 2>&1 | tail -1)"
+    _smoke_err="$("$VENV/bin/python" -c "$_smoke_imports; import src.observability.otel" 2>&1 | tail -1)"
```

失败文案同步指认 `src/lock.py` / `src/runtime/branch_guard.py`。

## 4. 回归测试

main 合入时同型搬运 `tests/test_issue_2706_branch_guard_lease_fallback.py`
（`LOCK_MODULE = "src.lock"`，import 路径 `from src.runtime import branch_guard as bg`）。

## 5. 验收

与 Issue Spec 一致：模拟缺 `src/lock.py` 时 `grape git push` 正常放行、stderr 出现
可定位 WARNING、无 ImportError 上抛；正常环境租约 acquire/renew/release 语义不回归。
