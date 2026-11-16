"""Smoke test: codearts + bwrap --unshare-net 端到端验证。

验证三判据（缺一不可，防止 NullSandbox 假阳性）：
1. 任务返回正确结果（非空/非错误）
2. 审计日志出现 sandbox_access/allow backend=codearts provider=bwrap（非 degrade/NullSandbox）
3. NetworkProxy 日志有对应 network_decision/allow（确认走了代理不是直连漏出）

背景：2026-09-27 验证发现旧的"Bun fetch 不走 HTTP_PROXY"结论是错误的。
codearts 的 proxy-fetch 模块每次 fetch 调 getProxyConfig() 读 HTTP_PROXY env，
传给 Bun fetch 的 proxy 参数。bwrap --unshare-net 下完全有效。

此 smoke test 把验证焊死成脚本——文档会过时，脚本不会。

运行前提：
- grape 3000 服务在跑（direct 模式）
- codearts 引擎已注册且 alive（runtime_id 可配 CODEARTS_RID 环境变量，默认自动解析）
- agent_sandbox=on（不是 off——off 会触发 NullSandbox 降级，判据 2 会失败）
- 审计日志路径 /var/log/grape/sandbox-audit.jsonl 可读

运行方式：
    cd /home/<user>/grape
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_codearts_bwrap_smoke.py -v --tb=short
    # 或直接跑：
    PYTHONPATH=. .venv/bin/python tests/test_codearts_bwrap_smoke.py
"""
import json
import os
import sys
import time
import urllib.request
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ===== 配置 =====
GRAPE_URL = os.environ.get("GRAPE_URL", "http://localhost:3000")
GRAPE_USER = os.environ.get("GRAPE_USER", "xiaowu2119")
GRAPE_PASS = os.environ.get("GRAPE_PASS", "xiaowu2119")
AUDIT_LOG = os.environ.get("SANDBOX_AUDIT_LOG", "/var/log/grape/sandbox-audit.jsonl")
CODEARTS_RID = int(os.environ.get("CODEARTS_RID", "0"))  # 0 = 自动解析
REPO_KEY = os.environ.get("SMOKE_REPO_KEY", "gitcode:signway/fluxgate")
SMOKE_MESSAGE = os.environ.get("SMOKE_MESSAGE", "回复一个字: ok")


def _login():
    """登录 grape 获取 JWT token。"""
    data = json.dumps({"username": GRAPE_USER, "password": GRAPE_PASS}).encode()
    req = urllib.request.Request(
        f"{GRAPE_URL}/api/auth/login",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())["token"]


def _resolve_codearts_rid(token):
    """自动解析 codearts 引擎 ID。"""
    if CODEARTS_RID:
        return CODEARTS_RID
    from src.session_manager import _resolve_chat_runtime_id
    return _resolve_chat_runtime_id("codearts")


def _send_chat(token, message, repo_key):
    """发 chat 消息，返回 (conversation_id, response_text)。"""
    # 创建 conversation
    req = urllib.request.Request(
        f"{GRAPE_URL}/api/chat/conversations",
        data=json.dumps({"repo_key": repo_key}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    conv_id = json.loads(resp.read())["conversation_id"]

    # 发消息（非 stream，等待完整回复）
    req = urllib.request.Request(
        f"{GRAPE_URL}/api/chat/send",
        data=json.dumps({
            "message": message,
            "conversation_id": conv_id,
            "repo_key": repo_key,
        }).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=180)
    result = json.loads(resp.read())
    return conv_id, result


def _read_audit_log(since_ts):
    """读取审计日志，返回 since_ts 之后的记录。"""
    records = []
    if not os.path.exists(AUDIT_LOG):
        return records
    with open(AUDIT_LOG) as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                if d.get("ts", "") >= since_ts:
                    records.append(d)
            except (json.JSONDecodeError, KeyError):
                continue
    return records


class CodeArtsBwrapSmokeTest(unittest.TestCase):
    """codearts + bwrap --unshare-net 端到端 smoke test。"""

    @classmethod
    def setUpClass(cls):
        cls.token = _login()
        cls.codearts_rid = _resolve_codearts_rid(cls.token)
        if not cls.codearts_rid:
            raise unittest.SkipTest("codearts 引擎未注册，跳过 smoke test")
        # 记录审计日志起始时间（UTC ISO 格式）
        cls.audit_since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    def test_01_task_returns_valid_result(self):
        """判据 1：任务返回正确结果。"""
        conv_id, result = _send_chat(self.token, SMOKE_MESSAGE, REPO_KEY)
        self.conv_id = conv_id

        # result 应该包含 assistant 回复
        text = ""
        if isinstance(result, dict):
            text = result.get("text", result.get("reply", result.get("content", "")))
        self.assertTrue(
            text and len(text) > 0,
            f"任务返回空结果: {result}"
        )
        # 记录结果供后续判据使用
        self.__class__.task_result_text = text
        self.__class__.task_conv_id = conv_id

    def test_02_audit_has_bwrap_allow(self):
        """判据 2：审计日志出现 codearts + bwrap allow（非 degrade/NullSandbox）。"""
        # 等待审计日志落盘
        time.sleep(2)

        records = _read_audit_log(self.audit_since)
        bwrap_allows = [
            r for r in records
            if r.get("kind") == "sandbox_access"
            and r.get("outcome") == "allow"
            and r.get("detail", {}).get("provider") == "bwrap"
            and r.get("detail", {}).get("exec_backend") == "codearts"
        ]
        degrades = [
            r for r in records
            if r.get("kind") == "policy_decision"
            and r.get("outcome") == "degrade"
            and "codearts" in str(r.get("detail", {}).get("backend", ""))
        ]

        self.assertTrue(
            len(bwrap_allows) > 0,
            f"判据 2 失败：审计日志没有 codearts + bwrap allow 记录。\n"
            f"这可能意味着 NullSandbox 降级（假阳性）。\n"
            f"degrade 记录数: {len(degrades)}\n"
            f"全部记录数: {len(records)}\n"
            f"检查 GRAPE_AGENT_SANDBOX 是否为 off。"
        )
        self.assertEqual(
            len(degrades), 0,
            f"发现 codearts degrade 记录（{len(degrades)} 条），说明有降级发生"
        )

    def test_03_network_proxy_has_requests(self):
        """判据 3：NetworkProxy 日志有 network_decision/allow 记录。"""
        records = _read_audit_log(self.audit_since)
        network_allows = [
            r for r in records
            if r.get("kind") == "network_decision"
            and r.get("outcome") == "allow"
        ]

        self.assertTrue(
            len(network_allows) > 0,
            f"判据 3 失败：审计日志没有 network_decision/allow 记录。\n"
            f"这意味着 codearts 的请求可能直连绕过了代理（--unshare-net 应该阻止直连）。\n"
            f"全部记录数: {len(records)}"
        )

        # 所有 network_decision 应该是 allow（白名单匹配），不应有 deny
        denies = [
            r for r in records
            if r.get("kind") == "network_decision"
            and r.get("outcome") == "deny"
        ]
        self.assertEqual(
            len(denies), 0,
            f"发现 network_decision/deny 记录（{len(denies)} 条），"
            f"说明白名单拦截了 codearts 的请求"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
