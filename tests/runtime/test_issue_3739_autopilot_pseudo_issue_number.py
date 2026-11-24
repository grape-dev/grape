"""Issue #3739：autopilot 合成伪 issue_number（autopilot-<letters>）不得流入真实 Issue 通道。

基线演进（rebase 到 arch-weekly-11-ljt）：Issue #4065 已把「非数字 ident 可回帖口径」
收敛到 kernel 单点（`deliverable_comment_ident` / `normalize_card_ident`）并落地了
结果评论短路 + 卡片兜底 + 指标。本用例锁定 #3739 的验收语义，实现口径以 #4065
为唯一判据，避免同一判据两套实现漂移。

覆盖（对应 Spec 验收标准）：
- 伪号判定：`autopilot-*` / `pr_*` 在数字平台（gitcode/github/gitlab）恒不可回帖；
  真实号（纯数字、gitee 平台 ident IK*，#3055）不误伤；
- 回帖入口 `_post_result_to_issue`：伪号零 `/comments` 外呼（GET 预检 + POST 评论都不发），
  结论不丢——落 `RESULT_CARD_FALLBACK` 留痕（`agent_tasks.errors`，看板/任务中心可查），
  并落 `RESULT_COMMENT_SKIPPED`（结构化可检索，不再静默吞异常）；
- 回帖入口 `_post_issue_comment_ledgered`：伪号零 VCS 客户端、零外呼、零台账，
  返回 False 且留结构化跳过记录；
- 真实 Issue 回帖零回归（照常 GET + POST）；
- `/internal/issues/card` 非数字 issue_number 不再 422：`autopilot-*` 是 issue 表真实
  TEXT 主键，照常返回卡片；`pr_*`/垃圾形态 400 + 可读原因，不进查询层。

传输层全部 mock（`create_client` / `_do_request`），不触碰真实平台。
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:acme/ap3739"
REPO = "acme/ap3739"
PSEUDO = "autopilot-ab"          # scheduler._autopilot_issue_ref 纯字母合成主键
PR_SUB_PSEUDO = "pr_12_scene7"   # PR 订阅伪 id（#2804 同族）
GITEE_RK = "gitee:GTDTeam/CodeArts"
STRUCTURED = json.dumps({"actions": ["inspect"], "conclusion": "巡检结论：一切正常"},
                        ensure_ascii=False)


# =====================================================================
# 工具
# =====================================================================

def _seed_task(tid="t_3739_ap", issue=PSEUDO, seq=37390001):
    """种一行 autopilot 任务（合成 issue_id 与线上形态一致）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, errors) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "s3739", 0, seq, issue, RK, "sre", "p", ""))
        db.commit()
    return tid


def _seed_event(seq, issue_id, payload, repo_key=RK):
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


@pytest.fixture
def vcs_calls(monkeypatch):
    """记录 VCS 远程调用（method, path）；空列表 = 零外呼。"""
    calls = []

    class _Client:
        owner = "acme"
        repo = "ap3739"

        def _generic_path(self, github_path, alt_path=""):
            return github_path

        def _request(self, method, path, data=None, **kwargs):
            calls.append((method, path))
            return {"id": 1}

    import src.domains.runtime.vcs.api_client as api
    monkeypatch.setattr(api, "create_client", lambda repo_key, token="": _Client())
    return calls


@pytest.fixture
def vcs_forbidden(monkeypatch):
    """任何 VCS 客户端构造都视为违规（伪号零外呼硬断言）。"""
    def _boom(*args, **kwargs):
        raise AssertionError("伪号不得构造 VCS 客户端发起远程请求")

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


# =====================================================================
# 1. 伪号判定：可回帖口径（kernel 单点，#4065）
# =====================================================================

class TestPseudoIdentNotDeliverable:
    @pytest.mark.parametrize("raw", [PSEUDO, "autopilot-xyz", f"#{PSEUDO}", PR_SUB_PSEUDO,
                                    f"#{PR_SUB_PSEUDO}", "", "   ", "0", "not-an-issue!"])
    def test_numeric_platform_rejects(self, raw):
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident(raw, "gitcode") == "", \
            "伪号/垃圾形态在数字平台恒不可回帖"

    def test_gitee_native_ident_deliverable(self):
        """gitee `/issues/{ident}/notes` 接受平台原生 ident（#3055），不得误伤。"""
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident("IKW28R", "gitee") == "IKW28R"

    @pytest.mark.parametrize("raw,expected", [(3739, "3739"), ("3739", "3739"),
                                              ("#3739", "3739")])
    def test_real_number_deliverable(self, raw, expected):
        from src.kernel.issue_ident import deliverable_comment_ident
        assert deliverable_comment_ident(raw, "gitcode") == expected


# =====================================================================
# 2. _post_result_to_issue：伪号零外呼 + 结论留痕落库
# =====================================================================

class TestPostResultToIssuePseudoShortCircuit:
    def test_autopilot_pseudo_no_outbound(self, vcs_forbidden):
        tid = _seed_task()
        from src.domains.runtime.pipeline.event_pipeline import _post_result_to_issue
        _post_result_to_issue(REPO, PSEUDO, STRUCTURED, "fake_token", "sre", RK,
                              agent_result=True, task_id=tid)
        # create_client 被替换为断言失败桩：走到这里即证明零外呼

    def test_pseudo_skip_and_card_fallback_recorded(self, vcs_forbidden):
        tid = _seed_task()
        from src.domains.runtime.pipeline.event_pipeline import _post_result_to_issue
        _post_result_to_issue(REPO, PSEUDO, STRUCTURED, "fake_token", "sre", RK,
                              agent_result=True, task_id=tid)
        skipped = _records(tid, "RESULT_COMMENT_SKIPPED")
        fallback = _records(tid, "RESULT_CARD_FALLBACK")
        assert skipped, "伪号短路必须留结构化跳过记录（平台侧可查）"
        assert skipped[0].get("reason") == "non_numeric_ident"
        assert skipped[0].get("issue_ident") == PSEUDO
        assert fallback, "结论不得丢失：必须落卡片/事件流留痕"
        assert "巡检结论" in json.dumps(fallback[0], ensure_ascii=False)

    def test_pr_subscription_pseudo_no_outbound(self, vcs_forbidden):
        tid = _seed_task(tid="t_3739_pr", issue=PR_SUB_PSEUDO, seq=37390002)
        from src.domains.runtime.pipeline.event_pipeline import _post_result_to_issue
        _post_result_to_issue(REPO, PR_SUB_PSEUDO, STRUCTURED, "fake_token", "sre", RK,
                              agent_result=True, task_id=tid)
        assert _records(tid, "RESULT_COMMENT_SKIPPED"), "pr_* 订阅伪 id 同样短路留痕"

    def test_skip_is_observable(self, vcs_forbidden, log_spy):
        """跳过不是静默吞：生产检索行 `RESULT_COMMENT_SKIPPED reason=... ident=...`。"""
        tid = _seed_task(tid="t_3739_log", seq=37390003)
        from src.domains.runtime.pipeline.event_pipeline import _post_result_to_issue
        _post_result_to_issue(REPO, PSEUDO, STRUCTURED, "fake_token", "sre", RK,
                              agent_result=True, task_id=tid)
        joined = "\n".join(m for _lvl, m in log_spy)
        assert "RESULT_COMMENT_SKIPPED" in joined
        assert "non_numeric_ident" in joined
        assert PSEUDO in joined

    def test_pseudo_task_with_real_number_posts_to_real_issue(self, monkeypatch):
        """双保险：事件 payload 带真实号时凭真实号回帖（不丢结论）。"""
        import src.domains.runtime.pipeline.event_pipeline as ep
        tid, seq = "t_3739_realnum", 37390004
        _seed_task(tid, PSEUDO, seq=seq)
        _seed_event(seq, PSEUDO, {"issue_number": "3739"})
        sent = []
        monkeypatch.setattr(ep, "_list_issue_comments_card", lambda *a, **k: [])
        monkeypatch.setattr(ep, "_post_issue_comment_ledgered",
                            lambda repository, num, *a, **k: sent.append(num) or True)
        ep._post_result_to_issue(REPO, PSEUDO, STRUCTURED, "tok", "sre", RK,
                                 agent_result=True, task_id=tid, seq=seq)
        assert sent == ["3739"], "有真实号必须凭真实号回帖"


# =====================================================================
# 3. 真实 Issue 回帖零回归
# =====================================================================

class TestPostResultToIssueRealNumberRegression:
    def test_real_number_still_posts_comment(self, vcs_calls, monkeypatch):
        tid = _seed_task(tid="t_3739_real", issue="#3739", seq=37390005)
        from src.domains.runtime.pipeline.event_pipeline import _post_result_to_issue
        monkeypatch.setattr(
            "src.kernel.token_service.get_bot_identity_token", lambda rk: "tok-bot")
        with patch("src.api.event_pipeline._ledger_record_comment") as ledger:
            _post_result_to_issue(REPO, "3739", "✅ 已完成", "fake_token", "sre", RK,
                                  agent_result=False, task_id=tid)
        assert any(m == "GET" for m, _p in vcs_calls), "真实号照常 GET 评论列表（去重）"
        assert any(m == "POST" for m, _p in vcs_calls), "真实号照常 POST 评论"
        assert ledger.called, "真实号成功回帖照常记账"
        post = [p for m, p in vcs_calls if m == "POST"][0]
        assert "/issues/3739/comments" in post


# =====================================================================
# 4. _post_issue_comment_ledgered：伪号兜底闸（防御所有调用点）
# =====================================================================

class TestPostIssueCommentLedgeredGuard:
    def test_pseudo_number_short_circuits(self, vcs_forbidden, log_spy):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        with patch("src.api.event_pipeline._ledger_record_comment") as ledger:
            ok = _post_issue_comment_ledgered(REPO, PSEUDO, "hello", "fake_token", repo_key=RK)
        assert ok is False, "伪号必须返回 False（调用方据此走兜底）"
        assert ledger.called is False, "伪号不得记账"
        joined = "\n".join(m for _lvl, m in log_spy)
        assert "RESULT_COMMENT_SKIPPED" in joined, "跳过必须可检索，不得静默"

    def test_pr_subscription_pseudo_short_circuits(self, vcs_forbidden):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        with patch("src.api.event_pipeline._ledger_record_comment") as ledger:
            ok = _post_issue_comment_ledgered(REPO, PR_SUB_PSEUDO, "hello", "fake_token",
                                              repo_key=RK)
        assert ok is False
        assert ledger.called is False

    def test_real_number_still_posts(self, vcs_calls):
        from src.domains.runtime.pipeline.event_pipeline import _post_issue_comment_ledgered
        with patch("src.api.event_pipeline._ledger_record_comment") as ledger:
            ok = _post_issue_comment_ledgered(REPO, "3739", "hi", "fake_token", repo_key=RK)
        assert ok is True
        assert len(vcs_calls) == 1 and vcs_calls[0][0] == "POST"
        assert "/issues/3739/comments" in vcs_calls[0][1]
        assert ledger.called is True


# =====================================================================
# 5. /internal/issues/card：非数字号不再 422
# =====================================================================

class TestIssueCardRoute:
    @pytest.fixture()
    def client_and_fake(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.domains.runtime.routes import internal as internal_mod

        class _FakeRuntime:
            def __init__(self):
                self.calls = []

            def get_issue_card(self, repo_key, issue_number):
                self.calls.append((repo_key, issue_number))
                return {"stage": "done", "sentinel": True}

        fake = _FakeRuntime()
        monkeypatch.setattr(internal_mod, "_get_local", lambda domain: fake)
        app = FastAPI()
        app.include_router(internal_mod.router)
        return TestClient(app), fake

    def test_pseudo_number_no_longer_422(self, client_and_fake):
        """autopilot-* 是 issue 表真实 TEXT 主键：200 + 卡片，不再是 FastAPI 422。"""
        c, fake = client_and_fake
        r = c.get("/internal/issues/card", params={"repo_key": RK, "issue_number": PSEUDO})
        assert r.status_code == 200, "伪号不得再抛 FastAPI int 校验 422"
        assert r.json().get("sentinel") is True
        assert fake.calls == [(RK, PSEUDO)], "合成键照常查卡片（卡片刷新链不得断）"

    def test_pr_pseudo_returns_readable_400(self, client_and_fake):
        c, fake = client_and_fake
        r = c.get("/internal/issues/card", params={"repo_key": RK,
                                                  "issue_number": PR_SUB_PSEUDO})
        assert r.status_code == 400, "不可归一 ident 需可读 400，而非 422/500"
        body = r.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        assert detail.get("error") == "invalid_issue_ident"
        assert fake.calls == [], "不可归一 ident 不得进入查询层"

    def test_numeric_number_unchanged(self, client_and_fake):
        c, fake = client_and_fake
        r = c.get("/internal/issues/card", params={"repo_key": RK, "issue_number": "3739"})
        assert r.status_code == 200
        assert r.json().get("sentinel") is True
        assert fake.calls == [(RK, "3739")], "数字号照常透传 Local 服务（内部归一 int）"