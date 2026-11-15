"""`kernel.domain_db.cursor` 语义：own DSN 优先，否则回落进程库；未知域直接报错。"""
import pytest

from src.kernel import db_registry, domain_db


def test_unknown_domain_rejected():
    with pytest.raises(ValueError):
        with domain_db.cursor("other"):
            pass  # pragma: no cover - 上下文未建立


def test_own_dsn_uses_domain_cursor(monkeypatch):
    calls = {}

    class _Cur:
        description = ()

        def execute(self, *a, **k):
            return self

        def fetchall(self):
            return []

    class _CM:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *exc):
            return False

    def _cursor(domain):
        calls["domain"] = domain
        return _CM()

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: d == "runtime")
    monkeypatch.setattr(db_registry, "cursor", _cursor)
    with domain_db.cursor("runtime") as c:
        assert c is not None
    assert calls["domain"] == "runtime"


def test_fallback_uses_process_db(monkeypatch):
    """无自有 DSN（单库/视图期）时回到 session_manager.db，行为与改动前一致。"""
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False)
    from src.kernel import session_manager

    used = {}

    class _Cur:
        pass

    class _Db:
        def cursor(self):
            used["process"] = True
            return _Cur()

    monkeypatch.setattr(session_manager, "db", _Db())
    with domain_db.cursor("runtime") as c:
        assert isinstance(c, _Cur)
    assert used.get("process") is True