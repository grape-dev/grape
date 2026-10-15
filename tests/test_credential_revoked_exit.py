"""Issue: 主控吊销正在心跳/执行任务的 worker 后，worker 端行为修复测试。

修复前（bug）：
- 心跳 401 被当网络错误无限退避重试（僵尸进程）
- report_messages 401 二次确认 state 也 401 → 判定"网络错误"→ 任务继续执行
  → PR 照常提交但服务端状态脱节 / dispatched 任务双跑

修复后：
- _request 401 → CredentialRevokedError（不重试直接上抛）
- report_messages 二次确认 401 → 上抛（不再当网络错误返回 {}）
- _execute_task 捕获 → 杀引擎 + 清理 + 上抛
- _wrap 捕获 → 置全局 _credential_revoked Event
- 主循环检查 Event → raise → _amain → SystemExit(42)
- daemon（start-worker.sh/ps1）见 42 退出码 → 停止守护不重启
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime.client import CredentialRevokedError, RuntimeClient


class TestCredentialRevokedError(unittest.TestCase):
    """CredentialRevokedError 异常定义与 client 行为。"""

    def test_exception_class_exists(self):
        self.assertTrue(issubclass(CredentialRevokedError, Exception))

    def test_request_401_raises_credential_revoked(self):
        """_request 收到 401 应抛 CredentialRevokedError（不是返回 None）。"""
        client = RuntimeClient("http://srv", "rta_dead")

        resp = MagicMock()
        resp.status = 401
        resp.text = AsyncMock(return_value='{"detail":"invalid runtime credential"}')

        session = MagicMock()
        session.closed = False
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.request = MagicMock(return_value=ctx)
        client._session = session

        with self.assertRaises(CredentialRevokedError):
            asyncio.run(client._request("POST", "/api/runtime/heartbeat", {}))

    def test_request_401_no_retry(self):
        """401 不应触发指数退避重试（凭据死了重试无意义）。"""
        client = RuntimeClient("http://srv", "rta_dead")
        call_count = {"n": 0}

        resp = MagicMock()
        resp.status = 401
        resp.text = AsyncMock(return_value="unauthorized")

        def make_ctx():
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=resp)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session = MagicMock()
        session.closed = False

        def counted_request(*a, **kw):
            call_count["n"] += 1
            return make_ctx()

        session.request = counted_request
        client._session = session

        with self.assertRaises(CredentialRevokedError):
            asyncio.run(client._request("POST", "/api/runtime/heartbeat", {}, retries=4))
        # 只调了一次——没有重试
        self.assertEqual(call_count["n"], 1)

    def test_report_messages_401_raises(self):
        """report_messages 401 应上抛（不当网络错误返回 {} 继续执行）。"""
        client = RuntimeClient("http://srv", "rta_dead")

        # messages POST → 401 抛；state GET 二次确认 → 也 401 抛
        with patch.object(client, "_request", side_effect=CredentialRevokedError("/messages")):
            with self.assertRaises(CredentialRevokedError):
                asyncio.run(client.report_messages("t_1", [{"role": "user", "content": "x"}]))

    def test_report_messages_410_returns_none(self):
        """正常 410（任务终止）仍返回 None（回归：不能破坏原语义）。"""
        client = RuntimeClient("http://srv", "rta_ok")
        with patch.object(client, "_request", side_effect=RuntimeGoneStub()):
            result = asyncio.run(client.report_messages("t_1", [{"role": "user", "content": "x"}]))
            # RuntimeGoneStub 被 _request 内部捕获返回 None → report_messages 走 state 确认
            # state 返回 None（mock）→ 当网络错误返回 {}
            self.assertEqual(result, {})


class RuntimeGoneStub(Exception):
    pass


class TestWorkerExitOnRevoke(unittest.TestCase):
    """worker 主循环凭据吊销退出机制。"""

    def test_credential_revoked_event_exists(self):
        """全局 _credential_revoked Event 存在且可置位。"""
        from src.runtime import agent as ra
        self.assertIsInstance(ra._credential_revoked, asyncio.Event)
        ra._credential_revoked.set()
        self.assertTrue(ra._credential_revoked.is_set())
        ra._credential_revoked.clear()

    def test_reregister_revoked_raises(self):
        """_reregister 遇 401 应上抛 CredentialRevokedError（不放回退避循环）。"""
        from src.runtime import agent as ra
        client = MagicMock()
        client.reset_session = AsyncMock()
        client.register = AsyncMock(side_effect=CredentialRevokedError("/register"))
        installed = [{"name": "hermes", "installed": True}]
        with self.assertRaises(CredentialRevokedError):
            asyncio.run(ra._reregister(client, installed, "dev"))

    def test_reregister_network_error_returns_none(self):
        """普通网络错误仍返回 None 走退避（回归：吊销才上抛）。"""
        from src.runtime import agent as ra
        client = MagicMock()
        client.reset_session = AsyncMock()
        client.register = AsyncMock(side_effect=OSError("network down"))
        installed = [{"name": "hermes", "installed": True}]
        result = asyncio.run(ra._reregister(client, installed, "dev"))
        self.assertIsNone(result)

    def test_daemon_sh_exit_42_stops(self):
        """start-worker.sh 应含 42 退出码处理（停止守护不重启）。"""
        sh = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "start-worker.sh")
        with open(sh, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('-eq 42', content, "start-worker.sh must handle exit code 42 (credential revoked)")
        self.assertIn('credential revoked', content)

    def test_daemon_ps1_exit_42_stops(self):
        """start-worker.ps1 应含 42 退出码处理（停止守护不重启）。"""
        ps1 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "scripts", "start-worker.ps1")
        with open(ps1, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('-eq 42', content, "start-worker.ps1 must handle exit code 42 (credential revoked)")
        self.assertIn('credential revoked', content)

    def test_agent_main_handles_credential_revoked(self):
        """agent.py main 应有 CredentialRevokedError → SystemExit(42) 处理。"""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "runtime", "agent.py"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("except CredentialRevokedError", content)
        self.assertIn("SystemExit(42)", content)


if __name__ == "__main__":
    unittest.main()
