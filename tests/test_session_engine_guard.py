"""session 引擎守卫实际执行测试（session-engine-guard）。

覆盖三条核心路径：
1. 守卫函数本身：跨引擎指针弃用 / 同家放行 / 引擎未知放行（fail-safe）
2. claim 兜底守卫：广播模式任务领取时跨引擎指针置空
3. 死 session 检测清理：指针 + 引擎标记一起清
"""
import pytest

from src.session_manager import db, db_lock
from src.api import session_store


@pytest.fixture(autouse=True)
def _clean_issue_sessions():
    with db_lock:
        db.cursor().execute("DELETE FROM issue_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()


class TestGuardIssueSessionPointer:
    def test_same_engine_passthrough(self):
        isess = {"runtime_session_id": "ses_op_1", "session_engine": "opencode"}
        assert session_store.guard_issue_session_pointer(isess, "opencode") == "ses_op_1"

    def test_cross_engine_dropped(self):
        isess = {"runtime_session_id": "ses_op_1", "session_engine": "opencode"}
        assert session_store.guard_issue_session_pointer(isess, "hermes") == ""

    def test_unknown_pointer_engine_passthrough(self):
        # 存量行 session_engine 为空 → 放行（fail-safe，不能反推归属）
        isess = {"runtime_session_id": "ses_legacy", "session_engine": ""}
        assert session_store.guard_issue_session_pointer(isess, "hermes") == "ses_legacy"

    def test_unknown_current_engine_passthrough(self):
        # 广播模式/反查失败 → 引擎未知 → 放行
        isess = {"runtime_session_id": "ses_op_1", "session_engine": "opencode"}
        assert session_store.guard_issue_session_pointer(isess, "") == "ses_op_1"

    def test_empty_pointer_returns_empty(self):
        isess = {"runtime_session_id": "", "session_engine": "opencode"}
        assert session_store.guard_issue_session_pointer(isess, "hermes") == ""

    def test_none_isess_tolerated(self):
        assert session_store.guard_issue_session_pointer(None, "hermes") == ""


class TestSessionEngineColumn:
    def test_get_or_create_returns_engine_field(self):
        s = session_store.get_or_create_issue_session("gitcode:org/g1", "coding", "#7")
        assert s.get("session_engine") == ""

    def test_update_issue_session_writes_engine(self):
        s = session_store.get_or_create_issue_session("gitcode:org/g1", "coding", "#7")
        session_store.update_issue_session(s["id"], runtime_session_id="ses_h_1", session_engine="hermes")
        s2 = session_store.get_issue_session("gitcode:org/g1", "coding", "#7")
        assert s2["runtime_session_id"] == "ses_h_1"
        assert s2["session_engine"] == "hermes"

    def test_overwrite_cross_engine_pointer(self):
        # B2 修正后的语义：回写直接覆盖为最新产出引擎（不跳过）
        s = session_store.get_or_create_issue_session("gitcode:org/g1", "coding", "#7")
        session_store.update_issue_session(s["id"], runtime_session_id="ses_op_1", session_engine="opencode")
        session_store.update_issue_session(s["id"], runtime_session_id="ses_h_1", session_engine="hermes")
        s2 = session_store.get_issue_session("gitcode:org/g1", "coding", "#7")
        assert s2["runtime_session_id"] == "ses_h_1"
        assert s2["session_engine"] == "hermes"


class TestClaimGuardAndDeadSession:
    _ENGINES = [
        (2142, "hermes"),
        (2143, "codearts"),
    ]

    @pytest.fixture(autouse=True)
    def _setup_engines(self):
        """测试库造引擎行 + 能力标签行 + 新鲜心跳（claim_by_tag 健康分检查需要）。

        last_seen_at 用 SQLite datetime('now')（UTC，与 _fetch_heartbeat 的 parse 判定同源），
        空/过期 → 失联 → 健康分 0 → 拒绝认领。
        """
        with db_lock:
            c = db.cursor()
            for rid, name in self._ENGINES:
                c.execute(
                    "INSERT OR REPLACE INTO agent_runtimes (id, name, credential_id, status, worker_version, last_seen_at) "
                    "VALUES (?,?,0,'online','',datetime('now'))", (rid, name))
                c.execute(
                    "INSERT OR REPLACE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)", (rid,))
            db.commit()
        yield

    def _claim(self, engine_rid):
        """plugin 模式下走 claim_by_tag（与真实 worker 一致）。"""
        from src.runtime import service
        return service.claim_by_tag("all", engine_rid)

    def _setup_task(self, runtime_id, ptr_engine, sid="ses_op_1"):
        """入队一个带 issue_session 指针的任务，返回 task_id。"""
        from src.runtime import service
        s = session_store.get_or_create_issue_session("gitcode:org/g2", "coding", "#9")
        session_store.update_issue_session(s["id"], runtime_session_id=sid, session_engine=ptr_engine)
        service.enqueue_task(
            agent_session_id=f"claim-guard-test-{ptr_engine}-{sid}", runtime_id=runtime_id,
            pool_agent_type="coding" if not runtime_id else "",
            pool_tag="all" if not runtime_id else "",
            prompt="t", cwd=".", runtime_session_id=sid, event_type="",
            account="", user_id=0, agent_type="coding", skill="", skill_path="",
            skill_id=0, server_url="", issue_session_id=s["id"],
            repo_key="gitcode:org/g2", issue_id="#9", event_seq=0,
        )
        # 从 DB 找回 task_id
        with db_lock:
            row = db.cursor().execute(
                "SELECT id FROM agent_tasks WHERE issue_session_id=? ORDER BY created_at DESC LIMIT 1",
                (s["id"],)).fetchone()
        return row[0], s["id"]

    def test_claim_drops_cross_engine_pointer(self):
        from src.runtime import service
        # 广播任务被 hermes 引擎（runtime_id=2142）领取，指针是 opencode 的
        task_id, _ = self._setup_task(runtime_id=0, ptr_engine="opencode")
        task = self._claim(2142)
        assert task is not None
        assert task["runtime_session_id"] == "", "claim 应置空跨引擎指针"

    def test_claim_keeps_same_engine_pointer(self):
        from src.runtime import service
        # 指针是 hermes 的，hermes 引擎（2142）领取 → 同家保留
        task_id, _ = self._setup_task(runtime_id=0, ptr_engine="hermes")
        task = self._claim(2142)
        assert task is not None
        assert task["runtime_session_id"] == "ses_op_1", "同家引擎指针应保留"

    def test_claim_keeps_unknown_engine_pointer(self):
        from src.runtime import service
        # 存量脏指针（engine 空）→ 放行（fail-safe）
        task_id, _ = self._setup_task(runtime_id=0, ptr_engine="")
        task = self._claim(2142)
        assert task is not None
        assert task["runtime_session_id"] == "ses_op_1", "引擎未知应放行"

    def test_dead_session_clears_pointer_and_engine(self):
        from src.runtime import service
        # 建会话 + 指针，并拿 issue_session_id
        s = session_store.get_or_create_issue_session("gitcode:org/g2", "coding", "#9")
        self._isid = s["id"]
        session_store.update_issue_session(s["id"], runtime_session_id="ses_h_2", session_engine="hermes")
        # 先造 2 条历史负数失败（同一个 issue_session）
        with db_lock:
            for i in range(2):
                db.cursor().execute(
                    "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, cwd, "
                    "runtime_session_id, event_type, account, user_id, agent_type, repo_key, issue_id, "
                    "issue_session_id, status, exit_code, errors, created_at) VALUES "
                    f"('t_hist{i}', 'hist{i}', 0, 't', '.', 'ses_h_2', '', '', 0, 'coding', "
                    f"'gitcode:org/g2', '#9', ?, 'failed', -1, '[]', datetime('now', ?))",
                    (self._isid, f"-{3-i} minutes"))
            db.commit()
        # 第 3 次失败走 fail_task → 触发死 session 检测（LIMIT 3 = hist0, hist1, 本次）
        task_id, _ = self._setup_task(runtime_id=0, ptr_engine="hermes", sid="ses_h_2")
        service.fail_task(task_id, "simulated timeout", exit_code=-1)
        s2 = session_store.get_issue_session("gitcode:org/g2", "coding", "#9")
        assert s2["runtime_session_id"] == "", "死 session 检测应清指针"
        assert s2["session_engine"] == "", "死 session 检测应连引擎标记一起清"
