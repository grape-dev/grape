"""Issue #697：巡检重复判定双维度（根因对比 + 疑似重复降级 + owner 确认门禁）回归测试。

覆盖（prompt 侧规则已按运维决定回退，本文件只锁代码侧行为）：
1. parse_findings 透传 dedup_level/dedup_note（疑似重复差异说明），未知级别 fail-closed。
2. dispatch 门禁：suspected_duplicate 未确认 → 409；owner 确认后放行并落标记。
3. 派发描述追加判定级别与差异说明（证据链）。
4. 级别归一/门禁判定辅助函数（scheduler 单一事实源）。
"""
import importlib.util
import logging
import os
import sys
import types
import threading

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC_DIR)

_log = logging.getLogger("test_issue_697")

# ===== 2/3. scheduler：执行 prompt 注入 + findings 透传 =====

_SCHED = None


@pytest.fixture(scope="module")
def sched():
    """Load scheduler.py standalone with stubbed config/session_manager（同 test_autopilot_feishu_notify）."""
    global _SCHED
    if _SCHED is not None:
        yield _SCHED
        return
    saved = {k: sys.modules.get(k) for k in ("src.config", "src.session_manager")}
    cfg = types.ModuleType("src.config")
    cfg.log = _log
    src_pkg = sys.modules.get("src") or types.ModuleType("src")
    if not hasattr(src_pkg, "__path__"):
        src_pkg.__path__ = [os.path.join(SRC_DIR, "src")]
    sys.modules["src"] = src_pkg
    sys.modules["src.config"] = cfg
    sm = types.ModuleType("src.session_manager")
    sm.db = None
    sm.db_lock = threading.Lock()
    sys.modules["src.session_manager"] = sm
    try:
        spec = importlib.util.spec_from_file_location(
            "sched_697_test", os.path.join(SRC_DIR, "src", "runtime", "scheduler.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SCHED = mod
        yield mod
    finally:
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v


class TestSchedulerDedupRule:
    def test_parse_findings_passthrough_dedup_fields(self, sched):
        out = ('<!-- autopilot-findings\n'
               '{"result":"warning","summary":"s1","findings":[{"id":"f1","title":"t","description":"d",'
               '"severity":"high","dedup_level":"suspected_duplicate","dedup_note":"报错信息不一致: '
               'unknown request field(s) vs 404 scene not found"}]}\n-->')
        r = sched.parse_findings(out)
        f = r["findings"][0]
        assert f["dedup_level"] == "suspected_duplicate"
        assert "unknown request field(s)" in f["dedup_note"]

    def test_parse_findings_defaults_dedup_fields(self, sched):
        out = '<!-- autopilot-findings\n{"result":"","summary":"","findings":[{"id":"f1","title":"t","description":"d"}]}\n-->'
        r = sched.parse_findings(out)
        f = r["findings"][0]
        assert f["dedup_level"] == ""
        assert f["dedup_note"] == ""

    def test_parse_findings_unknown_level_fail_closed(self, sched):
        """LLM 拼错 dedup_level（非法枚举）→ 解析层即降级 suspected_duplicate（fail-closed）。"""
        out = ('<!-- autopilot-findings\n'
               '{"result":"warning","summary":"s1","findings":[{"id":"f1","title":"t","description":"d",'
               '"dedup_level":"suspected"}]}\n-->')
        f = sched.parse_findings(out)["findings"][0]
        assert f["dedup_level"] == "suspected_duplicate"

    def test_parse_findings_truncates_long_note(self, sched):
        """dedup_note 在解析层限长（写入公开 Issue 描述前的防线）。"""
        import json as _json
        payload = {"result": "warning", "summary": "s1", "findings": [
            {"id": "f1", "title": "t", "description": "d",
             "dedup_level": "suspected_duplicate", "dedup_note": "长" * 500}]}
        out = '<!-- autopilot-findings\n' + _json.dumps(payload, ensure_ascii=False) + '\n-->'
        f = sched.parse_findings(out)["findings"][0]
        assert len(f["dedup_note"]) == sched.DEDUP_NOTE_MAX_CHARS

    def test_normalize_and_guard_helpers(self, sched):
        assert sched.normalize_dedup_level("") == ""
        assert sched.normalize_dedup_level(None) == ""
        assert sched.normalize_dedup_level("Exact_Duplicate") == "exact_duplicate"
        assert sched.normalize_dedup_level("suspected") == "suspected_duplicate"  # 未知 fail-closed
        # 并入门禁：疑似未确认拒绝，确认放行
        assert sched.dedup_auto_merge_allowed("suspected_duplicate", False) is False
        assert sched.dedup_auto_merge_allowed("suspected_duplicate", True) is True
        assert sched.dedup_auto_merge_allowed("", False) is True

# ===== 4/5. routes/autopilot.py：dispatch 门禁 + 描述证据链 =====

@pytest.fixture(scope="module")
def ap():
    from src.api.routes import autopilot
    return autopilot


class TestDispatchGuard:
    def test_suspected_unconfirmed_rejected(self, ap):
        finding = {"id": "f1", "dedup_level": "suspected_duplicate", "dedup_note": "根因不一致"}
        with pytest.raises(Exception) as ei:
            ap._dedup_dispatch_guard(finding, dedup_confirmed=False)
        assert getattr(ei.value, "status_code", None) == 409
        assert "owner" in str(ei.value.detail)

    def test_suspected_confirmed_passes(self, ap):
        finding = {"id": "f1", "dedup_level": "suspected_duplicate", "dedup_note": "根因不一致",
                   "dedup_confirmed": True}
        ap._dedup_dispatch_guard(finding, dedup_confirmed=False)  # 已确认 → 不抛

    def test_exact_duplicate_passes_without_confirmation(self, ap):
        finding = {"id": "f1", "dedup_level": "exact_duplicate"}
        ap._dedup_dispatch_guard(finding, dedup_confirmed=False)

    def test_no_level_passes(self, ap):
        ap._dedup_dispatch_guard({}, dedup_confirmed=False)

    def test_unknown_level_fail_closed(self, ap):
        """LLM 拼错/漏发非空 dedup_level → fail-closed 按 suspected 处理（409）。"""
        for bad in ("suspected", "SUSPECTED_DUPLICATE ", "duplicate?", "exact"):
            finding = {"id": "f1", "dedup_level": bad}
            with pytest.raises(Exception) as ei:
                ap._dedup_dispatch_guard(finding, dedup_confirmed=False)
            assert getattr(ei.value, "status_code", None) == 409, bad

    def test_append_dedup_note_unknown_level_fail_closed(self, ap):
        out = ap._append_dedup_note("原始描述", {"dedup_level": "duplicate?"})
        assert "level=suspected_duplicate" in out
        assert "待跟踪单 owner agent 确认后方可并入" in out

    def test_append_dedup_note_truncates_and_flattens(self, ap):
        finding = {"dedup_level": "suspected_duplicate", "dedup_note": "行1\n行2 " + "长" * 400}
        out = ap._append_dedup_note("原始描述", finding)
        seg = out.split("[dedup] 差异说明: ", 1)[1].split("\n", 1)[0]
        assert "\n" not in seg
        assert len(seg) == 200

    def test_confirmation_persisted_even_if_issue_create_fails(self, ap, monkeypatch):
        """Issue #697：owner 确认标记先于建 Issue 落库，建 Issue 失败（issue_number=0）不丢确认。"""
        import asyncio
        import sqlite3
        import json as _json
        import src.api.issues_center as issues_center
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE autopilot_executions (id INTEGER PRIMARY KEY, task_id INTEGER, findings_json TEXT)")
        conn.execute("CREATE TABLE autopilot_tasks (id INTEGER PRIMARY KEY, repo_key TEXT)")
        conn.execute("INSERT INTO autopilot_tasks VALUES (1, 'gitcode:org/repo')")
        findings = {"result": "", "summary": "", "findings": [
            {"id": "f1", "title": "t", "description": "d", "dedup_level": "suspected_duplicate"}]}
        conn.execute("INSERT INTO autopilot_executions VALUES (5, 1, ?)", (_json.dumps(findings),))
        monkeypatch.setattr(ap, "db", conn)
        monkeypatch.setattr(ap, "db_lock", threading.Lock())

        async def fake_create(body, user=None):
            return {"issue_number": "0"}
        monkeypatch.setattr(issues_center, "create_task_via_issue", fake_create)
        r = asyncio.run(ap._dispatch_finding_core(5, "f1", dedup_confirmed=True,
                                                  dedup_confirmed_by="owner-agent"))
        assert r.get("ok") is False
        row = conn.execute("SELECT findings_json FROM autopilot_executions WHERE id=5").fetchone()
        f = _json.loads(row[0])["findings"][0]
        assert f["dedup_confirmed"] is True
        assert f["dedup_confirmed_by"] == "owner-agent"
        assert not f.get("dispatched_issue_number")

    def test_confirmed_by_sanitized(self, ap):
        """dedup_confirmed_by 非自由文本：压平限长。"""
        assert ap.sanitize_dedup_confirmed_by(" a\n b " + "x" * 100) == "a b " + "x" * 60

    def test_append_dedup_note_evidence_chain(self, ap):
        finding = {"dedup_level": "suspected_duplicate",
                   "dedup_note": "报错信息: unknown request field(s) vs 404 scene not found"}
        out = ap._append_dedup_note("原始描述", finding)
        assert "level=suspected_duplicate" in out
        assert "404 scene not found" in out
        assert "待跟踪单 owner agent 确认后方可并入" in out

    def test_append_dedup_note_no_level_untouched(self, ap):
        assert ap._append_dedup_note("原始描述", {}) == "原始描述"
