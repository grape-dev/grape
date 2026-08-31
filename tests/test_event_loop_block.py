"""复现「worker 事件循环被同步生成器阻塞」根因（见 worker-event-loop-block-rootcause.md）。

真实故障（gitcode-pr-assistant #8 seq499 卡 dispatched）：
- `_execute_task`（src/runtime/agent.py:105-129）用同步 `for` 迭代 `stream_runtime`（同步生成器），
  且仅 batch≥50 才 `await`；
- 长任务执行期间（batch < 50 且无网络 await 时）整个 asyncio 事件循环被同步 for 循环占住；
- `_run_loop` 的 claim / heartbeat 无法调度 → 新任务无法领取（卡 dispatched）、心跳中断。

本测试：
- mock `stream_runtime` 为「每次 yield 前同步 sleep」的慢生成器（模拟 opencode stdout 读取耗时）；
- 在 `_execute_task` 运行期间并行启动一个 heartbeat 计数器 async 任务；
- 断言 heartbeat 能被持续调度（修复方向：迭代中 `await asyncio.sleep(0)` 让出事件循环）。
"""
import asyncio
import os
import time
import pytest

from src.runtime import agent as runtime_agent


class _SlowClient:
    """最小 RuntimeClient mock：记录 report/complete/fail 调用，不真正发网络请求。"""
    def __init__(self):
        self.started = []
        self.messages_batches = 0
        self.completed = []
        self.failed = []

    async def task_start(self, tid):
        self.started.append(tid)

    async def report_messages(self, tid, batch):
        self.messages_batches += 1
        return True  # 非 None 表示服务端仍存活（防 report_messages 返回 None 触发中止）

    async def complete(self, tid, result_text, exit_code, session_id="", work_dir=""):
        self.completed.append((tid, exit_code))

    async def fail(self, tid, error):
        self.failed.append((tid, error))


def _slow_stream_runtime(*args, **kwargs):
    """模拟慢同步生成器：每次 yield 前 sleep 0.02s（模拟 opencode stdout 读取耗时）。"""
    for i in range(5):  # 5 个事件，batch < 50 不触发 await
        time.sleep(0.02)
        yield ("step_start", f"Step {i+1}")
    yield ("done", {"exit_code": 0, "text": "done", "oc_session_id": ""})


def _silent_long_stream_runtime(*args, **kwargs):
    """模拟「长命令静默无输出」：生成器长时间 sleep 阻塞（readline 卡住），期间无任何 yield。

    对应 worker-event-loop-block-rootcause.md §8.3：方案 1（await sleep(0)）在此场景下失效，
    因为生成器停在 readline() 上、没有 yield 就没有让出点。
    """
    for i in range(3):
        time.sleep(0.2)          # 0.2s 长静默（模拟 opencode 长命令无输出）
        yield ("step_start", f"Step {i+1}")
    yield ("done", {"exit_code": 0, "text": "done", "oc_session_id": ""})


class TestEventLoopBlock:
    def _build_task(self, monkeypatch):
        """构造最小 task dict，供 _execute_task 使用。"""
        return {
            "id": "t_slow_1",
            "cwd": "/tmp",
            "prompt": "p",
            "runtime_session_id": "",
            "event_type": "issue_opened",
            "account": "owner/repo",
            "user_id": 0,
            "repo_gitcode_token": "tok",
            "issue_id": "#5",
            "agent_type": "vod",
            "env_extra": "{}",
            "skill_files": "{}",
            "project_info_files": "{}",
            "server_url": "http://localhost:8080",
        }

    def test_heartbeat_scheduled_during_slow_iteration(self, monkeypatch):
        """慢生成器迭代期间，事件循环应持续调度其他 async 任务（heartbeat）。

        修复前：_execute_task 同步 for 迭代 + 无 await，heartbeat 计数几乎不增长；
        修复后：迭代中 await asyncio.sleep(0) 让出，heartbeat 持续增长。
        """
        # patch stream_runtime 为慢同步生成器
        monkeypatch.setattr("src.runtime.executor.stream_runtime", _slow_stream_runtime)

        client = _SlowClient()
        task = self._build_task(monkeypatch)

        async def _run():
            heartbeats = {"n": 0}

            async def _heartbeat():
                while True:
                    heartbeats["n"] += 1
                    await asyncio.sleep(0.005)

            hb_task = asyncio.create_task(_heartbeat())
            exec_task = asyncio.create_task(runtime_agent._execute_task(client, task))
            await exec_task
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            return heartbeats["n"], client, exec_task.result()

        hb_count, client, _result = asyncio.run(_run())

        # 5 个事件 × 0.02s = 0.1s 执行窗口；heartbeat 每 0.005s → 期望约 20 次
        # 修复后（迭代让出）应远大于 1；若阻塞则接近 0
        assert hb_count >= 3, (
            f"事件循环被同步生成器阻塞：heartbeat 仅调度 {hb_count} 次（期望持续调度）。"
            f"修复：_execute_task 迭代中 await asyncio.sleep(0) 让出事件循环。"
        )

    def test_task_start_and_complete_called(self, monkeypatch):
        """慢生成器执行完后，task_start 应被调用且 _execute_task 返回 done 事件。"""
        monkeypatch.setattr("src.runtime.executor.stream_runtime", _slow_stream_runtime)
        client = _SlowClient()
        task = self._build_task(monkeypatch)
        result = asyncio.run(runtime_agent._execute_task(client, task))
        assert "t_slow_1" in client.started
        # _execute_task 返回 done dict（含 exit_code）；complete 由外层 _wrap 调用
        assert isinstance(result, dict) and result.get("exit_code") == 0, \
            f"_execute_task 应返回 done 事件, got {result}"

    def test_heartbeat_scheduled_during_silent_long_command(self, monkeypatch):
        """「长命令静默无输出」场景下事件循环不得被阻塞（worker-event-loop-block-rootcause.md §8.3/8.5）。

        方案 1（await sleep(0)）在生成器停在 readline 上时失效（无 yield 无让出点）；
        方案 2（线程化 _stream_runtime_async）把 readline() 阻塞移到后台线程，事件循环保持调度。
        """
        monkeypatch.setattr("src.runtime.executor.stream_runtime", _silent_long_stream_runtime)
        client = _SlowClient()
        task = self._build_task(monkeypatch)

        async def _run():
            heartbeats = {"n": 0}

            async def _heartbeat():
                while True:
                    heartbeats["n"] += 1
                    await asyncio.sleep(0.005)

            hb_task = asyncio.create_task(_heartbeat())
            exec_task = asyncio.create_task(runtime_agent._execute_task(client, task))
            await exec_task
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            return heartbeats["n"], client, exec_task.result()

        hb_count, client, _result = asyncio.run(_run())

        # 3 个静默段 × 0.2s = 0.6s 执行窗口；heartbeat 每 0.005s → 期望约 120 次
        # 方案 1（sleep(0)）下 0.2s 静默无 yield → heartbeat 被冻结，计数远低于期望；
        # 方案 2（线程化）下生成器阻塞在线程中，事件循环持续调度 heartbeat。
        assert hb_count >= 10, (
            f"长命令静默期间事件循环被阻塞：heartbeat 仅调度 {hb_count} 次（期望持续调度）。"
            f"修复：_stream_runtime_async 线程化迭代 stream_runtime，readline() 阻塞不占事件循环。"
        )
        assert "t_slow_1" in client.started
        # _execute_task 返回 done dict；complete 由外层 _wrap 调用，此处验证执行成功
        assert _result is not None and isinstance(_result, dict) and _result.get("exit_code") == 0, \
            "静默长任务应正常完成"

    def test_worktree_created_before_skill_materialize(self, monkeypatch, tmp_path):
        """方案 A 顺序看护（coding-worktree-skill-collision.md §5.2）：
        _execute_task 必须「先建 git worktree、再物化技能」，否则 cwd 被 .skills 占住会触发
        `git worktree add` "already exists"（#711 根因）。本用例断言 ensure_issue_worktree
        调用时 cwd 的 .skills 尚未被写入。
        """
        import json as _json
        from src.runtime import agent as ra

        # 构造 coding 任务（git agent + 有关联 issue + issue_session_id）
        task = self._build_task(monkeypatch)
        task.update({
            "id": "t_wt_order",
            "agent_type": "coding",
            "issue_id": "#7",
            "issue_session_id": "sess-7",
            "cwd": str(tmp_path / "issues" / "7" / "coding"),
            "skill_files": _json.dumps({"implementation": {"SKILL.md": "# impl\n", "ref.md": "ref\n"}}),
        })

        # 记录 ensure_issue_worktree 调用时 .skills 是否已存在
        seen = {"skills_existed_at_worktree_call": None, "wt_called": False}

        def _fake_ensure_worktree(*a, **k):
            seen["wt_called"] = True
            cwd = task["cwd"]
            seen["skills_existed_at_worktree_call"] = os.path.isdir(os.path.join(cwd, ".skills"))
            return os.path.join(cwd, ".worktree")  # 假 worktree 路径

        monkeypatch.setattr("src.runtime.repogit.ensure_issue_worktree", _fake_ensure_worktree)
        monkeypatch.setattr("src.runtime.executor.stream_runtime", _slow_stream_runtime)

        client = _SlowClient()
        asyncio.run(runtime_agent._execute_task(client, task))

        assert seen["wt_called"], "coding 任务应触发 ensure_issue_worktree"
        assert seen["skills_existed_at_worktree_call"] is False, (
            "修复要求先建 worktree 再物化技能：ensure_issue_worktree 调用时 .skills 不应已存在。"
            "若为 True（先技能后 worktree）→ 触发 #711 already-exists 冲突。"
        )
        # 技能应已物化（在 worktree 之后）
        assert os.path.isfile(os.path.join(task["cwd"], ".skills", "implementation", "SKILL.md"))
