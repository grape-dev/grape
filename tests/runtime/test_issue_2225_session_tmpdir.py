"""Issue #2225：会话级唯一暂存目录（注入/回退/并发隔离/清理）。"""
import logging
import os

from src.domains.runtime.engine import session_tmpdir as st
from src.domains.runtime.engine import cleanup as sweeper


def test_inject_env_only_declares_path(tmp_path):
    env = st.inject_session_tmpdir_env({}, "t_abc123", root=str(tmp_path))
    assert env[st.ENV_TASK_TMPDIR] == env[st.ENV_SESSION_DIR]
    assert "t_abc123" in env[st.ENV_TASK_TMPDIR]
    assert env[st.ENV_SESSION_KEY] == "t_abc123"
    # 平台侧只下发路径，不在服务端创建目录（worker 可能在远端）
    assert not os.path.exists(env[st.ENV_TASK_TMPDIR])


def test_ensure_creates_writable_session_dir(tmp_path):
    env = st.inject_session_tmpdir_env({}, "t_writable", root=str(tmp_path))
    out = st.ensure_session_tmpdir(env, "t_writable", root=str(tmp_path))
    path = out[st.ENV_TASK_TMPDIR]
    assert os.path.isdir(path)
    assert out["GRAPE_SESSION_TMPDIR_FALLBACK"] == "0"
    body = os.path.join(path, "body.md")
    with open(body, "w", encoding="utf-8") as f:
        f.write("issue-2225")
    with open(body, encoding="utf-8") as f:
        assert f.read() == "issue-2225"


def test_missing_vars_fall_back_to_worker_local_session_dir(tmp_path, caplog):
    # QA 复审：docstring 声明三档优先级，实现只落 mkdtemp 一档——补齐第二档后，
    # 平台变量缺失应落 worker 本地确定性会话目录（可被 sweeper 覆盖、按 key 清理）。
    caplog.set_level(logging.WARNING)
    out = st.ensure_session_tmpdir({}, "t_missing", root=str(tmp_path))
    assert out[st.ENV_TASK_TMPDIR] == os.path.join(str(tmp_path), "opencode", "t_missing")
    assert out["GRAPE_SESSION_TMPDIR_FALLBACK"] == "1"
    assert os.path.isdir(out[st.ENV_TASK_TMPDIR])
    assert any("missing" in r.message for r in caplog.records)


def test_unusable_worker_local_dir_falls_back_to_mktemp(tmp_path, caplog, monkeypatch):
    def _mkdtemp(*args, **kwargs):
        path = os.path.join(str(tmp_path), "opencode-fallback-x")
        os.makedirs(path, exist_ok=True)
        return path

    monkeypatch.setattr(st, "platform_tmpdir", lambda *a, **k: "")
    monkeypatch.setattr(st.tempfile, "mkdtemp", _mkdtemp)
    caplog.set_level(logging.WARNING)
    out = st.ensure_session_tmpdir({}, "t_missing", root=str(tmp_path))
    assert out[st.ENV_TASK_TMPDIR] == os.path.join(str(tmp_path), "opencode-fallback-x")
    assert out["GRAPE_SESSION_TMPDIR_FALLBACK"] == "1"
    assert os.path.isdir(out[st.ENV_TASK_TMPDIR])
    assert any("mktemp" in r.message for r in caplog.records)


def test_concurrent_sessions_do_not_overwrite(tmp_path):
    env1 = st.ensure_session_tmpdir(
        st.inject_session_tmpdir_env({}, "t_session_a", root=str(tmp_path)),
        "t_session_a", root=str(tmp_path))
    env2 = st.ensure_session_tmpdir(
        st.inject_session_tmpdir_env({}, "t_session_b", root=str(tmp_path)),
        "t_session_b", root=str(tmp_path))
    assert env1[st.ENV_TASK_TMPDIR] != env2[st.ENV_TASK_TMPDIR]

    p1 = os.path.join(env1[st.ENV_TASK_TMPDIR], "body.md")
    p2 = os.path.join(env2[st.ENV_TASK_TMPDIR], "body.md")
    with open(p1, "w", encoding="utf-8") as f:
        f.write("issue-2080")
    with open(p2, "w", encoding="utf-8") as f:
        f.write("issue-2085")
    with open(p1, encoding="utf-8") as f:
        assert f.read() == "issue-2080"
    with open(p2, encoding="utf-8") as f:
        assert f.read() == "issue-2085"


def test_cleanup_removes_own_session_dir_only(tmp_path):
    env = st.ensure_session_tmpdir(
        st.inject_session_tmpdir_env({}, "t_cleanup", root=str(tmp_path)),
        "t_cleanup", root=str(tmp_path))
    path = env[st.ENV_TASK_TMPDIR]
    assert st.cleanup_session_tmpdir(env, root=str(tmp_path)) is True
    assert not os.path.exists(path)

    keep = os.path.join(str(tmp_path), "keep-me")
    os.makedirs(keep, exist_ok=True)
    assert st.cleanup_session_tmpdir({st.ENV_TASK_TMPDIR: keep}, root=str(tmp_path)) is False
    assert os.path.isdir(keep)


def test_cleanup_by_key_covers_direct_path(tmp_path):
    # QA 复审 Nice-to-have：落点收敛到 tmp_path，不再在共享 worker 的真实暂存根建删目录。
    out = st.ensure_session_tmpdir({}, "d_direct", root=str(tmp_path))
    assert out[st.ENV_TASK_TMPDIR].startswith(str(tmp_path))
    assert os.path.isdir(out[st.ENV_TASK_TMPDIR])
    assert st.cleanup_session_tmpdir_by_key("d_direct", root=str(tmp_path)) is True
    assert not os.path.exists(out[st.ENV_TASK_TMPDIR])


def test_sweeper_cleans_legacy_flat_files_and_stale_session_dirs(tmp_path, monkeypatch):
    base = os.path.join(str(tmp_path), "opencode")
    os.makedirs(os.path.join(base, "t_stale"), exist_ok=True)
    # F6：会话目录归属标记——sweeper 只清理 grape 创建（带标记）的目录
    with open(os.path.join(base, "t_stale", ".grape-session-tmpdir"), "w") as f:
        f.write("")
    flat = os.path.join(base, "body.md")
    with open(flat, "w", encoding="utf-8") as f:
        f.write("stale")
    monkeypatch.setattr(sweeper, "_TASK_TMPDIR_TTL_SECONDS", 0)
    monkeypatch.setattr(sweeper, "_active_session_task_ids", lambda: set())
    # QA 复审 R3：扫描根必须收敛到 tmp_path——共享 worker 上真实 /tmp/opencode 里
    # 是其他会话的活跃暂存目录，直接调生产默认根会把它们删掉。
    import time
    cleaned = sweeper._sweep_opencode_tmpdirs(time.time(), roots=[base])
    assert cleaned >= 2
    assert not os.path.exists(flat)
    assert not os.path.exists(os.path.join(base, "t_stale"))


def test_sweeper_roots_override_confines_scan(tmp_path, monkeypatch):
    """QA 复审 R3：显式 roots 时只动该根，根外目录（含真实暂存根）不受影响。"""
    import time
    base = os.path.join(str(tmp_path), "opencode")
    outside = os.path.join(str(tmp_path), "outside")
    os.makedirs(os.path.join(base, "stale_a"), exist_ok=True)
    os.makedirs(os.path.join(outside, "stale_b"), exist_ok=True)
    for d in (os.path.join(base, "stale_a"), os.path.join(outside, "stale_b")):
        with open(os.path.join(d, ".grape-session-tmpdir"), "w") as f:
            f.write("")
    monkeypatch.setattr(sweeper, "_TASK_TMPDIR_TTL_SECONDS", 0)
    monkeypatch.setattr(sweeper, "_active_session_task_ids", lambda: set())
    monkeypatch.setattr(sweeper, "_opencode_staging_roots",
                        lambda: {os.path.join(str(tmp_path), "never-scan")})

    cleaned = sweeper._sweep_opencode_tmpdirs(time.time(), roots=[base])

    assert cleaned == 1
    assert not os.path.exists(os.path.join(base, "stale_a"))
    assert os.path.isdir(os.path.join(outside, "stale_b"))


def test_sweeper_skips_session_dir_of_running_task(tmp_path, monkeypatch):
    """QA 复审 Nice-to-have：超 TTL 但任务仍在跑 → 不清理其会话子目录。"""
    import time
    base = os.path.join(str(tmp_path), "opencode")
    running = os.path.join(base, "t_running")
    orphan = os.path.join(base, "t_orphan")
    os.makedirs(running, exist_ok=True)
    os.makedirs(orphan, exist_ok=True)
    for d in (running, orphan):
        with open(os.path.join(d, ".grape-session-tmpdir"), "w") as f:
            f.write("")
    monkeypatch.setattr(sweeper, "_TASK_TMPDIR_TTL_SECONDS", 0)
    monkeypatch.setattr(sweeper, "_active_session_task_ids", lambda: {"t_running"})

    cleaned = sweeper._sweep_opencode_tmpdirs(time.time(), roots=[base])

    assert cleaned == 1
    assert os.path.isdir(running)
    assert not os.path.exists(orphan)


def test_sweeper_empty_roots_scans_nothing(tmp_path, monkeypatch):
    """QA 复审 Nice-to-have：``roots=[]`` 表示「不扫任何根」，不得回落到生产扫描根。"""
    import time
    real = os.path.join(str(tmp_path), "opencode")
    os.makedirs(os.path.join(real, "stale_x"), exist_ok=True)
    with open(os.path.join(real, "stale_x", ".grape-session-tmpdir"), "w") as f:
        f.write("")
    monkeypatch.setattr(sweeper, "_TASK_TMPDIR_TTL_SECONDS", 0)
    monkeypatch.setattr(sweeper, "_active_session_task_ids", lambda: set())
    monkeypatch.setattr(sweeper, "_opencode_staging_roots", lambda: {real})

    cleaned = sweeper._sweep_opencode_tmpdirs(time.time(), roots=[])

    assert cleaned == 0
    assert os.path.isdir(os.path.join(real, "stale_x"))


def test_sweeper_spares_foreign_names_and_unmarked_dirs(tmp_path, monkeypatch):
    """QA round-2 F6：非约定文件名 / 无归属标记的目录超 TTL 也不得删除。"""
    import time
    base = os.path.join(str(tmp_path), "opencode")
    os.makedirs(base, exist_ok=True)
    # 第三方工具的文件与目录（非约定名、无 grape 标记）
    foreign_file = os.path.join(base, "cache.bin")
    with open(foreign_file, "w") as f:
        f.write("third-party data")
    foreign_dir = os.path.join(base, "other-tool-workdir")
    os.makedirs(foreign_dir, exist_ok=True)
    monkeypatch.setattr(sweeper, "_TASK_TMPDIR_TTL_SECONDS", 0)
    monkeypatch.setattr(sweeper, "_active_session_task_ids", lambda: set())
    sweeper._sweep_opencode_tmpdirs(time.time(), roots=[base])
    assert os.path.exists(foreign_file), "non-whitelisted flat file must be spared"
    assert os.path.isdir(foreign_dir), "unmarked directory must be spared"
    # 对照：约定名文件与带标记目录仍被清理
    flat = os.path.join(base, "req.json")
    with open(flat, "w") as f:
        f.write("stale")
    marked = os.path.join(base, "t_gone")
    os.makedirs(marked, exist_ok=True)
    with open(os.path.join(marked, ".grape-session-tmpdir"), "w") as f:
        f.write("")
    sweeper._sweep_opencode_tmpdirs(time.time(), roots=[base])
    assert not os.path.exists(flat)
    assert not os.path.isdir(marked)
