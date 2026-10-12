"""事件活跃守卫 + 双投去重回归测试（duplicate-issue-opened-event-fix-design.md 方案 A/C/D）。

#296 事故（2026-10-11）：Task Center 新建需求同一秒落两条 issue_opened
（#10112 GitCode 原生 webhook + #10113 create_task 同步补发合成事件）→ vod 双跑 → 双评论。
本文件验证：守卫表跨进程原子抢占（A）、近 60s 窗口（C）、终态/TTL 释放、create_task 按平台发原生 payload（D）。
"""
import json
from unittest.mock import patch

from src.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.api import event_store as es


RK = "gitcode:test/repo"
_PREFIX = """repo_key LIKE 'gitcode:test/%' OR repo_key LIKE 'test/%'"""
_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/%'",
]


def _clean():
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql)
        db.commit()


def _run_opened(payload: dict, kind: str = "gitcode"):
    """把一次 issue_opened 事件喂给 run_pipeline（GitCode 原生 / GitHub 形态）。"""
    raw = json.dumps(payload).encode()
    if kind == "github":
        headers = {"X-GitHub-Event": "issues"}
    else:
        headers = {"X-GitCode-Event": "issue_opened"}
    return ep.run_pipeline(raw, headers, account="owner/repo", repo_key_override="test/repo")


def _gitcode_payload(num=5, title="T"):
    return {"object_kind": "issue", "project": {"path_with_namespace": "owner/repo"},
            "object_attributes": {"id": num, "iid": num, "title": title, "action": "open"},
            "user": {"username": "alice"}}


def _github_synthetic_payload(num=5, title="T"):
    """create_task 旧实现/真实 GitHub webhook 的 GitHub 格式 payload（方案 D 前）。"""
    return {"action": "opened", "issue": {"number": num, "title": title, "user": {"login": "alice"}},
            "repository": {"full_name": "owner/repo"}, "sender": {"login": "alice"}}


def _event_count(repo_key=RK, issue="#5", agent="vod", evt="issue_opened"):
    with db_lock:
        row = db.cursor().execute(
            "SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? AND event_type=?",
            (repo_key, issue, agent, evt)).fetchone()
        return (row[0] if row else 0)


class TestEventActiveGuardUnit:
    def test_claim_then_release(self):
        _clean()
        assert es.claim_event_guard(RK, "#5", "vod") is True
        assert es.claim_event_guard(RK, "#5", "vod") is False, "同 key 二次抢占必须失败"
        es.update_event_guard_seq(RK, "#5", "vod", 99)
        es.release_event_guard(RK, "#5", "vod", 99)
        assert es.claim_event_guard(RK, "#5", "vod") is True, "释放后可重新抢占"

    def test_release_ignores_wrong_seq(self):
        _clean()
        es.claim_event_guard(RK, "#5", "qa")
        es.update_event_guard_seq(RK, "#5", "qa", 11)
        es.release_event_guard(RK, "#5", "qa", 12)  # 错误 seq → 不释放
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM event_active_guard WHERE repo_key=? AND issue_id=? AND agent_type=?",
                (RK, "#5", "qa")).fetchone()
        assert row[0] == 1

    def test_purge_stale(self):
        _clean()
        es.claim_event_guard(RK, "#5", "vod")
        with db_lock:
            db.cursor().execute(
                "UPDATE event_active_guard SET created_at=datetime('now','-3 hours') "
                "WHERE repo_key=? AND issue_id=? AND agent_type=?", (RK, "#5", "vod"))
            db.commit()
        assert es.purge_stale_event_guards(max_age_seconds=3600) >= 1
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM event_active_guard WHERE repo_key=? AND issue_id=? AND agent_type=?",
                (RK, "#5", "vod")).fetchone()
        assert row[0] == 0


class TestRunPipelineDoubleOpenedDedup:
    """#296 等价场景：同 (repo,issue,agent,event) 双通道并发 → 只落 1 条事件。"""

    def _install_routes(self):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO event_route_config (repo_key, event_type, skill) VALUES ('','issue_opened','issue')")
            c.execute("INSERT OR REPLACE INTO agent_type (agent_type, display_name, enable, order_num, subscribe_events) "
                      "VALUES ('issue','Issue Agent',1,2,'[\"issue_opened\",\"issue_updated\",\"issue_comment\"]')")
            db.commit()

    def test_webhook_then_synthetic_both_land_once(self):
        _clean()
        self._install_routes()
        # 补发先到（GitCode 原生形态，方案 D）
        r1 = _run_opened(_gitcode_payload(), "gitcode")
        assert _event_count() == 1
        # 另一通道（GitHub 形态 = 真实 GitHub webhook / 方案 D 前的合成补发）随后到 → 去重跳过
        r2 = _run_opened(_github_synthetic_payload(), "github")
        assert _event_count() == 1, "双通道只能落 1 条 issue_opened"
        assert r2.get("skipped") in ("duplicate_active_task", "duplicate_recent_window"), r2
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM event_skipped WHERE repo_key=? AND reason LIKE 'dedup:%'", (RK,)).fetchone()
        assert row[0] >= 1, "应有 dedup 审计记录"

    def test_after_terminal_and_window_allowed(self):
        _clean()
        self._install_routes()
        r1 = _run_opened(_gitcode_payload(), "gitcode")
        assert _event_count() == 1
        # 标记终态 + 回退 created_at 越过 60s 窗（终态释放守卫后允许新一轮）
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE events SET status='completed', created_at=datetime('now','-2 minutes') "
                      "WHERE repo_key=? AND issue_id=? AND agent_type=? AND event_type=?",
                      (RK, "#5", "vod", "issue_opened"))
            db.commit()
        with db_lock:
            row = db.cursor().execute(
                "SELECT seq FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? AND event_type=? "
                "ORDER BY seq DESC LIMIT 1", (RK, "#5", "vod", "issue_opened")).fetchone()
        prev_seq = row[0]
        es.release_event_guard(RK, "#5", "vod", prev_seq)  # 终态释放（按事件真实 seq）
        r2 = _run_opened(_gitcode_payload(), "gitcode")
        assert r2.get("skipped") not in ("duplicate_active_task", "duplicate_recent_window"), r2
        assert _event_count() == 2, "终态 + 越过近窗后允许新一轮事件"

    def test_diff_issue_not_blocked(self):
        _clean()
        self._install_routes()
        _run_opened(_gitcode_payload(5), "gitcode")
        _run_opened(_gitcode_payload(6, "T2"), "gitcode")
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM events WHERE repo_key=? AND event_type=? AND agent_type=?",
                (RK, "issue_opened", "vod")).fetchone()
        assert row[0] == 2, "不同 issue 互不影响"


class TestCreateTaskGitcodeNativePayload:
    """方案 D：create_task_via_issue 对 GitCode 发 GitCode 原生 payload（不再发 GitHub 格式）。"""

    def test_payload_builder_native_shape(self):
        """直接断言 create_task 补发 payload 分支：GitCode 原生 vs GitHub 格式。"""
        from src.api.issues_center import create_task_via_issue
        import inspect
        # 方案 D：源码中 GitCode 分支必须构造 object_attributes/project（原生），
        # 不得再以 action/issue/repository/sender 的 GitHub 四键喂给 gitcode。
        src = inspect.getsource(create_task_via_issue)
        assert '"object_kind": "issue"' in src
        assert '"object_attributes"' in src
        assert '"path_with_namespace"' in src
        assert '"X-Gitcode-Event": "issue_opened"' in src
        assert '"X-GitHub-Event": "issues"' in src  # GitHub 分支保留
        # GitCode 分支不再出现 GitHub 格式四键
        gitcode_seg = src.split('_is_github:')[1] if '_is_github:' in src else None
        assert gitcode_seg is None or '"sender"' not in gitcode_seg.lower()