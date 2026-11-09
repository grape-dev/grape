"""Issue #1545：agent 产出回帖策略（阻止引擎报错文本外泄）。

覆盖：
- B1 `_looks_like_error_text` 固定规则矩阵
- B2/B3 `GRAPE_RESULT_POST_POLICY` 三模式语义（strict / clean / off）
- A1/A3 `agent_result=False` 系统通知在三种模式下照常发送
- C1/C2 抑制留痕（`RESULT_POST_SUPPRESSED`、幂等、DB 异常不影响主流程）
- D3 7 个调用点全部显式传 `agent_result`
"""
import ast
import inspect
import json
import os
import pathlib

import pytest

from src.session_manager import db, db_lock

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _seed_task(tid="t_1545_policy", seq=1545001, issue="#1545",
               repo="gitcode:grape-dev/grape", agent="vod", errors=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, errors) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "s1", 0, seq, issue, repo, agent, "p", errors))
        db.commit()


def _get_errors(tid):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT errors FROM agent_tasks WHERE id=?", (tid,))
        row = c.fetchone()
    if not row or not row[0]:
        return []
    try:
        parsed = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return [str(row[0])]
    return parsed if isinstance(parsed, list) else [parsed]


def _suppressed_records(tid):
    return [e for e in _get_errors(tid)
            if isinstance(e, dict) and e.get("error_code") == "RESULT_POST_SUPPRESSED"]


def _make_mock_urlopen(captured, existing_comments=None):
    _existing = json.dumps(existing_comments or []).encode()

    class _MockResp:
        def __init__(self, data=b'{"id":1}'):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _mock(req, timeout=15):
        if req.data:
            captured["body"] = json.loads(req.data.decode())["body"]
            captured["url"] = req.full_url
            return _MockResp()
        return _MockResp(_existing)

    return _mock


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    import src.api.event_pipeline as ep
    monkeypatch.delenv("GRAPE_RESULT_POST_POLICY", raising=False)
    monkeypatch.setattr(ep, "_RESULT_POST_POLICY_WARNED", False)


# =====================================================================
# B1 判定矩阵
# =====================================================================

class TestLooksLikeErrorText:
    @pytest.mark.parametrize("text", [
        "HTTP 404: Model not found",
        "❌ 上游瞬时抖动，稍后重试",
        "Traceback (most recent call last):\n  File ...",
        "connect ECONNREFUSED 127.0.0.1:443",
        "exit_code=1",
        "HTTP 401 Unauthorized",
        "HTTP 403 Forbidden",
        "upstream timed out",
        "Bad Gateway",
        "service unavailable",
        "SocketTimeout",
        "rate limit exceeded",
        "quota exceeded",
        "model_not_found",
    ])
    def test_hits(self, text):
        from src.api.event_pipeline import _looks_like_error_text
        assert _looks_like_error_text(text) is True

    @pytest.mark.parametrize("text", [
        "✅ 已完成：补丁见 PR #12",
        "已修复 bug，测试通过。",
        "conclusion: 审查通过",
        "",
    ])
    def test_misses(self, text):
        from src.api.event_pipeline import _looks_like_error_text
        assert _looks_like_error_text(text) is False


# =====================================================================
# B2 策略读取
# =====================================================================

class TestPolicyReading:
    @pytest.mark.parametrize("value,expected", [
        ("strict", "strict"),
        ("clean", "clean"),
        ("off", "off"),
        ("CLEAN", "clean"),
        ("", "strict"),
    ])
    def test_valid_and_default(self, monkeypatch, value, expected):
        from src.api.event_pipeline import _result_post_policy
        monkeypatch.setenv("GRAPE_RESULT_POST_POLICY", value)
        assert _result_post_policy() == expected

    def test_invalid_falls_back_strict_and_warns_once(self, monkeypatch):
        import src.api.event_pipeline as ep
        monkeypatch.setenv("GRAPE_RESULT_POST_POLICY", "weird")
        calls = []
        monkeypatch.setattr(ep.log, "warning", lambda *a, **k: calls.append(a))
        assert ep._result_post_policy() == "strict"
        assert ep._result_post_policy() == "strict"
        assert len(calls) == 1, "非法值进程内只告警一次"


# =====================================================================
# B3 三模式语义 + agent_result 来源区分
# =====================================================================

class TestPolicyModes:
    ERROR_TEXT = "HTTP 404: Model not found"
    NORMAL_TEXT = "✅ 已完成：补丁见 PR #12"
    STRUCTURED = json.dumps({"actions": ["fix"], "conclusion": "已修复"})

    def _post(self, monkeypatch, captured, text, agent_result, policy, tid):
        from src.api.event_pipeline import _post_result_to_issue
        monkeypatch.setenv("GRAPE_RESULT_POST_POLICY", policy)
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", _make_mock_urlopen(captured))
        _post_result_to_issue("test/repo", "5", text, "fake_token", "vod",
                              agent_result=agent_result, task_id=tid)

    @pytest.mark.parametrize("policy", ["strict", "clean", "off"])
    def test_error_text_suppressed(self, monkeypatch, policy):
        tid = f"t_1545_err_{policy}"
        _seed_task(tid=tid)
        captured = {}
        self._post(monkeypatch, captured, self.ERROR_TEXT, True, policy, tid)
        assert "body" not in captured, f"{policy} 下错误文本不应发评论"
        assert _suppressed_records(tid), f"{policy} 下应留痕 RESULT_POST_SUPPRESSED"

    def test_strict_suppresses_normal_plain_text(self, monkeypatch):
        tid = "t_1545_strict_normal"
        _seed_task(tid=tid)
        captured = {}
        self._post(monkeypatch, captured, self.NORMAL_TEXT, True, "strict", tid)
        assert "body" not in captured, "strict 只发结构化 conclusion"
        assert _suppressed_records(tid)

    def test_strict_posts_structured_conclusion(self, monkeypatch):
        tid = "t_1545_strict_struct"
        _seed_task(tid=tid)
        captured = {}
        self._post(monkeypatch, captured, self.STRUCTURED, True, "strict", tid)
        assert "body" in captured
        assert "[VOD Agent]" in captured["body"]
        assert "已修复" in captured["body"]
        assert not _suppressed_records(tid)

    def test_clean_posts_normal_plain_text(self, monkeypatch):
        tid = "t_1545_clean_normal"
        _seed_task(tid=tid)
        captured = {}
        self._post(monkeypatch, captured, self.NORMAL_TEXT, True, "clean", tid)
        assert "body" in captured, "clean 下正常纯文本应照发"
        assert self.NORMAL_TEXT in captured["body"]
        assert "<!-- grape-auto-comment -->" in captured["body"]
        assert not _suppressed_records(tid)

    def test_clean_truncates_long_plain_text_to_3000(self, monkeypatch):
        tid = "t_1545_clean_long"
        _seed_task(tid=tid)
        long_text = "A" * 3500
        captured = {}
        self._post(monkeypatch, captured, long_text, True, "clean", tid)
        assert "body" in captured, "clean 下正常长文本应照发"
        assert long_text[:3000] in captured["body"], "clean 纯文本应沿用既有 3000 字截断"
        assert long_text[:3001] not in captured["body"]
        assert long_text not in captured["body"]
        assert "<!-- grape-auto-comment -->" in captured["body"]
        assert not _suppressed_records(tid)

    def test_off_suppresses_structured(self, monkeypatch):
        tid = "t_1545_off_struct"
        _seed_task(tid=tid)
        captured = {}
        self._post(monkeypatch, captured, self.STRUCTURED, True, "off", tid)
        assert "body" not in captured, "off 下 agent 产出零评论"
        assert _suppressed_records(tid)

    @pytest.mark.parametrize("policy", ["strict", "clean", "off"])
    def test_system_notification_always_posted(self, monkeypatch, policy):
        tid = f"t_1545_sys_{policy}"
        _seed_task(tid=tid)
        captured = {}
        self._post(monkeypatch, captured, self.ERROR_TEXT, False, policy, tid)
        assert "body" in captured, f"{policy} 下系统通知应照发"
        assert self.ERROR_TEXT in captured["body"]
        assert "[VOD Agent]" in captured["body"]
        assert "<!-- grape-auto-comment -->" in captured["body"]
        assert not _suppressed_records(tid)


# =====================================================================
# C 抑制留痕：幂等 + DB 异常
# =====================================================================

class TestSuppressionLedger:
    def test_idempotent_same_task_same_text(self, monkeypatch):
        from src.api.event_pipeline import _record_post_suppressed
        tid = "t_1545_idem"
        _seed_task(tid=tid)
        monkeypatch.setenv("GRAPE_RESULT_POST_POLICY", "strict")
        for _ in range(2):
            _record_post_suppressed(tid, 1545001, "gitcode:grape-dev/grape", "#1545",
                                    "vod", "strict_no_conclusion",
                                    "HTTP 404: Model not found")
        recs = _suppressed_records(tid)
        assert len(recs) == 1, "同任务同文本重复抑制只记一条"

    def test_db_error_does_not_raise(self, monkeypatch):
        import src.api.event_pipeline as ep
        tid = "t_1545_dberr"
        _seed_task(tid=tid)

        class _BoomDB:
            def cursor(self):
                raise RuntimeError("db down")

            def commit(self):
                raise RuntimeError("db down")

        monkeypatch.setattr(ep, "db", _BoomDB())
        ep._record_post_suppressed(tid, 1545001, "gitcode:grape-dev/grape", "#1545",
                                   "vod", "policy_off", "HTTP 404: Model not found")


# =====================================================================
# D3 调用点完整性（AST）
# =====================================================================

class TestCallSitesExplicit:
    FILES = [
        "src/api/event_pipeline.py",
        "src/runtime/direct_executor.py",
        "src/runtime/service.py",
        "src/api/pr_merged_sre_retry.py",
        "src/token_resolver.py",
    ]
    NAMES = {"_post_result_to_issue", "_post_result_to_pr"}

    def test_all_call_sites_pass_agent_result(self):
        total = 0
        for rel in self.FILES:
            path = os.path.join(ROOT, rel)
            tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                else:
                    continue
                if name not in self.NAMES:
                    continue
                total += 1
                kws = {k.arg for k in node.keywords if k.arg}
                assert "agent_result" in kws, \
                    f"{rel}:{node.lineno} 调用 {name} 未显式传 agent_result"
        assert total == 7, f"应为 7 个调用点，实际 {total}"