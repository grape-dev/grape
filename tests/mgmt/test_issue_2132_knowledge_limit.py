"""Issue #2132 B2 回归（mgmt 域内）：条目上限、截断可观测、SQL 侧下推。"""
import pytest

from src.domains.mgmt import knowledge as kb


def test_entry_limit_env_and_default(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_MAX_FILES", raising=False)
    assert kb.knowledge_entry_limit() == 1000
    monkeypatch.setenv("KNOWLEDGE_MAX_FILES", "5")
    assert kb.knowledge_entry_limit() == 5
    monkeypatch.setenv("KNOWLEDGE_MAX_FILES", "-3")  # 非法回退默认
    assert kb.knowledge_entry_limit() == 1000


def test_truncate_entries_keeps_first_n_and_counts(monkeypatch, caplog):
    monkeypatch.setenv("KNOWLEDGE_MAX_FILES", "3")
    before = kb.knowledge_cache_stats()["truncated_files"]
    files = {f"f{i}": {"content": "x"} for i in range(7)}  # 插入序 f0..f6
    out = kb.truncate_entries(files, "acme/demo")
    assert list(out.keys()) == ["f0", "f1", "f2"]
    after = kb.knowledge_cache_stats()["truncated_files"]
    assert after - before == 4  # 丢 4 条，计数可见


def test_agent_query_pushes_down_order_and_limit(monkeypatch):
    """B2（F4 整改）：SQL 必须带 ORDER BY id DESC + LIMIT，截断发生在下载前。"""
    import threading
    captured = {}

    class _Cur:
        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return self

        def fetchall(self):
            return []

    class _DB:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(kb, "db_lock", threading.Lock())
    monkeypatch.setattr(kb, "db", _DB())
    monkeypatch.setenv("KNOWLEDGE_MAX_FILES", "9")

    kb.agent_knowledge_files("acme/demo", agent_type="coding")
    sql = captured.get("sql", "")
    assert "ORDER BY k.id DESC" in sql, sql
    assert "LIMIT ?" in sql, sql
    assert captured["params"][-1] == 9  # 上限是最后一个参数
