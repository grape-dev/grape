"""Issue #4065：结果回帖链路的 ident 归一、短路与可观测。

生产症状（Loki 1h 实测）：autopilot 任务（`agent_tasks.issue_id = autopilot-<字母>`）跑完后
结果评论全失败——`/repos/{o}/{r}/issues/autopilot-as/comments` 恒 HTTP 400「参数类型错误」，
预检（`_list_issue_comments_card`）先 400，`_post_result_to_issue` 的 try/except 把它吞成
一条 WARNING，结论彻底不可见。

本文件锁定四件事：
1. 三类非数字 ident（`autopilot-*` / `IK*` / `pr_*`）在投递与预检入口**不发起任何远程请求**；
2. 跳过时落结构化记录 `RESULT_COMMENT_SKIPPED`（reason/ident/repo/task_id）；
3. 有真实 `issue_number` 时凭真实号回帖，没有时结果落卡片/事件流（`RESULT_CARD_FALLBACK`）；
4. 失败指标 `comment_post_failed_total{reason}` 可按 reason 聚合，连续失败达阈值告警。
"""

import json
import os
import threading

import pytest

from src.kernel.session_manager import db, db_lock

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

GITCODE_RK = "gitcode:grape-dev/grape"
GITEE_RK = "gitee:GTDTeam/CodeArts"


# =====================================================================
# 公共桩：VCS 客户端与任务/事件行
# =====================================================================

class _RecordingClient:
    """最小 VcsApiClient 替身：记录每次远程调用。"""

    owner = "grape-dev"
    repo = "grape"

    def __init__(self, calls, resp=None):
        self._calls = calls
        self._resp = {"id": 1} if resp is None else resp

    def _generic_path(self, github_path, alt_path=""):
        return github_path

    def _request(self, method, path, data=None, **kwargs):
        self._calls.append((method, path))
        return self._resp


@pytest.fixture
def vcs_spy(monkeypatch):
    """把 create_client 换成记录器；返回记录列表（空 = 零远程请求）。"""
    calls = []
    import src.domains.runtime.vcs.api_client as api

    monkeypatch.setattr(api, "create_client",
                        lambda repo_key, token="": _RecordingClient(calls))
    return calls


@pytest.fixture
def vcs_forbidden(monkeypatch):
    """任何远程客户端构造都视为违规（非数字 ident 的硬断言）。"""
    def _boom(*args, **kwargs):
        raise AssertionError("非数字 ident 不得构造 VCS 客户端发起远程请求")

    import src.domains.runtime.vcs.api_client as api

    monkeypatch.setattr(api, "create_client", _boom)


@pytest.fixture
def log_spy(monkeypatch):
    """捕获 event_pipeline.log 的结构化行。"""
    lines = []

    class _Log:
        def _record(self, level):
            def _f(msg, *args, **kwargs):
                lines.append((level, str(msg)))
            return _f

        def __getattr__(self, name):
            return self._record(name)

    import src.domains.runtime.pipeline.event_pipeline as ep

    monkeypatch.setattr(ep, "log", _Log())
    return lines


def _seed_task(tid, issue_id, seq=None, repo_key=GITCODE_RK, agent="sre"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, errors) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "s1", 0, seq, issue_id, repo_key, agent, "p", ""))
        db.commit()


def _seed_event(seq, issue_id, payload, repo_key=GITCODE_RK):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, payload, agent_type) "
            "VALUES (?,?,?,?,?,?)",
            (seq, repo_key, "webhook", issue_id, json.dumps(payload), "sre"))
        db.commit()


def _task_errors(tid):
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


def _records(tid, code):
    return [e for e in _task_errors(tid) if isinstance(e, dict) and e.get("error_code") == code]


# =====================================================================
# 1. 归一口径（kernel 单点）
# =====================================================================

class TestCommentIdentNormalization:
    @pytest.mark.parametrize("raw", ["autopilot-as", "autopilot-ay", "#autopilot-ad",
                                    "pr_123", "#pr_123_scene_id", "", "  ", "0"])
    def test_non_deliverable_on_numeric_platform(self, raw):
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident(raw, "gitcode") == ""

    @pytest.mark.parametrize("raw,expected", [
        (4065, "4065"), ("4065", "4065"), ("#4065", "4065"), (" 4065 ", "4065"),
    ])
    def test_numeric_deliverable(self, raw, expected):
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident(raw, "gitcode") == expected

    def test_gitee_native_ident_deliverable(self):
        """gitee 的 notes 路由接受平台原生字符串 ident，不得误杀。"""
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident("IKX1DL", "gitee") == "IKX1DL"
        assert deliverable_comment_ident("#IKX1DL", "gitee") == "IKX1DL"

    def test_gitee_native_ident_rejected_on_numeric_platform(self):
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident("IKX1DL", "gitcode") == ""


class TestCardIdentNormalization:
    @pytest.mark.parametrize("raw,expected", [
        (4065, "4065"), ("#4065", "4065"),
        ("IKX1DL", "IKX1DL"),
        ("autopilot-as", "autopilot-as"),          # issue 表 TEXT 主键，真实卡片键
    ])
    def test_normalizable(self, raw, expected):
        from src.kernel.issue_ident import normalize_card_ident
        assert normalize_card_ident(raw) == expected

    @pytest.mark.parametrize("raw", ["pr_123", "#pr_123_scene_id", "", "   ", "a" * 40,
                                    "autopilot as", "4065\n"])
    def test_not_normalizable(self, raw):
        from src.kernel.issue_ident import normalize_card_ident
        assert normalize_card_ident(raw) == ""

    @pytest.mark.parametrize("raw,expected", [
        (4065, 4065), ("4065", 4065),        # #995：数字平台契约恒 int
        ("IKX1DL", "IKX1DL"),
        ("autopilot-as", "autopilot-as"),
        ("pr_123", None),
        (None, None),
    ])
    def test_card_ident_number(self, raw, expected):
        from src.kernel.issue_ident import card_ident_number
        assert card_ident_number(raw) == expected


# =====================================================================
# 2. 投递 / 预检入口：非数字 ident 零远程 400
# =====================================================================

class TestDeliveryEntryShortCircuit:
    def test_post_comment_skips_autopilot_without_remote(self, vcs_forbidden, log_spy):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        ok = _post_issue_comment_ledgered("grape-dev/grape", "autopilot-as", "body", "tok",
                                          repo_key=GITCODE_RK)
        assert ok is False
        joined = "\n".join(m for _lvl, m in log_spy)
        assert "RESULT_COMMENT_SKIPPED" in joined
        assert "non_numeric_ident" in joined
        assert "autopilot-as" in joined

    @pytest.mark.parametrize("ident", ["autopilot-au", "pr_9", "IKX1DL"])
    def test_post_comment_skips_all_three_shapes_on_gitcode(self, vcs_forbidden, log_spy, ident):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        assert _post_issue_comment_ledgered("grape-dev/grape", ident, "body", "tok",
                                            repo_key=GITCODE_RK) is False

    def test_list_comments_skips_without_remote(self, vcs_forbidden):
        from src.domains.runtime.pipeline.event_pipeline import _list_issue_comments_card
        assert _list_issue_comments_card(GITCODE_RK, "grape-dev/grape", "autopilot-as", "tok") == []

    def test_post_comment_numeric_still_hits_remote(self, vcs_spy):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        assert _post_issue_comment_ledgered("grape-dev/grape", "4065", "body", "tok",
                                            repo_key=GITCODE_RK) is True
        assert vcs_spy, "合法数字号必须真实投递"
        method, path = vcs_spy[-1]
        assert method == "POST"
        assert path.endswith("/issues/4065/comments")

    def test_post_comment_normalizes_hash_prefix(self, vcs_spy):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        assert _post_issue_comment_ledgered("grape-dev/grape", "#4065", "body", "tok",
                                            repo_key=GITCODE_RK) is True
        assert vcs_spy[-1][1].endswith("/issues/4065/comments")


# =====================================================================
# 3. 结果路由：真实号回帖 / 无号落卡片事件流
# =====================================================================

class TestResultRouting:
    STRUCTURED = json.dumps({"actions": ["x"], "conclusion": "巡检结论：一切正常"})

    def test_autopilot_result_falls_back_to_card_stream(self, monkeypatch, log_spy):
        """非数字 ident + 无真实号 → 不发评论，落 RESULT_COMMENT_SKIPPED + RESULT_CARD_FALLBACK。"""
        import src.domains.runtime.pipeline.event_pipeline as ep
        tid, seq = "t_4065_auto", 4065001
        _seed_task(tid, "autopilot-as", seq=seq)
        _seed_event(seq, "autopilot-as", {"issue_number": "autopilot-as"})

        sent = []
        monkeypatch.setattr(ep, "_post_issue_comment_ledgered",
                            lambda *a, **k: sent.append(a))
        monkeypatch.setattr(ep, "_list_issue_comments_card",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("预检不得发起")))

        ep._post_result_to_issue("grape-dev/grape", "autopilot-as", self.STRUCTURED,
                                 "tok", "sre", GITCODE_RK, agent_result=True,
                                 task_id=tid, seq=seq)
        assert sent == [], "无有效号不得投递评论"
        assert _records(tid, "RESULT_COMMENT_SKIPPED"), "应留痕 RESULT_COMMENT_SKIPPED"
        assert _records(tid, "RESULT_CARD_FALLBACK"), "结果应落卡片/事件流留痕"
        skipped = _records(tid, "RESULT_COMMENT_SKIPPED")[0]
        assert skipped.get("reason") == "non_numeric_ident"
        assert skipped.get("issue_ident") == "autopilot-as"
        fallback = _records(tid, "RESULT_CARD_FALLBACK")[0]
        assert "巡检结论" in json.dumps(fallback, ensure_ascii=False)
        assert "RESULT_COMMENT_SKIPPED" in "\n".join(m for _l, m in log_spy)

    def test_synthetic_key_uses_real_issue_number_from_task(self, monkeypatch):
        """任务记录带真实号（事件 payload）→ 凭真实号回帖（双保险）。"""
        import src.domains.runtime.pipeline.event_pipeline as ep
        tid, seq = "t_4065_real", 4065002
        _seed_task(tid, "autopilot-ay", seq=seq)
        _seed_event(seq, "autopilot-ay", {"issue_number": "4065"})

        sent = []
        monkeypatch.setattr(ep, "_list_issue_comments_card", lambda *a, **k: [])
        monkeypatch.setattr(ep, "_post_issue_comment_ledgered",
                            lambda repository, num, *a, **k: sent.append(num) or True)

        ep._post_result_to_issue("grape-dev/grape", "autopilot-ay", self.STRUCTURED,
                                 "tok", "sre", GITCODE_RK, agent_result=True,
                                 task_id=tid, seq=seq)
        assert sent == ["4065"], "应凭任务记录里的真实 issue_number 回帖"

    def test_numeric_ident_posts_normally(self, monkeypatch):
        import src.domains.runtime.pipeline.event_pipeline as ep
        tid = "t_4065_numeric"
        _seed_task(tid, "4065")
        sent = []
        monkeypatch.setattr(ep, "_list_issue_comments_card", lambda *a, **k: [])
        monkeypatch.setattr(ep, "_post_issue_comment_ledgered",
                            lambda repository, num, *a, **k: sent.append(num) or True)
        ep._post_result_to_issue("grape-dev/grape", "#4065", self.STRUCTURED,
                                 "tok", "sre", GITCODE_RK, agent_result=True, task_id=tid)
        assert sent == ["4065"]
        assert not _records(tid, "RESULT_COMMENT_SKIPPED")


# =====================================================================
# 4. 指标：reason 维度聚合 + 连续失败告警
# =====================================================================

class TestCommentMetrics:
    def test_reason_enum_and_classification(self):
        from src.kernel import comment_metrics as cm
        assert cm.FAIL_REASONS == ("non_numeric_ident", "http_4xx", "http_5xx",
                                   "network", "unknown")
        assert cm.classify_failure(
            "post issue comment failed: {'error': 'HTTP 400', 'status_code': 400}") == "http_4xx"
        assert cm.classify_failure(
            "post issue comment failed: {'error': 'HTTP 502', 'status_code': 502}") == "http_5xx"
        assert cm.classify_failure("urllib timeout: timed out") == "network"
        assert cm.classify_failure("something odd") == "unknown"

    def test_failed_total_aggregates_by_reason(self):
        from prometheus_client import CollectorRegistry
        from src.kernel import comment_metrics as cm
        reg = CollectorRegistry()
        cm.install(reg)
        before = reg.get_sample_value("grape_comment_post_failed_total",
                                      {"reason": "http_4xx"}) or 0.0
        cm.observe_failure("http_4xx", repo_key="gitcode:grape-dev/grape")
        cm.observe_failure("http_4xx", repo_key="gitcode:grape-dev/grape")
        after = reg.get_sample_value("grape_comment_post_failed_total",
                                     {"reason": "http_4xx"}) or 0.0
        assert after - before == 2.0

    def test_unknown_reason_folds_to_unknown(self):
        from prometheus_client import CollectorRegistry
        from src.kernel import comment_metrics as cm
        reg = CollectorRegistry()
        cm.install(reg)
        cm.observe_failure("weird_free_text", repo_key="gitcode:grape-dev/grape")
        assert (reg.get_sample_value("grape_comment_post_failed_total",
                                     {"reason": "unknown"}) or 0.0) >= 1.0
        assert reg.get_sample_value("grape_comment_post_failed_total",
                                    {"reason": "weird_free_text"}) is None

    def test_consecutive_failures_alert_at_threshold(self, monkeypatch):
        from src.kernel import comment_metrics as cm
        cm.reset_streaks()
        monkeypatch.setenv("GRAPE_COMMENT_POST_FAIL_ALERT_THRESHOLD", "3")
        results = [cm.observe_failure("http_4xx", repo_key="gitcode:grape-dev/grape")
                   for _ in range(3)]
        assert [r["consecutive"] for r in results] == [1, 2, 3]
        assert results[-1]["alert"] is True
        assert results[0]["alert"] is False
        # 成功投递清零
        cm.observe_delivered("gitcode:grape-dev/grape")
        assert cm.observe_failure("http_4xx", repo_key="gitcode:grape-dev/grape")["consecutive"] == 1

    def test_threshold_default_and_env_clamp(self, monkeypatch):
        from src.kernel import comment_metrics as cm
        monkeypatch.delenv("GRAPE_COMMENT_POST_FAIL_ALERT_THRESHOLD", raising=False)
        assert cm.alert_threshold() == 5
        monkeypatch.setenv("GRAPE_COMMENT_POST_FAIL_ALERT_THRESHOLD", "9999")
        assert cm.alert_threshold() == 1000

    def test_skip_counter_tracks_reason(self):
        from prometheus_client import CollectorRegistry
        from src.kernel import comment_metrics as cm
        reg = CollectorRegistry()
        cm.install(reg)
        before = reg.get_sample_value("grape_comment_post_skipped_total",
                                      {"reason": "non_numeric_ident"}) or 0.0
        cm.observe_skipped("non_numeric_ident", repo_key="gitcode:grape-dev/grape")
        after = reg.get_sample_value("grape_comment_post_skipped_total",
                                     {"reason": "non_numeric_ident"}) or 0.0
        assert after - before == 1.0

    def test_thread_safe_streak_counter(self):
        from src.kernel import comment_metrics as cm
        cm.reset_streaks()
        for _ in range(4):
            threading.Thread(target=cm.observe_failure,
                             args=("network", "gitcode:grape-dev/grape")).start()
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join()
        assert cm.consecutive_failures("gitcode:grape-dev/grape") == 4