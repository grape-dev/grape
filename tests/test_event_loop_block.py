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
            return heartbeats["n"], client

        hb_count, client = asyncio.run(_run())

        # 5 个事件 × 0.02s = 0.1s 执行窗口；heartbeat 每 0.005s → 期望约 20 次
        # 修复后（迭代让出）应远大于 1；若阻塞则接近 0
        assert hb_count >= 3, (
            f"事件循环被同步生成器阻塞：heartbeat 仅调度 {hb_count} 次（期望持续调度）。"
            f"修复：_execute_task 迭代中 await asyncio.sleep(0) 让出事件循环。"
        )

    def test_task_start_and_complete_called(self, monkeypatch):
        """慢生成器执行完后，task_start 与 complete 应被正常调用。"""
        monkeypatch.setattr("src.runtime.executor.stream_runtime", _slow_stream_runtime)
        client = _SlowClient()
        task = self._build_task(monkeypatch)
        asyncio.run(runtime_agent._execute_task(client, task))
        assert "t_slow_1" in client.started
        assert any(tid == "t_slow_1" for tid, _ in client.completed), "慢任务完成后应 complete"
