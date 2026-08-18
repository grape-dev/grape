"""本机直连健康探测测试（local-direct-health-probe-design.md P01-P08 核心）。"""
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime.local_probe import probe_local_engines, LOCAL_PROBE_TIMEOUT


def test_probe_normal_cli_healthy():
    """P01：正常 CLI（python --version）→ healthy=True + version 非空"""
    eng = {"name": "testcli", "cli_path": sys.executable, "installed": True}
    out = probe_local_engines([eng])[0]
    assert out["healthy"] is True
    assert out["version"]


def test_probe_missing_cli_unhealthy():
    """P02：无 cli/未安装 → healthy=False，不抛错"""
    out = probe_local_engines([{"name": "x", "cli_path": "", "installed": False}])[0]
    assert out["healthy"] is False


def test_probe_nonzero_exit_unhealthy():
    """P08：CLI 返回非零 → healthy=False"""
    out = probe_local_engines([{"name": "x", "cli_path": sys.executable, "installed": True,
                                "probe_args": ["-c", "import sys; sys.exit(3)"]}])[0]
    assert out["healthy"] is False


def test_probe_hung_cli_timeout():
    """P03：CLI 挂起 → 5s 超时 → healthy=False"""
    eng = {"name": "hung", "cli_path": sys.executable, "installed": True,
           "probe_args": ["-c", "import time; time.sleep(60)"]}
    t0 = time.time()
    out = probe_local_engines([eng])[0]
    elapsed = time.time() - t0
    assert out["healthy"] is False
    assert elapsed < LOCAL_PROBE_TIMEOUT + 3


def test_probe_multiple_completes():
    """P04：多引擎并发探测均完成"""
    engs = [
        {"name": f"c{i}", "cli_path": sys.executable, "installed": True} for i in range(4)
    ]
    out = probe_local_engines(engs)
    assert len(out) == 4
    assert all(o["healthy"] for o in out)


def test_probe_save_writes_healthy_to_db():
    """P05：探测结果经 save_agent_runtimes 写入 DB 后 healthy=1"""
    from src.user import db as user_db
    from src.session_manager import db, db_lock
    name = "probe-test-local"
    with db_lock:
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name=?", (name,))
        db.commit()
    out = probe_local_engines([{"name": name, "cli_path": sys.executable, "installed": True,
                                "display_name": "ProbeTest"}])
    user_db.save_agent_runtimes(out)
    with db_lock:
        row = db.cursor().execute("SELECT healthy, version FROM agent_runtimes WHERE name=?", (name,)).fetchone()
    assert row and row[0] == 1 and row[1]
    with db_lock:
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name=?", (name,))
        db.commit()
