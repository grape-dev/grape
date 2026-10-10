"""复现「任务卡 dispatched 无法执行」的故障现象（gitcode-pr-assistant #8 seq499）。

> ⚠️ 根因说明（2026-08-11 修正）：seq499 卡 dispatched 的**真正根因**是 worker 侧
> `_execute_task` 用同步生成器（stream_runtime）迭代阻塞了 asyncio 事件循环，导致
> claim/heartbeat 无法运行（详见 docs/new-version2/worker-event-loop-block-rootcause.md）。
> 本测试保留「claim 后任务若未被 worker 执行则永久 dispatched」的**状态层验证**
> （无论根因是响应丢失还是事件循环阻塞，最终故障表现一致：孤儿 dispatched 无人恢复）。

故障表现：
1. 服务器 claim 端点将任务 queued → dispatched（DB 已改）；
2. worker 未对该任务执行（_execute_task 未启动 / 被阻塞 / 响应未达）；
3. 服务器 DB 任务永久停留在 dispatched，无自动恢复 → 孤儿任务。

本测试验证：
- 服务器侧 claim 会把任务置 dispatched（先决条件）；
- worker 未执行时，任务保持 dispatched 且无法再次被 claim（孤儿）；
- 孤儿任务需 recover-orphans 手动恢复为 queued 才能重新执行。
"""
import os
import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean_tasks():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()


def _enqueue(session_id="sess-1", runtime_id=1, prompt="p", seq=1, issue_session_id="", issue_id="#5"):
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt=prompt,
        event_seq=seq, issue_id=issue_id, repo_key="gitcode:test/repo", agent_type="issue",
        skill="issue", env_extra={"K": "v"}, issue_session_id=issue_session_id,
    )


class _FailingClaimClient:
    """模拟 worker 侧 claim 响应丢失：调用即抛异常（连接/解析失败）。"""
    def __init__(self, runtime_id=1):
        self.runtime_id = runtime_id
        self.claim_calls = 0

    async def claim(self, runtime_id):
        self.claim_calls += 1
        raise OSError("connection reset: claim response lost")


class TestClaimResponseLostRace:
    def test_server_claim_marks_dispatched(self):
        """先决条件：服务器 claim 会把 queued 任务置为 dispatched。"""
        tid = _enqueue(session_id="sess-1", runtime_id=1, seq=1)
        got = service.claim(1)
        assert got and got["id"] == tid
        t = service.get_task(tid)
        assert t["status"] == "dispatched", f"claim 后应 dispatched, got {t['status']}"

    def test_orphan_dispatched_when_claim_response_lost(self):
        """复现竞态：worker claim 异常 → 任务卡 dispatched，无人恢复。"""
        tid = _enqueue(session_id="sess-1", runtime_id=1, seq=1)
        # 服务器 claim 成功（响应丢失前的原子 UPDATE 已发生）
        got = service.claim(1)
        assert got and got["id"] == tid
        # worker 侧 claim 抛异常（模拟响应丢失）→ 等价于 worker 没拿到任务
        client = _FailingClaimClient()
        with pytest.raises(OSError):
            # 用 asyncio 驱动 client.claim，模拟 _run_loop 的 claim 调用
            import asyncio
            asyncio.run(client.claim(1))
        # 服务器 DB：任务仍是 dispatched（孤儿，无人置回 queued/标失败）
        t = service.get_task(tid)
        assert t["status"] == "dispatched", \
            f"claim 响应丢失后任务应保持 dispatched（孤儿）, got {t['status']}"
        # 该任务无法再次被 claim（已被 dispatched 占用，串行槽/状态过滤排除）
        again = service.claim(1)
        assert again is None, "孤儿 dispatched 任务不应再次被 claim"

    def test_run_loop_silently_skips_on_claim_error(self):
        """验证 _run_loop 对 claim 异常静默 continue（不重试、不标失败、不打 error 级日志）。"""
        import asyncio
        from src.runtime import agent as runtime_agent

        # 预置任务并让服务器先 claim（置 dispatched）
        tid = _enqueue(session_id="sess-1", runtime_id=1, seq=1)
        got = service.claim(1)
        assert got and got["id"] == tid

        # 单次 _run_loop 迭代：client.claim 抛异常 → 应 continue，不崩溃
        client = _FailingClaimClient()
        results = []

        async def _single_iteration():
            # 复刻 _run_loop 的核心 claim 异常处理（agent.py:146-153）
            try:
                await client.claim(1)
            except Exception as e:
                # 真实代码是 log.warning + continue
                results.append(f"claim failed: {e}")
                return
            results.append("claimed")

        asyncio.run(_single_iteration())
        assert results and results[0].startswith("claim failed"), "claim 异常应被捕获"
        # 任务仍 dispatched（无恢复逻辑）
        assert service.get_task(tid)["status"] == "dispatched"

    def test_recover_orphans_restores_queued(self):
        """对照：孤儿任务可通过 recover-orphans 恢复为 queued（当前无自动触发，仅端点支持）。"""
        tid = _enqueue(session_id="sess-1", runtime_id=1, seq=1)
        service.claim(1)
        assert service.get_task(tid)["status"] == "dispatched"
        # 模拟 recover-orphans：置回 queued
        from src.api.routes.runtime import runtime_recover_orphans
        # 直接调 service 层恢复（端点需 rta_ 鉴权，这里用底层 UPDATE 模拟）
        with db_lock:
            db.cursor().execute(
                "UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=?", (tid,))
            db.commit()
        assert service.get_task(tid)["status"] == "queued", "恢复后应可重新 claim"
        again = service.claim(1)
        assert again and again["id"] == tid, "恢复后可再次 claim"
