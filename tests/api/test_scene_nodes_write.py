"""场景节点写原语接线单测（P3-4b）：lib 可用 / 不可用 / 并发不一致 / 失败补偿。"""
import json

import pytest

from src.domains.runtime.engine import scene_nodes_write as snw
from src.kernel import cross_write as cw


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class _FakeLib:
    """最小 mgmt lib 替身：按预设行为返回 set_scene_nodes 结果。"""

    def __init__(self, result=None, raises=None):
        self.result = result or {"action": "updated", "changed": 1, "hash": "h"}
        self.raises = raises
        self.calls = []

    def set_scene_nodes(self, nodes_json, scene_id=None, row_id=None, repo_key=None,
                        expected_hash=None):
        self.calls.append({"nodes_json": nodes_json, "row_id": row_id,
                           "expected_hash": expected_hash})
        if self.raises:
            raise self.raises
        return dict(self.result)


def _patch_lib(monkeypatch, lib):
    monkeypatch.setattr("src.kernel.domain_read.mgmt_lib", lambda: lib)


def _clean_key(key: str) -> None:
    from src.kernel.session_manager import db
    cw.ensure_log_table()
    db.cursor().execute(f"DELETE FROM {cw.LOG_TABLE} WHERE idempotency_key=?", (key,))


def test_lib_unavailable_uses_local_sql(monkeypatch):
    _patch_lib(monkeypatch, None)
    c = _FakeCursor()
    rc = snw.write_scene_nodes('[]', 7, "", c, "now-iso", None)
    assert rc == snw.LOCAL
    assert c.calls and "UPDATE devops_pipeline_scene SET nodes_json=?" in c.calls[0][0]
    assert c.calls[0][1] == ('[]', "now-iso", 7)


def test_lib_updated_records_applied_intent(monkeypatch):
    key = f"scene-nodes:11:{snw.content_hash('[]')}"
    _clean_key(key)
    lib = _FakeLib()
    _patch_lib(monkeypatch, lib)
    rc = snw.write_scene_nodes('[]', 11, "abcd1234", _FakeCursor(), "now-iso", None)
    assert rc == snw.UPDATED
    assert lib.calls[0]["expected_hash"] == "abcd1234"
    from src.kernel.session_manager import db
    row = db.cursor().execute(
        f"SELECT status, target_domain, action FROM {cw.LOG_TABLE} "
        "WHERE idempotency_key=?", (key,)).fetchone()
    assert row[0] == "applied" and row[1] == "mgmt" and row[2] == "scene_nodes_update"


def test_lib_skipped_when_content_unchanged(monkeypatch):
    _patch_lib(monkeypatch, _FakeLib({"action": "skipped", "changed": 0, "hash": "h"}))
    assert snw.write_scene_nodes('[]', 12, "abcd1234", _FakeCursor(), "now-iso",
                                 None) == snw.SKIPPED


def test_lib_mismatch_is_not_retried(monkeypatch):
    """expected_hash 不一致 = 并发写已生效：跳过且不留待补偿意图（不回写陈旧快照）。"""
    key = f"scene-nodes:13:{snw.content_hash('[]')}"
    _clean_key(key)
    _patch_lib(monkeypatch, _FakeLib({"action": "mismatch", "changed": 0, "hash": "h"}))
    assert snw.write_scene_nodes('[]', 13, "stale0000", _FakeCursor(), "now-iso",
                                 None) == snw.MISMATCH
    from src.kernel.session_manager import db
    row = db.cursor().execute(
        f"SELECT status FROM {cw.LOG_TABLE} WHERE idempotency_key=?", (key,)).fetchone()
    assert row[0] == "applied"       # 不进入重试队列


def test_lib_failure_queues_and_drain_replays(monkeypatch):
    payload_nodes = '[{"t":1}]'
    key = f"scene-nodes:14:{snw.content_hash(payload_nodes)}"
    _clean_key(key)
    _patch_lib(monkeypatch, _FakeLib(raises=RuntimeError("mgmt down")))
    assert snw.write_scene_nodes(payload_nodes, 14, "", _FakeCursor(), "now-iso",
                                 None) == snw.FAILED

    from src.kernel.session_manager import db
    db.cursor().execute(f"UPDATE {cw.LOG_TABLE} SET next_retry_at=0 WHERE idempotency_key=?",
                        (key,))
    lib2 = _FakeLib()
    _patch_lib(monkeypatch, lib2)
    out = cw.drain({"scene_nodes_update": lambda p: lib2.set_scene_nodes(
        p["nodes_json"], row_id=int(p["rid"]))})
    assert out["applied"] == 1
    assert lib2.calls[0]["row_id"] == 14
    assert json.loads(lib2.calls[0]["nodes_json"]) == [{"t": 1}]


def test_duplicate_content_does_not_rewrite(monkeypatch):
    key = f"scene-nodes:15:{snw.content_hash('[]')}"
    _clean_key(key)
    lib = _FakeLib()
    _patch_lib(monkeypatch, lib)
    snw.write_scene_nodes('[]', 15, "", _FakeCursor(), "now-iso", None)
    assert snw.write_scene_nodes('[]', 15, "", _FakeCursor(), "now-iso", None) == snw.SKIPPED
    assert len(lib.calls) == 1        # 幂等键命中，没有第二次写


def test_content_hash_matches_lib_formula():
    import hashlib
    assert snw.content_hash("abc") == hashlib.sha1(b"abc").hexdigest()[:12]