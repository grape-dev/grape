"""Issue #3742：分布式锁契约（消费 acquired / strict 模式 / TTL 单点化）。

症状：`run_with_distributed_lock` 未抢到锁时 `yield False`，但 5 处调用点都不接收
yielded 值，导致 4 实例部署下每周期各自跑一遍同步/清理；`skill_sync` 还出现
「前半段全量同步 4 遍、场景同步被跳过」与 58 条/日 ERROR 噪音。

这里锁住契约：
- 未抢到锁：非严格模式 yield False 不抛；strict=True 抛 LockNotAcquired。
- 两个 worker 并发进入 `_skill_sync_worker`，`sync_official_skills` 只执行 1 次。
- 全仓 `src/` 下每一处 `async with run_with_distributed_lock(...)` 都消费 yielded flag
  并以该 flag 守卫 with 主体（静态 AST 枚举，不限定文件清单）。
- `SKILL_SYNC_LOCK_TTL` 单点派生（worker CM / `_ensure_outer_lock` / 超时阈值）。
"""
import ast
import asyncio
import inspect
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from src.domains.runtime.storage import skill_sync as ss

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeDistributedLock:
    """单持有者语义的进程内锁桩（acquire 在 to_thread 中执行，需线程安全）。

    `renew_fail_from=n`：第 n 次及之后的 renew 返回 False（模拟锁被其它实例接管），
    用于验证「续期失败即中止本轮剩余步骤」。
    """

    def __init__(self, holder=None, renew_fail_from=None):
        self._guard = threading.Lock()
        self._holder = holder
        self.acquisitions = 0
        self.releases = 0
        self.renew_calls = 0
        self.renew_fail_from = renew_fail_from

    def acquire_lock(self, key, ttl=300):
        with self._guard:
            if self._holder is None:
                self._holder = key
                self.acquisitions += 1
                return True
            return False

    def renew_lock(self, key, ttl=300):
        with self._guard:
            self.renew_calls += 1
            if self.renew_fail_from is not None and self.renew_calls >= self.renew_fail_from:
                return False
            return self._holder == key

    def release_lock(self, key):
        with self._guard:
            if self._holder == key:
                self._holder = None
                self.releases += 1
            return True

    def cleanup_expired_locks(self):
        return 0


def _install_lock_stub(monkeypatch, fake):
    stub = types.ModuleType("src.kernel.lock")
    stub.acquire_lock = fake.acquire_lock
    stub.renew_lock = fake.renew_lock
    stub.release_lock = fake.release_lock
    stub.cleanup_expired_locks = fake.cleanup_expired_locks
    monkeypatch.setitem(sys.modules, "src.kernel.lock", stub)
    return stub


def test_strict_mode_raises_when_lock_held(monkeypatch):
    from src.domains.runtime.engine.task_lock import (
        LockNotAcquired,
        run_with_distributed_lock,
    )

    fake = _FakeDistributedLock(holder="skill_sync")
    _install_lock_stub(monkeypatch, fake)

    async def _run():
        async with run_with_distributed_lock("skill_sync", strict=True):
            raise AssertionError("body must not run when lock is held")

    with pytest.raises(LockNotAcquired):
        asyncio.run(_run())


def test_non_strict_mode_yields_false_without_raising(monkeypatch):
    from src.domains.runtime.engine.task_lock import run_with_distributed_lock

    fake = _FakeDistributedLock(holder="skill_sync")
    _install_lock_stub(monkeypatch, fake)

    seen = []

    async def _run():
        async with run_with_distributed_lock("skill_sync") as acquired:
            seen.append(acquired)

    asyncio.run(_run())
    assert seen == [False]


def test_two_workers_concurrently_call_sync_once(monkeypatch):
    """两个 worker 同时进入 `_skill_sync_worker`，同步主体只执行 1 次。"""
    calls = []

    async def _fake_sync():
        calls.append(1)
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(ss, "sync_official_skills", _fake_sync)
    fake = _FakeDistributedLock()
    _install_lock_stub(monkeypatch, fake)

    async def _run():
        workers = [
            asyncio.create_task(ss._skill_sync_worker(interval=0.01)),
            asyncio.create_task(ss._skill_sync_worker(interval=0.01)),
        ]
        await asyncio.sleep(0.2)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    asyncio.run(_run())
    assert len(calls) == 1, f"sync must run once, got {len(calls)}"


_LOCK_CM = "run_with_distributed_lock"


def _iter_lock_call_sites():
    """全仓枚举（AST）`async with run_with_distributed_lock(...)` 调用点。

    Issue #3742 复审要求：静态断言不能只扫固定文件清单，否则新增/遗漏的调用点
    不会被发现。这里按语法树遍历 `src/` 下所有 .py（注释、docstring 里的示例不算），
    返回 [(相对路径, 行号, 锁 key, 绑定的 flag 名, with 节点)]。
    """
    sites = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            for item in node.items:
                call = item.context_expr
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == _LOCK_CM):
                    continue
                key = (call.args[0].value
                       if call.args and isinstance(call.args[0], ast.Constant)
                       else "<dynamic>")
                flag = (item.optional_vars.id
                        if isinstance(item.optional_vars, ast.Name) else None)
                sites.append((rel, node.lineno, key, flag, node))
    return sites


def _is_guarded_body(node, flag):
    """with 主体的首条语句必须是以 flag 为条件的 if —— 即「消费 acquired」的守卫。"""
    if not node.body or not isinstance(node.body[0], ast.If):
        return False
    return any(isinstance(n, ast.Name) and n.id == flag for n in ast.walk(node.body[0].test))


def test_all_call_sites_consume_acquired():
    sites = _iter_lock_call_sites()
    expected = {
        ("src/domains/runtime/storage/skill_sync.py", "skill_sync"),
        ("src/domains/runtime/storage/plugin_sync.py", "plugin_sync"),
        ("src/domains/runtime/storage/upload_cleanup.py", "upload_cleanup"),
        ("src/domains/runtime/engine/scheduler.py", "autopilot_scheduler"),
        ("src/domains/runtime/engine/table_maintenance.py", "table_maintenance_tasks"),
        ("src/domains/runtime/engine/table_maintenance.py", "table_maintenance_messages"),
        ("src/api/main.py", "table_maintenance"),
        ("src/api/main.py", "table_maintenance_messages"),
        ("src/api/grape_server.py", "table_maintenance_messages"),
        ("src/api/feedback_issue_converter.py", "feedback_reconcile"),
        ("src/api/feedback_issue_converter.py", "feedback_status_sync"),
    }
    found = {(rel, key) for rel, _lineno, key, _flag, _node in sites}
    missing = expected - found
    assert not missing, f"lock call sites disappeared (contract regression): {sorted(missing)}"

    unguarded = [f"{rel}:{lineno} key={key}"
                 for rel, lineno, key, flag, node in sites
                 if flag is None or not _is_guarded_body(node, flag)]
    assert not unguarded, (
        "every run_with_distributed_lock call site must consume the yielded flag and "
        f"guard its body (CM does not skip the body): {unguarded}")


def _run_loop_briefly(monkeypatch, looper, holder, run_for=0.15):
    """在给定 holder 状态下跑一小段 loop，然后取消（loop 内含 interval 睡眠）。"""
    fake = _FakeDistributedLock(holder=holder)
    _install_lock_stub(monkeypatch, fake)

    async def _run():
        task = asyncio.create_task(looper())
        await asyncio.sleep(run_for)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_table_maintenance_tasks_skipped_when_lock_held(monkeypatch):
    """table_maintenance_tasks 锁被其它实例持有时不执行清理（正/反双向）。"""
    from src.domains.runtime.engine import table_maintenance as tm

    calls = []
    monkeypatch.setattr(tm, "_TABLE_MAINTENANCE_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(tm, "_TABLE_MAINTENANCE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(tm, "cleanup_old_agent_tasks_fields", lambda: calls.append("fields"))
    monkeypatch.setattr(tm, "delete_old_agent_tasks", lambda: calls.append("delete"))

    _run_loop_briefly(monkeypatch, tm.table_maintenance_tasks_loop,
                      holder="table_maintenance_tasks")
    assert calls == [], f"agent_tasks cleanup must be skipped while lock is held: {calls}"

    _run_loop_briefly(monkeypatch, tm.table_maintenance_tasks_loop, holder=None)
    assert "fields" in calls and "delete" in calls, "cleanup must run when the lock is free"


def test_table_maintenance_messages_skipped_when_lock_held(monkeypatch):
    """table_maintenance_messages 锁被其它实例持有时不执行清理（正/反双向）。"""
    from src.domains.runtime.engine import table_maintenance as tm

    calls = []
    monkeypatch.setattr(tm, "_TABLE_MAINTENANCE_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(tm, "_TABLE_MAINTENANCE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(tm, "cleanup_old_task_messages", lambda: calls.append("messages"))

    _run_loop_briefly(monkeypatch, tm.table_maintenance_messages_loop,
                      holder="table_maintenance_messages")
    assert calls == [], f"task_messages cleanup must be skipped while lock is held: {calls}"

    _run_loop_briefly(monkeypatch, tm.table_maintenance_messages_loop, holder=None)
    assert calls, "cleanup must run when the lock is free"


def test_plugin_sync_skipped_when_lock_held(monkeypatch):
    """plugin_sync worker 未持锁时不同步（否则每实例各拉一遍外部插件）。"""
    from src.domains.runtime.storage import plugin_sync as ps

    calls = []

    async def _fake_sync():
        calls.append(1)
        return {}

    monkeypatch.setattr(ps, "sync_official_plugins", _fake_sync)

    _run_loop_briefly(monkeypatch, lambda: ps._plugin_sync_worker(interval=0.01),
                      holder="plugin_sync")
    assert calls == [], "plugin sync must be skipped while the lock is held"

    _run_loop_briefly(monkeypatch, lambda: ps._plugin_sync_worker(interval=0.01), holder=None)
    assert calls, "plugin sync must run when the lock is free"


def test_feedback_reconcile_skipped_when_lock_held(monkeypatch):
    """feedback reconcile / status sync 未持锁时不得转换、不得拉外部状态。"""
    from src.api import feedback_issue_converter as fc

    calls = []
    monkeypatch.setattr(fc, "convert_pending_feedback",
                        lambda *a, **k: calls.append("convert"))
    monkeypatch.setattr(fc, "_sync_statuses", lambda *a, **k: calls.append("statuses"))

    _run_loop_briefly(monkeypatch, lambda: fc.feedback_reconcile_loop(interval=0.01),
                      holder="feedback_reconcile")
    assert calls == [], f"reconcile/status sync must be skipped while the lock is held: {calls}"

    _run_loop_briefly(monkeypatch, lambda: fc.feedback_reconcile_loop(interval=0.01), holder=None)
    assert "convert" in calls and "statuses" in calls, "both steps must run when locks are free"


def test_autopilot_scheduler_skipped_when_lock_held(monkeypatch):
    """autopilot 调度循环未持锁时不扫描任务、不派发（防重复派发）。"""
    from src.domains.runtime.engine import scheduler as sc

    calls = []
    monkeypatch.setattr(sc, "_SCHEDULER_INTERVAL", 0.01)
    monkeypatch.setattr(sc, "_get_enabled_tasks", lambda: calls.append("scan") or [])
    monkeypatch.setattr(sc, "_reap_zombie_executions", lambda: None)
    monkeypatch.setattr(sc, "_sweep_pending_self_report_timeouts", lambda: None)
    monkeypatch.setattr(sc, "_sweep_no_progress_tasks", lambda: None)

    _run_loop_briefly(monkeypatch, sc.scheduler_loop, holder="autopilot_scheduler")
    assert calls == [], "autopilot scan/dispatch must be skipped while the lock is held"

    _run_loop_briefly(monkeypatch, sc.scheduler_loop, holder=None)
    assert calls, "autopilot scan must run when the lock is free"


def test_skill_sync_ttl_single_source():
    # TTL 允许 env 覆盖（SKILL_SYNC_LOCK_TTL），断言「默认值 + 同源派生」而非把 600
    # 写死，避免运行环境设置了该 env 时误报。
    assert isinstance(ss.SKILL_SYNC_LOCK_TTL, int) and ss.SKILL_SYNC_LOCK_TTL > 0
    module_src = Path(ss.__file__).read_text(encoding="utf-8")
    assert re.search(
        r'SKILL_SYNC_LOCK_TTL = int\(os\.environ\.get\("SKILL_SYNC_LOCK_TTL", "600"\)\)',
        module_src,
    ), "TTL must keep env-overridable default 600"
    worker_src = inspect.getsource(ss._skill_sync_worker)
    assert "ttl=SKILL_SYNC_LOCK_TTL" in worker_src
    default_ttl = inspect.signature(ss._ensure_outer_lock).parameters["ttl"].default
    assert default_ttl == ss.SKILL_SYNC_LOCK_TTL
    cycle_src = inspect.getsource(ss.sync_official_skills)
    assert "SKILL_SYNC_LOCK_TTL" in cycle_src
    assert "900" not in cycle_src, "cycle threshold must derive from TTL, not hardcode 900"


def _install_sync_stubs(monkeypatch, tmp_path):
    """把 `sync_official_skills` 的外部依赖收窄为纯内存桩（不联网、不写 DB）。"""
    from src.domains.runtime.storage import scene_sync

    async def _no_download(client, token):
        raise RuntimeError("offline in test")

    monkeypatch.setattr(scene_sync, "_download_repo_zip_with_retry", _no_download)
    monkeypatch.setattr(scene_sync, "_get_sync_token", lambda: "")
    monkeypatch.setattr(ss, "CONFIG_DIR", str(tmp_path))  # 无 skill-description.json → 跳过 DB 步骤

    calls = {"obs": 0, "scene": 0}

    async def _fake_obs():
        calls["obs"] += 1
        return {"added": 0, "updated": 0, "skipped": 0}

    async def _fake_scenes(repo_root=None):
        calls["scene"] += 1
        return {}

    monkeypatch.setattr(ss, "sync_official_skills_to_obs", _fake_obs)
    monkeypatch.setattr(scene_sync, "sync_official_scenes", _fake_scenes)
    return calls


def test_obs_step_aborts_when_outer_lock_renew_fails(monkeypatch, tmp_path, caplog):
    """续期失败（锁已被其它实例接管）→ 不上传 OBS、不跑场景步骤，日志留 INFO。"""
    import logging

    fake = _FakeDistributedLock(holder="skill_sync", renew_fail_from=2)
    _install_lock_stub(monkeypatch, fake)
    calls = _install_sync_stubs(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO):
        asyncio.run(ss.sync_official_skills())

    assert fake.renew_calls >= 2, "must renew the outer lock before OBS upload"
    assert calls["obs"] == 0, "OBS upload must be skipped once the outer lock is lost"
    assert calls["scene"] == 0, "scene step must be skipped once the outer lock is lost"
    assert any("outer lock" in r.message for r in caplog.records)


def test_obs_step_runs_while_outer_lock_held(monkeypatch, tmp_path):
    """续期成功时 OBS 步骤照常执行（防「一律中止」式假绿）。"""
    fake = _FakeDistributedLock(holder="skill_sync")
    _install_lock_stub(monkeypatch, fake)
    calls = _install_sync_stubs(monkeypatch, tmp_path)

    asyncio.run(ss.sync_official_skills())

    assert fake.renew_calls >= 2
    assert calls["obs"] == 1
