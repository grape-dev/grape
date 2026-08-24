"""RuntimeService 任务队列测试（见 acceptance-plan M1-R1~R7 / concurrency CC-3）"""
import json, threading
import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean_tasks():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()


def _enqueue(session_id="sess-1", runtime_id=1, prompt="p", seq=1, issue_session_id="", issue_id="#5"):
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt=prompt,
        event_seq=seq, issue_id=issue_id, repo_key="gitcode:test/repo", agent_type="issue",
        skill="issue", env_extra={"K": "v"}, issue_session_id=issue_session_id,
    )


class TestEnqueue:
    def test_requires_session_and_runtime(self):
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="", runtime_id=1, prompt="p")
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="s", runtime_id=0, prompt="p")

    def test_snapshot_fields(self):
        tid = _enqueue(seq=7)
        t = service.get_task(tid)
        assert t["agent_session_id"] == "sess-1" and t["runtime_id"] == 1
        assert t["event_seq"] == 7 and t["issue_id"] == "#5"
        assert t["status"] == "queued"
        assert json.loads(t["env_extra"])["K"] == "v"

    def test_packs_skill_files(self):
        import os
        from src.config import SKILLS_DIR
        # 直接指定 skill_path，不依赖 skill 表（避免全量测试顺序污染）
        tid = service.enqueue_task(
            agent_session_id="sess-1", runtime_id=1, prompt="p", event_seq=1,
            issue_id="#5", repo_key="test/repo", agent_type="issue",
            skill="issue", skill_path=os.path.join(SKILLS_DIR, "issue", "SKILL.md"),
            env_extra={"K": "v"},
        )
        t = service.get_task(tid)
        sf = json.loads(t["skill_files"])
        assert "issue" in sf, "skill_files 应含 issue 技能包"
        assert "SKILL.md" in sf["issue"], "SKILL.md 应打包"


class TestClaim:
    def test_claim_routing(self):
        a = _enqueue(session_id="s1", runtime_id=1, seq=1)
        b = _enqueue(session_id="s2", runtime_id=2, seq=2)
        got = service.claim(1)
        assert got and got["id"] == a
        got2 = service.claim(1)
        assert got2 is None, "同 runtime 无更多 queued"
        # runtime 2 的任务不受影响
        got3 = service.claim(2)
        assert got3 and got3["id"] == b

    def test_claim_serial_slot_d9(self):
        """同 agent_session_id 已有 dispatched → 不 claim（严格串行）"""
        _enqueue(session_id="s1", runtime_id=1, seq=1)
        _enqueue(session_id="s1", runtime_id=1, seq=2)
        first = service.claim(1)
        assert first and first["agent_session_id"] == "s1"
        second = service.claim(1)
        assert second is None, "同 session 已有活跃 task，不得 claim（D9）"

    def test_claim_serial_by_issue_session(self):
        """同 issue_session_id 严格串行（M2，见 multi-issue-isolation-design.md §3.3）"""
        isess = "isess-1"
        _enqueue(session_id="s1", runtime_id=1, seq=1, issue_session_id=isess)
        _enqueue(session_id="s1", runtime_id=1, seq=2, issue_session_id=isess)
        first = service.claim(1)
        assert first["issue_session_id"] == isess
        second = service.claim(1)
        assert second is None, "同 Issue 已有活跃 task，不得并行（串行槽）"

    def test_claim_parallel_across_issues(self):
        """不同 issue_session_id 可并行（M2 核心：跨 Issue 不互相阻塞）"""
        a = _enqueue(session_id="s1", runtime_id=1, seq=1, issue_session_id="isess-A", issue_id="#5")
        b = _enqueue(session_id="s1", runtime_id=1, seq=2, issue_session_id="isess-B", issue_id="#6")
        got_a = service.claim(1)
        got_b = service.claim(1)
        assert got_a and got_b, "不同 Issue 的两个 queued 任务都应被 claim（并行）"
        assert {got_a["id"], got_b["id"]} == {a, b}

    def test_claim_fallback_to_agent_session(self):
        """issue_session_id 为空（旧任务/#0）→ 回退按 agent_session_id 串行"""
        _enqueue(session_id="s1", runtime_id=1, seq=1, issue_id="#0")
        _enqueue(session_id="s1", runtime_id=1, seq=2, issue_id="#0")
        first = service.claim(1)
        assert first
        second = service.claim(1)
        assert second is None, "无 issue_session_id 时按 agent_session_id 串行（现状语义）"

    def test_complete_updates_issue_session(self):
        """complete 后新会话续接指针回写 Issue 级会话"""
        from src.api.session_store import get_or_create_issue_session, get_issue_session
        isess = get_or_create_issue_session("gitcode:test/repo", "issue", "#5")
        _enqueue(session_id="s1", runtime_id=1, seq=1, issue_session_id=isess["id"], issue_id="#5")
        tid = service.claim(1)["id"]
        service.complete_task(tid, "done", 0, usage={"session_id": "new_ses"})
        got = get_issue_session("gitcode:test/repo", "issue", "#5")
        assert got["runtime_session_id"] == "new_ses", "续接指针应写回 Issue 级会话"

    def test_claim_order_fifo(self):
        _enqueue(session_id="s1", runtime_id=1, seq=1)
        _enqueue(session_id="s2", runtime_id=1, seq=2)
        first = service.claim(1)
        assert first["event_seq"] == 1
        second = service.claim(1)
        assert second["event_seq"] == 2

    def test_atomic_claim_concurrent(self):
        """并发抢单：同一任务仅一个 worker 能 claim 成功（CC-3）"""
        tid = _enqueue(session_id="s1", runtime_id=1, seq=1)
        results = []
        lock = threading.Lock()

        def _w():
            r = service.claim(1)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_w) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        claimed = [r for r in results if r]
        assert len(claimed) == 1, f"同一任务并发抢单应仅 1 个成功, got {len(claimed)}"
        assert claimed[0]["id"] == tid


class TestTerminal:
    def test_complete_idempotent(self):
        tid = _enqueue(seq=1)
        service.claim(1)
        service.complete_task(tid, "done", 0)
        service.complete_task(tid, "done again", 0)
        t = service.get_task(tid)
        assert t["status"] == "completed"
        assert t["result_text"] == "done"

    def test_fail_sets_failed(self):
        tid = _enqueue(seq=1)
        service.claim(1)
        service.fail_task(tid, "boom", 1)
        t = service.get_task(tid)
        assert t["status"] == "failed"
        assert "boom" in t["errors"]

    def test_cancel(self):
        tid = _enqueue(seq=1)
        service.cancel_task(tid)
        t = service.get_task(tid)
        assert t["status"] == "cancelled"

    def test_unknown_task_noop(self):
        service.complete_task("t_nonexistent", "x", 0)
        service.fail_task("t_nonexistent", "x")


class TestBusinessFailureDetection:
    """业务失败检测：进程 exit_code=0 但业务实际失败 → 覆盖为 failed"""

    def test_sre_deploy_failure_detected(self):
        """SRE 结论含'部署中止/失败'→ exit_code 覆盖为 1, status=failed"""
        from src.api.event_store import parse_agent_output_json
        text = ('由于华为云账号未完成实名认证，无法购买ECS实例（Ecs.0707）。部署无法继续。\n'
                '```json\n{"conclusion":"部署中止，账号未实名认证","next_step":"请完成实名认证后重新部署"}\n```')
        output = parse_agent_output_json(text)
        assert service._detect_business_failure("sre", output, text) is True

    def test_sre_deploy_success_not_flagged(self):
        """SRE 结论含'部署成功'→ 不触发失败检测"""
        from src.api.event_store import parse_agent_output_json
        text = '```json\n{"conclusion":"ECS创建成功，应用已部署","next_step":"访问 http://1.2.3.4 验证"}\n```'
        output = parse_agent_output_json(text)
        assert service._detect_business_failure("sre", output, text) is False

    def test_non_sre_not_checked(self):
        """非 SRE agent（issue/coding/qa）→ 不检测，返回 False"""
        from src.api.event_store import parse_agent_output_json
        text = '```json\n{"conclusion":"部署失败，无法继续"}\n```'
        output = parse_agent_output_json(text)
        assert service._detect_business_failure("issue", output, text) is False
        assert service._detect_business_failure("coding", output, text) is False

    def test_complete_task_overrides_exit_code_on_business_failure(self):
        """complete_task 收到 exit_code=0 但业务失败 → task status=failed"""
        # enqueue SRE 任务
        tid = service.enqueue_task(
            agent_session_id="s1", runtime_id=1, prompt="p",
            event_seq=99, issue_id="#1", repo_key="gitcode:test/repo",
            agent_type="sre", skill="cloud-deploy",
        )
        service.claim(1)
        result_text = ('部署无法继续\n```json\n{"conclusion":"ECS创建失败(Ecs.0707)，部署中止",'
                        '"next_step":"完成实名认证后重新触发部署"}\n```')
        service.complete_task(tid, result_text, 0)
        t = service.get_task(tid)
        assert t["status"] == "failed", f"业务失败应标 failed, got {t['status']}"
        assert t["exit_code"] != 0, f"exit_code 应被覆盖为非0, got {t['exit_code']}"

    def test_complete_task_keeps_success_on_real_success(self):
        """complete_task 收到 exit_code=0 且业务成功 → task status=completed"""
        tid = service.enqueue_task(
            agent_session_id="s1", runtime_id=1, prompt="p",
            event_seq=98, issue_id="#1", repo_key="gitcode:test/repo",
            agent_type="sre", skill="cloud-deploy",
        )
        service.claim(1)
        result_text = '```json\n{"conclusion":"ECS部署成功，服务已上线","next_step":"验证访问"}\n```'
        service.complete_task(tid, result_text, 0)
        t = service.get_task(tid)
        assert t["status"] == "completed", f"业务成功应保持 completed, got {t['status']}"
        assert t["exit_code"] == 0


class TestWaitTaskTerminal:
    """wait_task_terminal：入队 + 轮询终态（IM/web 队列路径的回报助手，见 multi-runtime-instance-design 配套）"""

    def test_wait_returns_terminal_dict(self):
        import asyncio
        tid = _enqueue(seq=1)
        service.claim(1)
        service.complete_task(tid, "ok", 0)
        t = asyncio.run(service.wait_task_terminal(tid, poll_interval=0.05))
        assert t["status"] == "completed"
        assert t["result_text"] == "ok"

    def test_wait_times_out_on_queued(self):
        import asyncio
        tid = _enqueue(seq=1)  # 保持 queued，无人 claim
        with pytest.raises(TimeoutError):
            asyncio.run(service.wait_task_terminal(tid, timeout=0.2, poll_interval=0.05))

    def test_wait_polls_until_terminal(self):
        """任务稍后完成：应轮询直到终态（而非只查一次）"""
        import asyncio, threading, time
        tid = _enqueue(seq=1)

        def _finish():
            time.sleep(0.3)
            service.complete_task(tid, "late-ok", 0)

        th = threading.Thread(target=_finish)
        th.start()
        try:
            t = asyncio.run(service.wait_task_terminal(tid, timeout=5, poll_interval=0.1))
        finally:
            th.join()
        assert t["status"] == "completed"
        assert t["result_text"] == "late-ok"

    def test_wait_returns_failed_dict(self):
        import asyncio
        tid = _enqueue(seq=1)
        service.claim(1)
        service.fail_task(tid, "boom")
        t = asyncio.run(service.wait_task_terminal(tid, poll_interval=0.05))
        assert t["status"] == "failed"
        assert "boom" in t["errors"]


class TestMultiInstanceRouting:
    """R2/R3：多实例路由（S2 健康实例选择）与实例级隔离队列（multi-runtime-instance-design §4）"""

    @staticmethod
    def _seed_runtimes(rows):
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes")
            for r in rows:
                c.execute(
                    "INSERT INTO agent_runtimes (credential_id, name, scope, owner_id, status, last_seen_at) VALUES (?,?,?,?,?,?)",
                    (r["credential_id"], r["name"], r.get("scope", "global"), r.get("owner_id", ""),
                     r.get("status", "offline"), r.get("last_seen_at")))
            db.commit()

    def test_pick_online_instance(self):
        from src.user.db import get_runtime_id_by_name
        self._seed_runtimes([
            {"credential_id": 1, "name": "opencode", "status": "offline", "last_seen_at": None},
            {"credential_id": 2, "name": "opencode", "status": "online", "last_seen_at": "2026-08-09T00:00:00+00:00"},
        ])
        rid = get_runtime_id_by_name("opencode")
        from src.session_manager import db, db_lock
        with db_lock:
            st = db.cursor().execute("SELECT status FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
        assert st == "online", f"应优先选 online 实例, got id={rid} status={st}"

    def test_fallback_when_all_offline(self):
        from src.user.db import get_runtime_id_by_name
        self._seed_runtimes([
            {"credential_id": 1, "name": "opencode", "status": "offline", "last_seen_at": None},
            {"credential_id": 2, "name": "opencode", "status": "offline", "last_seen_at": None},
        ])
        assert get_runtime_id_by_name("opencode") is not None, "全 offline 应回退任意实例保证可入队"

    def test_ensure_default_no_dup(self):
        from src.user.db import ensure_default_runtime
        self._seed_runtimes([
            {"credential_id": 1, "name": "opencode", "status": "online", "last_seen_at": "2026-08-09T00:00:00+00:00"},
        ])
        rid = ensure_default_runtime("opencode")
        from src.session_manager import db, db_lock
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='opencode'").fetchone()[0]
        assert cnt == 1, f"ensure_default_runtime 不应重复插入, got {cnt}"
        assert rid is not None

    def test_claim_isolation_per_instance(self):
        """R3：任务绑实例 A → 仅 A 能 claim，实例 B 的 claim 拿不到（跨实例隔离队列）"""
        self._seed_runtimes([
            {"credential_id": 1, "name": "opencode", "status": "online", "last_seen_at": "2026-08-09T00:00:00+00:00"},
            {"credential_id": 2, "name": "opencode", "status": "online", "last_seen_at": "2026-08-09T00:00:00+00:00"},
        ])
        from src.session_manager import db, db_lock
        with db_lock:
            r1, r2 = [r[0] for r in db.cursor().execute("SELECT id FROM agent_runtimes ORDER BY id").fetchall()]
        _enqueue(session_id="s-m1", runtime_id=r1, seq=1)
        _enqueue(session_id="s-m2", runtime_id=r2, seq=2)
        got1 = service.claim(r1)
        got2 = service.claim(r2)
        assert got1 and got2 and got1["id"] != got2["id"], "两个实例各自 claim 各自队列"
        assert service.claim(r1) is None, "实例 1 不得跨实例 claim"
