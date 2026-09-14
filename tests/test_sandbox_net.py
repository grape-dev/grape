"""测试沙箱网络隔离（Issue #139）。

测试覆盖：
- 域名白名单匹配（通配符）
- DNS 地址校验
- hop-by-hop header 剥离
- 请求走私检查
- path 脱敏
- 代理 env 生成
- 动态白名单
- 审计哈希链
- 审计轮转
- 环境预检
- cgroup 降级
"""
import json
import os
import tempfile
import unittest

# 确保能 import grape 模块
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDomainMatch(unittest.TestCase):
    """域名白名单匹配。"""

    def test_exact_match(self):
        from src.agent.proxy import match_domain
        self.assertTrue(match_domain("gitcode.com", "gitcode.com"))
        self.assertTrue(match_domain("api.gitcode.com", "api.gitcode.com"))

    def test_no_match(self):
        from src.agent.proxy import match_domain
        self.assertFalse(match_domain("evil.com", "gitcode.com"))

    def test_wildcard_match(self):
        from src.agent.proxy import match_domain
        self.assertTrue(match_domain("api.gitcode.com", "*.gitcode.com"))
        self.assertTrue(match_domain("raw.gitcode.com", "*.gitcode.com"))

    def test_wildcard_no_match(self):
        from src.agent.proxy import match_domain
        self.assertFalse(match_domain("gitcode.com", "*.gitcode.com"))
        self.assertFalse(match_domain("evil.com", "*.gitcode.com"))

    def test_wildcard_too_broad_rejected(self):
        from src.agent.proxy import match_domain
        self.assertFalse(match_domain("foo.com", "*.com"))

    def test_port_suffix(self):
        from src.agent.proxy import match_domain
        self.assertTrue(match_domain("gitcode.com:443", "gitcode.com"))
        self.assertTrue(match_domain("api.gitcode.com:443", "*.gitcode.com"))


class TestDnsAddressGuard(unittest.TestCase):
    """DNS 地址校验。"""

    def test_loopback_denied(self):
        from src.agent.proxy import check_resolved_address
        ok, _ = check_resolved_address("127.0.0.1", 80)
        self.assertFalse(ok)
        ok, _ = check_resolved_address("127.0.0.1", 443)
        self.assertFalse(ok)

    def test_cloud_metadata_denied(self):
        from src.agent.proxy import check_resolved_address
        ok, _ = check_resolved_address("169.254.169.254", 80)
        self.assertFalse(ok)

    def test_cloud_metadata_azure_denied(self):
        from src.agent.proxy import check_resolved_address
        ok, _ = check_resolved_address("168.63.129.16", 80)
        self.assertFalse(ok)

    def test_valid_ip_allowed(self):
        from src.agent.proxy import check_resolved_address
        # 8.8.8.8 是 Google DNS — 不在拒绝列表
        ok, _ = check_resolved_address("8.8.8.8", 443)
        self.assertTrue(ok)


class TestHopByHopStripping(unittest.TestCase):
    """hop-by-hop header 剥离。"""

    def test_strip_connection(self):
        from src.agent.proxy import strip_hop_by_hop_headers
        result = strip_hop_by_hop_headers({"Connection": "keep-alive", "Host": "example.com"})
        self.assertNotIn("Connection", result)
        self.assertIn("Host", result)

    def test_strip_proxy_auth(self):
        from src.agent.proxy import strip_hop_by_hop_headers
        result = strip_hop_by_hop_headers({
            "Proxy-Authorization": "Bearer xxx",
            "Content-Type": "application/json",
        })
        self.assertNotIn("Proxy-Authorization", result)
        self.assertIn("Content-Type", result)

    def test_keep_normal_headers(self):
        from src.agent.proxy import strip_hop_by_hop_headers
        headers = {"Content-Type": "application/json", "Accept": "*/*", "Host": "example.com"}
        result = strip_hop_by_hop_headers(headers)
        self.assertEqual(result, headers)


class TestRequestSmuggling(unittest.TestCase):
    """请求走私检查。"""

    def test_cl_te_coexist_detected(self):
        from src.agent.proxy import check_request_smuggling
        ok, _ = check_request_smuggling({"Content-Length": "10", "Transfer-Encoding": "chunked"})
        self.assertFalse(ok)

    def test_cl_only_ok(self):
        from src.agent.proxy import check_request_smuggling
        ok, _ = check_request_smuggling({"Content-Length": "10"})
        self.assertTrue(ok)

    def test_te_only_ok(self):
        from src.agent.proxy import check_request_smuggling
        ok, _ = check_request_smuggling({"Transfer-Encoding": "chunked"})
        self.assertTrue(ok)

    def test_no_cl_no_te_ok(self):
        from src.agent.proxy import check_request_smuggling
        ok, _ = check_request_smuggling({"Host": "example.com"})
        self.assertTrue(ok)


class TestPathSanitization(unittest.TestCase):
    """path 脱敏。"""

    def test_token_redacted(self):
        from src.agent.proxy import sanitize_path
        result = sanitize_path("/api?token=secret123&foo=bar")
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("secret123", result)
        self.assertIn("foo=bar", result)

    def test_no_token_unchanged(self):
        from src.agent.proxy import sanitize_path
        path = "/api/v5/repos/grape-dev/grape/issues"
        self.assertEqual(sanitize_path(path), path)

    def test_multiple_sensitive_params(self):
        from src.agent.proxy import sanitize_path
        result = sanitize_path("/api?token=abc&key=def&normal=ghi")
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("abc", result)
        self.assertNotIn("def", result)
        self.assertIn("normal=ghi", result)


class TestProxyEnvs(unittest.TestCase):
    """代理 env 生成。"""

    def test_all_env_keys_present(self):
        from src.agent.proxy import generate_proxy_envs
        envs = dict(generate_proxy_envs(3128))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                     "ALL_PROXY", "all_proxy", "GRPC_PROXY", "grpc_proxy",
                     "NO_PROXY", "no_proxy"):
            self.assertIn(key, envs, f"missing env: {key}")

    def test_proxy_url(self):
        from src.agent.proxy import generate_proxy_envs
        envs = dict(generate_proxy_envs(3128))
        self.assertEqual(envs["HTTP_PROXY"], "http://localhost:3128")

    def test_no_proxy_excludes_localhost(self):
        """NO_PROXY 只含 ::1，不含 localhost/127.0.0.1。"""
        from src.agent.proxy import generate_proxy_envs
        envs = dict(generate_proxy_envs(3128))
        self.assertNotIn("localhost", envs["NO_PROXY"])
        self.assertNotIn("127.0.0.1", envs["NO_PROXY"])
        self.assertIn("::1", envs["NO_PROXY"])

    def test_git_ssh_disabled(self):
        from src.agent.proxy import generate_proxy_envs
        envs = dict(generate_proxy_envs(3128))
        self.assertEqual(envs["GIT_SSH_COMMAND"], "/bin/false")


class TestDynamicWhitelist(unittest.TestCase):
    """动态白名单。"""

    def test_localhost_always_added(self):
        from src.agent.proxy import get_dynamic_whitelist
        wl = get_dynamic_whitelist(["gitcode.com"])
        self.assertIn("127.0.0.1", wl)
        self.assertIn("gitcode.com", wl)

    def test_grape_server_url_added(self):
        from src.agent.proxy import get_dynamic_whitelist
        old = os.environ.get("GRAPE_SERVER_URL", "")
        try:
            os.environ["GRAPE_SERVER_URL"] = "http://192.168.1.100:3000"
            wl = get_dynamic_whitelist(["gitcode.com"])
            self.assertIn("192.168.1.100", wl)
        finally:
            if old:
                os.environ["GRAPE_SERVER_URL"] = old
            else:
                os.environ.pop("GRAPE_SERVER_URL", None)


class TestAuditHashChain(unittest.TestCase):
    """审计哈希链。"""

    def test_write_and_verify(self):
        from src.agent.audit import RotatingAuditLog
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            # 清理可能残留的 chain_state
            RotatingAuditLog._chain_state.pop(path, None)
            audit = RotatingAuditLog(path, hmac_key="test-key")
            audit.write({"ts": "2026-01-01T00:00:00Z", "host": "gitcode.com", "policy": "allowed"})
            audit.write({"ts": "2026-01-01T00:00:01Z", "host": "evil.com", "policy": "denied"})
            ok, msg = audit.verify_chain()
            self.assertTrue(ok, f"chain should be intact: {msg}")
        finally:
            os.unlink(path)
            sidecar = path + ".chainkey"
            if os.path.exists(sidecar):
                os.unlink(sidecar)

    def test_tamper_detected(self):
        from src.agent.audit import RotatingAuditLog
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            audit = RotatingAuditLog(path, hmac_key="test-key")
            audit.write({"host": "gitcode.com", "policy": "allowed"})
            # 篡改记录
            with open(path) as f:
                lines = f.readlines()
            record = json.loads(lines[0])
            record["host"] = "evil.com"  # 篡改
            with open(path, "w") as f:
                f.write(json.dumps(record) + "\n")
            ok, msg = audit.verify_chain()
            self.assertFalse(ok, "tamper should be detected")
            self.assertIn("mismatch", msg)
        finally:
            os.unlink(path)
            sidecar = path + ".chainkey"
            if os.path.exists(sidecar):
                os.unlink(sidecar)

    def test_prev_hash_chaining(self):
        from src.agent.audit import RotatingAuditLog
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            audit = RotatingAuditLog(path, hmac_key="test-key")
            audit.write({"host": "a.com"})
            audit.write({"host": "b.com"})
            with open(path) as f:
                lines = f.readlines()
            r1 = json.loads(lines[0])
            r2 = json.loads(lines[1])
            self.assertEqual(r1["prev_hash"], "")
            self.assertEqual(r2["prev_hash"], r1["record_hash"])
        finally:
            os.unlink(path)
            sidecar = path + ".chainkey"
            if os.path.exists(sidecar):
                os.unlink(sidecar)


class TestDetectEnv(unittest.TestCase):
    """环境预检。"""

    def test_detect_returns_results(self):
        from src.agent.detect import detect_sandbox_env
        can_proceed, results, warnings = detect_sandbox_env()
        self.assertIsInstance(can_proceed, bool)
        self.assertGreater(len(results), 0)
        self.assertIsInstance(warnings, list)

    def test_hard_failures_identified(self):
        from src.agent.detect import HARD_FAILURES
        self.assertIn("bwrap", HARD_FAILURES)
        self.assertIn("platform", HARD_FAILURES)
        self.assertIn("tmp", HARD_FAILURES)


class TestCgroupSlice(unittest.TestCase):
    """cgroup v2 优先 + v1 降级。"""

    def test_no_sudo_degrades(self):
        """sudo 不可用时降级。"""
        from src.agent.cgroup import CgroupSlice
        import unittest.mock as mock
        with mock.patch("shutil.which", return_value=None):
            cg = CgroupSlice("test-task", mem_limit_mb=512)
            ok = cg.create()
            self.assertFalse(ok)

    def test_detect_cgroup_version(self):
        """版本检测返回 0/1/2。"""
        from src.agent.cgroup import _detect_cgroup_version
        v = _detect_cgroup_version()
        self.assertIn(v, (0, 1, 2))

    def test_v1_fallback_when_v2_unavailable(self):
        """本机 v1 — 确认 cgroup_version=1 走 v1 降级路径。"""
        from src.agent.cgroup import CgroupSlice, _detect_cgroup_version
        v = _detect_cgroup_version()
        cg = CgroupSlice("test-v1", mem_limit_mb=0)
        self.assertEqual(cg._cgroup_version, v)
        if v == 1:
            self.assertEqual(cg._cgroup_version, 1)


if __name__ == "__main__":
    unittest.main()
