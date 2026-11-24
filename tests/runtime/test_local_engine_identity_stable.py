"""重部署不再新增本机引擎行：`engine_host()` 提供稳定身份（上游 Issue #13 / GitCode #2094）。

容器/Pod 每次重建 hostname 都会变，而 `host_name` 是本机引擎自然键的一部分
（`UNIQUE (credential_id, name, scope, owner_id, host_name)`，见 `src/domains/runtime/schema_ddl.py`）。
不固定它：每次重部署就多一条永远离线的引擎行 + 一条离线设备记录，场景节点绑定的引擎也会
停在旧行上（表现为「场景绑定的引擎全部离线」）。

托管镜像的 entrypoint 现在把身份固定成数据卷里的 `instance-id`（导出 `GRAPE_HOSTNAME`），
本用例锁定该契约：
  · 设了 `GRAPE_HOSTNAME` → 同一身份两次注册（模拟两次重部署）只留一行；
  · 不设时 hostname 一变就是两行 —— 反证线上「多一条离线」的成因。

跑法：.venv/bin/python -m pytest -q tests/runtime/test_local_engine_identity_stable.py
"""
import uuid

from src.domains.runtime import public as rt_public
from src.kernel.host_identity import engine_host
from src.kernel.session_manager import db, db_lock

NAME = "pytest-engine-identity"


def _agents():
    return [{"name": NAME, "display_name": NAME, "binary": NAME, "installed": 1,
             "healthy": 1, "version": "9.9.9", "cli_path": "/usr/bin/" + NAME}]


def _cleanup():
    with db_lock:
        c = db.cursor()
        for stmt in (
            "DELETE FROM engine_capabilities WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE name=?)",
            "DELETE FROM runtime_health_scores WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE name=?)",
            "DELETE FROM agent_runtimes WHERE name=?",
        ):
            try:
                c.execute(stmt, (NAME,))
            except Exception:  # noqa: BLE001 - 表可能不存在（拓扑差异）
                pass
        db.commit()


def _rows():
    with db_lock:
        return db.cursor().execute(
            "SELECT id, host_name FROM agent_runtimes WHERE name=? ORDER BY id",
            (NAME,)).fetchall()


def _register(host, ts):
    rt_public.upsert_local_runtimes(_agents(), "global", "", host, ts)


def test_engine_host_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("GRAPE_HOSTNAME", "inst-abc12345")
    assert engine_host() == "inst-abc12345"


def test_engine_host_falls_back_to_system_hostname(monkeypatch):
    monkeypatch.delenv("GRAPE_HOSTNAME", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "volatile-container-id")
    assert engine_host() == "volatile-container-id"


def test_stable_identity_reuses_one_row(monkeypatch):
    """同一稳定身份注册两次（模拟两次重部署）→ 只有一行。"""
    _cleanup()
    try:
        monkeypatch.setenv("GRAPE_HOSTNAME", "inst-" + uuid.uuid4().hex[:8])
        host = engine_host()
        _register(host, "2026-11-24T00:00:00+00:00")
        _register(host, "2026-11-24T00:10:00+00:00")
        rows = _rows()
        assert len(rows) == 1, f"同一稳定身份两次注册应复用一行，实际 {len(rows)} 行：{rows}"
        assert rows[0][1] == host
    finally:
        _cleanup()


def test_volatile_hostname_creates_a_second_row():
    """反证：身份不稳定时，hostname 变化会新增一行（线上「多一条离线」的成因）。"""
    _cleanup()
    try:
        _register("4c9002afdeaf", "2026-11-24T00:00:00+00:00")
        _register("820a896692cb", "2026-11-24T00:10:00+00:00")
        rows = _rows()
        assert len(rows) == 2, f"两个不同 hostname 应产生两行，实际 {len(rows)} 行：{rows}"
    finally:
        _cleanup()