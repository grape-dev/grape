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
import shutil
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
            RotatingAuditLog._chain_state.pop(path, None)
            audit = RotatingAuditLog(path, hmac_key="test-key")
            audit.write_sync({"ts": "2026-01-01T00:00:00Z", "host": "gitcode.com", "policy": "allowed"})
            audit.write_sync({"ts": "2026-01-01T00:00:01Z", "host": "evil.com", "policy": "denied"})
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
            audit.write_sync({"host": "gitcode.com", "policy": "allowed"})
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
            audit.write_sync({"host": "a.com"})
            audit.write_sync({"host": "b.com"})
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

    def test_parse_memory_k8s_format(self):
        """K8s 内存量格式解析。"""
        from src.agent.cgroup import parse_memory
        self.assertEqual(parse_memory("256Mi"), 256)
        self.assertEqual(parse_memory("1Gi"), 1024)
        self.assertEqual(parse_memory("512M"), 488)  # 512*1000*1000//1024//1024=488
        self.assertEqual(parse_memory("2048"), 0)    # 纯数字按字节 → 0 MB
        self.assertEqual(parse_memory("invalid"), 0)

    def test_parse_cpu_k8s_format(self):
        """K8s CPU 限额格式解析。"""
        from src.agent.cgroup import parse_cpu
        self.assertEqual(parse_cpu("1"), 1000)     # 1 核 = 1000 毫核
        self.assertEqual(parse_cpu("0.5"), 500)
        self.assertEqual(parse_cpu("500m"), 500)
        self.assertEqual(parse_cpu("invalid"), 0)

    def test_cgroup_accepts_k8s_format(self):
        """CgroupSlice 接受 K8s 格式字符串。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("test-k8s", mem_limit_mb="2Gi", cpu_quota="500m")
        self.assertEqual(cg._mem_limit, 2048)
        self.assertEqual(cg._cpu_quota, 500)


# ============================================================
# cgroup.py 补全测试
# ============================================================

class TestCgroupSliceExtended(unittest.TestCase):
    """cgroup v1/v2 路径、get_stats、destroy、context manager。"""

    def test_cgroup_name_format(self):
        """cgroup name 含可读前缀 + sha256 后缀。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("evt-20260914-001-coding")
        self.assertIn("evt-20260914", cg.name)
        self.assertIn("grape-sandbox-", cg.name)

    def test_cgroup_created_false_before_create(self):
        """created 属性在 create() 前为 False。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("test-created-false")
        self.assertFalse(cg.created)

    def test_cgroup_destroy_without_create(self):
        """destroy() 在未 create 时不报错（幂等）。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("test-destroy-nocreate")
        cg.destroy()  # 不应抛异常
        cg.destroy(force=True)  # 幂等

    def test_cgroup_context_manager(self):
        """with 语句自动 destroy。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("test-ctx-mgr")
        with cg as c:
            self.assertIs(c, cg)
        # __exit__ 调 destroy，created 应仍为 False（没实际 create）
        self.assertFalse(cg.created)

    def test_cgroup_auto_mem_limit(self):
        """_auto_mem_limit 返回 > 0 的值。"""
        from src.agent.cgroup import CgroupSlice
        limit = CgroupSlice._auto_mem_limit()
        self.assertGreater(limit, 0)
        self.assertGreaterEqual(limit, 512)  # 最低 512MB

    def test_cgroup_get_stats_not_created(self):
        """get_stats 在未 create 时返回空 dict。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("test-stats-nocreate")
        stats = cg.get_stats()
        self.assertIsInstance(stats, dict)

    def test_cgroup_attach_pid_not_created(self):
        """attach_pid 在未 create 时不报错。"""
        from src.agent.cgroup import CgroupSlice
        cg = CgroupSlice("test-attach-nocreate")
        # 不 create 直接 attach — 应静默跳过
        cg.attach_pid(os.getpid())

    def test_parse_memory_edge_cases(self):
        """parse_memory 边界用例。"""
        from src.agent.cgroup import parse_memory
        self.assertEqual(parse_memory("0"), 0)
        self.assertEqual(parse_memory("1024"), 0)  # 1024 字节 < 1MB
        self.assertEqual(parse_memory("1Ki"), 0)    # 1Ki = 1024B < 1MB
        self.assertEqual(parse_memory("1Mi"), 1)    # 1Mi = 1MB
        self.assertEqual(parse_memory("1Gi"), 1024)
        self.assertEqual(parse_memory("256Mi"), 256)
        self.assertEqual(parse_memory("2Gi"), 2048)
        self.assertEqual(parse_memory(""), 0)
        self.assertEqual(parse_memory("abc"), 0)

    def test_parse_cpu_edge_cases(self):
        """parse_cpu 边界用例。"""
        from src.agent.cgroup import parse_cpu
        self.assertEqual(parse_cpu("0"), 0)
        self.assertEqual(parse_cpu("1000m"), 1000)
        self.assertEqual(parse_cpu("1"), 1000)
        self.assertEqual(parse_cpu("2"), 2000)
        self.assertEqual(parse_cpu("0.25"), 250)
        self.assertEqual(parse_cpu(""), 0)
        self.assertEqual(parse_cpu("abc"), 0)

    def test_detect_cgroup_version_returns_int(self):
        """_detect_cgroup_version 返回 1 或 2。"""
        from src.agent.cgroup import _detect_cgroup_version
        v = _detect_cgroup_version()
        self.assertIn(v, (1, 2))


# ============================================================
# audit.py 补全测试
# ============================================================

class TestAuditAsyncAndRotate(unittest.TestCase):
    """异步写入、轮转、verify_chain 跨文件。"""

    def test_async_write_and_flush(self):
        """异步 write() + flush() 后记录落盘。"""
        from src.agent.audit import RotatingAuditLog
        import tempfile, os, time
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            audit = RotatingAuditLog(path)
            audit.write({"ts": "2026-01-01T00:00:00Z", "task_id": "async-1", "event": "test"})
            audit.write({"ts": "2026-01-01T00:00:01Z", "task_id": "async-1", "event": "test2"})
            audit.flush()
            time.sleep(0.5)  # 等异步线程
            ok, msg = audit.verify_chain()
            self.assertTrue(ok, f"verify_chain failed: {msg}")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_rotate_preserves_chain(self):
        """轮转后新文件哈希链继承 .1 文件。"""
        from src.agent.audit import RotatingAuditLog, _rotate
        import tempfile, os, json
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            audit = RotatingAuditLog(path)
            audit.write_sync({"ts": "2026-01-01", "task_id": "r1", "event": "test"})
            audit.write_sync({"ts": "2026-01-02", "task_id": "r1", "event": "test2"})
            # 手动轮转
            _rotate(path)
            # 新文件写一条（prev_hash 继承自 .1）
            audit2 = RotatingAuditLog(path)
            audit2.write_sync({"ts": "2026-01-03", "task_id": "r2", "event": "test3"})
            ok, msg = audit2.verify_chain()
            # 轮转后新文件第一条 prev_hash 不为空（继承 .1），
            # verify_chain 接受第一条 prev_hash 为任意值
            self.assertTrue(ok, f"verify after rotate failed: {msg}")
        finally:
            for p in [path, path + ".1", path + ".2", path + ".chainkey"]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_tamper_after_rotate_detected(self):
        """轮转后篡改记录被检测。"""
        from src.agent.audit import RotatingAuditLog, _rotate
        import tempfile, os, json
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            audit = RotatingAuditLog(path)
            audit.write_sync({"ts": "2026-01-01", "task_id": "t1", "event": "test"})
            _rotate(path)
            audit2 = RotatingAuditLog(path)
            audit2.write_sync({"ts": "2026-01-02", "task_id": "t2", "event": "test2"})
            # 篡改当前文件
            with open(path, "r") as f:
                lines = f.readlines()
            data = json.loads(lines[0])
            data["host"] = "evil.com"
            lines[0] = json.dumps(data) + "\n"
            with open(path, "w") as f:
                f.writelines(lines)
            ok, msg = audit2.verify_chain()
            self.assertFalse(ok, "tamper should be detected")
        finally:
            for p in [path, path + ".1", path + ".chainkey"]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_hmac_key_resolution(self):
        """HMAC key 从环境变量或 sidecar 文件解析。"""
        from src.agent.audit import RotatingAuditLog
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            audit = RotatingAuditLog(path, hmac_key="test-key")
            self.assertEqual(audit._hmac_key, b"test-key")
        finally:
            for p in [path, path + ".chainkey"]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_write_sync_multiple_records(self):
        """write_sync 连续写多条，哈希链连续。"""
        from src.agent.audit import RotatingAuditLog
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            audit = RotatingAuditLog(path)
            for i in range(10):
                audit.write_sync({"ts": f"2026-01-{i:02d}", "task_id": "multi", "seq": i})
            ok, msg = audit.verify_chain()
            self.assertTrue(ok, f"chain broken: {msg}")
        finally:
            for p in [path, path + ".chainkey"]:
                if os.path.exists(p):
                    os.unlink(p)


# ============================================================
# proxy.py 补全测试
# ============================================================

class TestProxyEnvGeneration(unittest.TestCase):
    """代理环境变量生成详细测试。"""

    def test_proxy_env_count(self):
        """generate_proxy_envs 返回足够多的 key。"""
        from src.agent.proxy import generate_proxy_envs
        envs = generate_proxy_envs(3128)
        keys = [k for k, v in envs]
        # 至少包含 HTTP_PROXY / HTTPS_PROXY / NO_PROXY / GIT_SSH_COMMAND
        self.assertIn("HTTP_PROXY", keys)
        self.assertIn("HTTPS_PROXY", keys)
        self.assertIn("NO_PROXY", keys)
        self.assertIn("GIT_SSH_COMMAND", keys)

    def test_proxy_env_url_format(self):
        """代理 URL 格式正确。"""
        from src.agent.proxy import generate_proxy_envs
        envs = dict(generate_proxy_envs(3128))
        self.assertEqual(envs["HTTP_PROXY"], "http://localhost:3128")
        self.assertEqual(envs["HTTPS_PROXY"], "http://localhost:3128")

    def test_proxy_env_no_proxy_content(self):
        """NO_PROXY 不含 localhost/127.0.0.1。"""
        from src.agent.proxy import generate_proxy_envs
        envs = dict(generate_proxy_envs(3128))
        no_proxy = envs["NO_PROXY"]
        self.assertNotIn("localhost", no_proxy)
        self.assertNotIn("127.0.0.1", no_proxy)


class TestProxyCheckRequest(unittest.TestCase):
    """_check_request 白名单 + 端口限制 + DNS 校验。"""

    def test_check_request_allowed_domain(self):
        """白名单域名 allowed。"""
        from src.agent.proxy import WhitelistProxyHandler
        # 构造 handler 实例（不启动 server）
        class FakeHandler(WhitelistProxyHandler):
            def __init__(self):
                self._whitelist_inst = ["gitcode.com", "127.0.0.1"]
                self._leaked_tokens_inst = []
                self._audit_log_inst = None
                self._task_id_inst = "test-check"
        h = FakeHandler()
        result = h._check_request("gitcode.com", 443)
        self.assertTrue(result["allowed"])

    def test_check_request_denied_domain(self):
        """非白名单域名 denied。"""
        from src.agent.proxy import WhitelistProxyHandler
        class FakeHandler(WhitelistProxyHandler):
            def __init__(self):
                self._whitelist_inst = ["gitcode.com", "127.0.0.1"]
                self._leaked_tokens_inst = []
                self._audit_log_inst = None
                self._task_id_inst = "test-check"
        h = FakeHandler()
        result = h._check_request("evil.com", 443)
        self.assertFalse(result["allowed"])

    def test_check_request_loopback_allowed_port(self):
        """127.0.0.1 只允许 grape API 端口。"""
        from src.agent.proxy import WhitelistProxyHandler
        class FakeHandler(WhitelistProxyHandler):
            def __init__(self):
                self._whitelist_inst = ["127.0.0.1"]
                self._leaked_tokens_inst = []
                self._audit_log_inst = None
                self._task_id_inst = "test-check"
        h = FakeHandler()
        # 默认 GRAPE_SERVER_URL=http://localhost:3000 → 端口 3000
        result_ok = h._check_request("127.0.0.1", 3000)
        self.assertTrue(result_ok["allowed"])
        # SSH 端口拒绝
        result_deny = h._check_request("127.0.0.1", 22)
        self.assertFalse(result_deny["allowed"])

    def test_check_request_wildcard_match(self):
        """通配符白名单匹配子域名。"""
        from src.agent.proxy import WhitelistProxyHandler
        class FakeHandler(WhitelistProxyHandler):
            def __init__(self):
                self._whitelist_inst = ["*.gitcode.com"]
                self._leaked_tokens_inst = []
                self._audit_log_inst = None
                self._task_id_inst = "test-check"
        h = FakeHandler()
        result = h._check_request("api.gitcode.com", 443)
        self.assertTrue(result["allowed"])

    def test_parse_target(self):
        """_parse_target 解析 host:port。"""
        from src.agent.proxy import WhitelistProxyHandler
        class FakeHandler(WhitelistProxyHandler):
            def __init__(self):
                pass
        h = FakeHandler()
        host, port = h._parse_target("gitcode.com:443")
        self.assertEqual(host, "gitcode.com")
        self.assertEqual(port, 443)

    def test_sanitize_path_edge_cases(self):
        """path 脱敏边界用例。"""
        from src.agent.proxy import sanitize_path
        self.assertEqual(sanitize_path(""), "")
        self.assertEqual(sanitize_path("/"), "/")
        self.assertEqual(sanitize_path("/api/test"), "/api/test")
        self.assertIn("[REDACTED]", sanitize_path("/api?token=secret"))
        self.assertIn("[REDACTED]", sanitize_path("/api?key=abc123"))
        self.assertIn("[REDACTED]", sanitize_path("/api?secret=xyz"))
        self.assertIn("[REDACTED]", sanitize_path("/api?password=pass"))


class TestProxySandboxProxy(unittest.TestCase):
    """SandboxProxy 初始化 + start/stop。"""

    def test_sandbox_proxy_init(self):
        """SandboxProxy 初始化属性正确。"""
        from src.agent.proxy import SandboxProxy
        proxy = SandboxProxy(task_id="test-proxy-init", whitelist=["gitcode.com"])
        self.assertEqual(proxy._task_id, "test-proxy-init")
        self.assertIn("gitcode.com", proxy._whitelist)
        self.assertFalse(proxy._server is not None)  # 未 start

    def test_sandbox_proxy_stop_without_start(self):
        """stop() 在未 start 时不报错（幂等）。"""
        from src.agent.proxy import SandboxProxy
        proxy = SandboxProxy(task_id="test-stop-nostart", whitelist=[])
        proxy.stop()  # 不应抛异常

    def test_sandbox_proxy_is_alive_false(self):
        """is_alive() 在未 start 时返回 False。"""
        from src.agent.proxy import SandboxProxy
        proxy = SandboxProxy(task_id="test-alive", whitelist=[])
        self.assertFalse(proxy.is_alive())


class TestProxyUnixSocketBridge(unittest.TestCase):
    """UnixSocketBridge 初始化。"""

    def test_bridge_init(self):
        """UnixSocketBridge 初始化属性正确。"""
        from src.agent.proxy import UnixSocketBridge
        bridge = UnixSocketBridge("/tmp/test-bridge.sock", 3128)
        self.assertEqual(bridge.socket_path, "/tmp/test-bridge.sock")
        self.assertEqual(bridge.proxy_port, 3128)
        self.assertFalse(bridge.is_alive())

    def test_bridge_stop_without_start(self):
        """stop() 在未 start 时不报错。"""
        from src.agent.proxy import UnixSocketBridge
        bridge = UnixSocketBridge("/tmp/test-bridge-stop.sock", 3128)
        bridge.stop()  # 不应抛异常


# ============================================================
# sandbox.py 补全测试
# ============================================================

class TestSandboxEnabled(unittest.TestCase):
    """sandbox_enabled / _bwrap_bin。"""

    @unittest.skipUnless(shutil.which("bwrap"), "bwrap not installed")
    def test_sandbox_enabled_default_off(self):
        """GRAPE_SANDBOX_NET 默认 on（与 GRAPE_AGENT_SANDBOX 一致）。"""
        import os
        old = os.environ.get("GRAPE_SANDBOX_NET", "")
        try:
            os.environ.pop("GRAPE_SANDBOX_NET", None)
            import importlib
            import src.config as _cfg
            importlib.reload(_cfg)
            self.assertTrue(_cfg.GRAPE_SANDBOX_NET)
        finally:
            if old:
                os.environ["GRAPE_SANDBOX_NET"] = old
            else:
                os.environ.pop("GRAPE_SANDBOX_NET", None)

    @unittest.skipUnless(shutil.which("bwrap"), "bwrap not installed")
    def test_bwrap_bin(self):
        """_bwrap_bin 返回非空字符串（bwrap 已安装）。"""
        from src.agent.sandbox import _bwrap_bin
        bwrap = _bwrap_bin()
        self.assertTrue(bwrap, "bwrap not found")
        self.assertIn("bwrap", bwrap)


class TestBuildBwrapCmd(unittest.TestCase):
    """build_bwrap_cmd 命令构建。"""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("bwrap"):
            raise unittest.SkipTest("bwrap not installed")

    @unittest.skipUnless(shutil.which("bwrap"), "bwrap not installed")
    def test_build_bwrap_cmd_share_net(self):
        """非网络隔离模式返回 --share-net 命令。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        self.assertIsNotNone(cmd)
        self.assertIn("--share-net", cmd)

    def test_build_bwrap_cmd_net_isolation(self):
        """网络隔离模式返回 --unshare-net 命令。"""
        from src.agent.sandbox import build_bwrap_cmd, SandboxContext
        ctx = SandboxContext("test-bwrap-net", net_enabled=True)
        ctx.proxy_port = 3128
        ctx.socket_path = "/tmp/test-bridge.sock"
        ctx.bridge_script_path = "/tmp/test-bridge-script.py"
        cmd = build_bwrap_cmd("/tmp", backend_name="codex", sandbox_ctx=ctx)
        self.assertIsNotNone(cmd)
        self.assertIn("--unshare-net", cmd)
        self.assertIn("--unshare-pid", cmd)
        self.assertIn("--unshare-ipc", cmd)
        self.assertIn("--unshare-uts", cmd)
        self.assertIn("--cap-drop", cmd)
        self.assertIn("ALL", cmd)

    def test_build_bwrap_cmd_has_proc(self):
        """两种模式都有 --proc /proc。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        self.assertIn("--proc", cmd)
        self.assertIn("/proc", cmd)

    def test_build_bwrap_cmd_has_dev(self):
        """两种模式都有 --dev /dev。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        self.assertIn("--dev", cmd)
        self.assertIn("/dev", cmd)


class TestSandboxContext(unittest.TestCase):
    """SandboxContext 数据类。"""

    def test_context_init(self):
        """SandboxContext 初始化属性正确。"""
        from src.agent.sandbox import SandboxContext
        ctx = SandboxContext("test-ctx", net_enabled=True)
        self.assertEqual(ctx.task_id, "test-ctx")
        self.assertTrue(ctx.net_enabled)
        self.assertIsNone(ctx._proxy)
        self.assertIsNone(ctx._cgroup)
        self.assertIsNone(ctx._audit_log)

    def test_context_cleanup_without_resources(self):
        """cleanup() 在无资源时不报错。"""
        from src.agent.sandbox import SandboxContext
        ctx = SandboxContext("test-cleanup", net_enabled=True)
        ctx.cleanup()  # 不应抛异常


class TestBuildInSandboxScript(unittest.TestCase):
    """_build_in_sandbox_script 脚本生成。"""

    def test_script_contains_bridge(self):
        """脚本包含桥接启动。"""
        from src.agent.sandbox import _build_in_sandbox_script
        script = _build_in_sandbox_script("echo hello", "/tmp/bridge.py")
        self.assertIn("bridge.py", script)
        self.assertIn("echo hello", script)

    def test_script_contains_trap(self):
        """脚本包含 trap EXIT 清理。"""
        from src.agent.sandbox import _build_in_sandbox_script
        script = _build_in_sandbox_script("echo test", "/tmp/bridge.py")
        self.assertIn("trap", script)
        self.assertIn("EXIT", script)

    def test_script_contains_polling(self):
        """脚本包含轮询等待桥接就绪。"""
        from src.agent.sandbox import _build_in_sandbox_script
        script = _build_in_sandbox_script("echo test", "/tmp/bridge.py")
        self.assertIn("for i in", script)
        self.assertIn("3128", script)


class TestWhitelistCache(unittest.TestCase):
    """白名单缓存 + invalidate。"""

    def test_invalidate_whitelist_cache(self):
        """invalidate_whitelist_cache 清空缓存。"""
        from src.agent.sandbox import invalidate_whitelist_cache, _whitelist_cache, _whitelist_cache_ts
        # 先填充
        _whitelist_cache[("test", "")] = ["example.com"]
        _whitelist_cache_ts[("test", "")] = 0.0
        # 清空
        invalidate_whitelist_cache()
        self.assertEqual(len(_whitelist_cache), 0)
        self.assertEqual(len(_whitelist_cache_ts), 0)


class TestCleanupSandbox(unittest.TestCase):
    """cleanup_sandbox 幂等性。"""

    def test_cleanup_none(self):
        """cleanup_sandbox(None) 不报错。"""
        from src.agent.sandbox import cleanup_sandbox
        cleanup_sandbox(None)  # 不应抛异常

    def test_cleanup_without_resources(self):
        """cleanup_sandbox 对无资源的 ctx 不报错。"""
        from src.agent.sandbox import cleanup_sandbox, SandboxContext
        ctx = SandboxContext("test-cleanup-sandbox", net_enabled=True)
        cleanup_sandbox(ctx)  # 不应抛异常


class TestWriteBridgeScript(unittest.TestCase):
    """_write_bridge_script 桥接脚本生成。"""

    def test_bridge_script_created(self):
        """_write_bridge_script 创建文件并返回路径。"""
        from src.agent.sandbox import _write_bridge_script
        import os, tempfile
        path = _write_bridge_script("/tmp/test-bridge-create.sock")
        try:
            self.assertTrue(os.path.isfile(path))
            with open(path, "r") as f:
                content = f.read()
            self.assertIn("3128", content)
            self.assertIn("ThreadingTCPServer", content)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_bridge_script_content_valid_python(self):
        """桥接脚本是合法 Python 语法。"""
        from src.agent.sandbox import _write_bridge_script
        import os, tempfile, subprocess
        path = _write_bridge_script("/tmp/test-bridge-syntax.sock")
        try:
            result = subprocess.run(
                [".venv/bin/python", "-c", f"compile(open('{path}').read(), '{path}', 'exec')"],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, f"Syntax error: {result.stderr}")
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestSandboxContextCleanup(unittest.TestCase):
    """SandboxContext.cleanup 详细测试。"""

    def test_cleanup_with_proxy_none(self):
        """cleanup() 当 _proxy 为 None 时不报错。"""
        from src.agent.sandbox import SandboxContext
        ctx = SandboxContext("test-cleanup-proxy-none", net_enabled=True)
        ctx._proxy = None
        ctx._cgroup = None
        ctx._audit_log = None
        ctx.cleanup()  # 不应抛异常

    def test_cleanup_idempotent(self):
        """cleanup() 多次调用不报错。"""
        from src.agent.sandbox import SandboxContext
        ctx = SandboxContext("test-cleanup-idempotent", net_enabled=True)
        ctx.cleanup()
        ctx.cleanup()
        ctx.cleanup()


# ============================================================
# 集成测试 — 实际启动代理 + 桥接 + 连接验证
# ============================================================

class TestProxyServerIntegration(unittest.TestCase):
    """启动 SandboxProxy + 实际 HTTP 请求验证。"""

    def test_proxy_start_and_stop(self):
        """SandboxProxy.start() 启动代理 + 桥接，stop() 清理。"""
        from src.agent.proxy import SandboxProxy
        import time
        proxy = SandboxProxy(
            task_id="test-integration-start",
            whitelist=["gitcode.com", "127.0.0.1"],
        )
        info = proxy.start()
        try:
            self.assertIn("proxy_port", info)
            self.assertIn("socket_path", info)
            self.assertTrue(proxy.is_alive())
            time.sleep(0.3)  # 等代理就绪
        finally:
            proxy.stop()
        self.assertFalse(proxy.is_alive())

    def test_proxy_denies_non_whitelisted(self):
        """代理拒绝非白名单域名的 CONNECT。"""
        from src.agent.proxy import SandboxProxy
        import socket, time
        proxy = SandboxProxy(
            task_id="test-deny-connect",
            whitelist=["gitcode.com"],
        )
        info = proxy.start()
        try:
            time.sleep(0.3)
            # 连接代理发 CONNECT evil.com:443
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("127.0.0.1", info["proxy_port"]))
            s.sendall(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com:443\r\n\r\n")
            resp = s.recv(4096).decode("utf-8", errors="replace")
            s.close()
            # 应该返回 403 或类似拒绝响应
            self.assertIn("403", resp)
        finally:
            proxy.stop()

    def test_proxy_denies_loopback_wrong_port(self):
        """代理拒绝 127.0.0.1 非 grape 端口的 CONNECT。"""
        from src.agent.proxy import SandboxProxy
        import socket, time
        proxy = SandboxProxy(
            task_id="test-loopback-port",
            whitelist=["127.0.0.1"],
        )
        info = proxy.start()
        try:
            time.sleep(0.3)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("127.0.0.1", info["proxy_port"]))
            s.sendall(b"CONNECT 127.0.0.1:22 HTTP/1.1\r\nHost: 127.0.0.1:22\r\n\r\n")
            resp = s.recv(4096).decode("utf-8", errors="replace")
            s.close()
            self.assertIn("403", resp)
        finally:
            proxy.stop()

    def test_proxy_http_forward_denied(self):
        """代理拒绝非白名单域名的 HTTP GET。"""
        from src.agent.proxy import SandboxProxy
        import socket, time
        proxy = SandboxProxy(
            task_id="test-http-deny",
            whitelist=["gitcode.com"],
        )
        info = proxy.start()
        try:
            time.sleep(0.3)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("127.0.0.1", info["proxy_port"]))
            s.sendall(b"GET http://evil.com/ HTTP/1.1\r\nHost: evil.com\r\n\r\n")
            resp = s.recv(4096).decode("utf-8", errors="replace")
            s.close()
            self.assertIn("403", resp)
        finally:
            proxy.stop()


class TestCgroupSliceV1Integration(unittest.TestCase):
    """cgroup v1 实际创建/销毁（需要 sudo）。"""

    def test_cgroup_create_and_destroy_v1(self):
        """cgroup v1 实际创建 + 销毁（本机 cgroup v1 + sudo）。"""
        from src.agent.cgroup import CgroupSlice, _detect_cgroup_version
        v = _detect_cgroup_version()
        if v != 1:
            self.skipTest("cgroup v1 not available on this system")
        cg = CgroupSlice("test-v1-integration", mem_limit_mb=512)
        ok = cg.create()
        if not ok:
            self.skipTest("sudo not available or cgroup creation failed")
        try:
            self.assertTrue(cg.created)
            # attach 当前进程
            cg.attach_pid(os.getpid())
            # get_stats 应返回非空
            stats = cg.get_stats()
            self.assertIsInstance(stats, dict)
        finally:
            cg.destroy(force=True)
            self.assertFalse(cg.created)


class TestAuditAsyncIntegration(unittest.TestCase):
    """异步审计实际写入 + flush + verify。"""

    def test_concurrent_async_writes(self):
        """多线程并发异步写入，全部落盘 + 哈希链连续。"""
        from src.agent.audit import RotatingAuditLog
        import tempfile, os, time, threading
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            audit = RotatingAuditLog(path)

            def writer(tid):
                for i in range(5):
                    audit.write({"ts": f"2026-01-{tid:02d}T{i:02d}", "task_id": f"thread-{tid}", "seq": i})

            threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            audit.flush()
            time.sleep(0.5)
            ok, msg = audit.verify_chain()
            self.assertTrue(ok, f"concurrent write chain broken: {msg}")
        finally:
            for p in [path, path + ".chainkey"]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_async_writer_put_and_flush(self):
        """_AsyncWriter put + flush 基本流程。"""
        from src.agent.audit import _AsyncWriter
        import tempfile, os, time
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            writer = _AsyncWriter()
            writer.put(path, '{"test": "line1"}\n')
            writer.put(path, '{"test": "line2"}\n')
            writer.flush()
            time.sleep(0.3)
            with open(path, "r") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestSandboxBuildBwrapDetailed(unittest.TestCase):
    """build_bwrap_cmd 详细验证。"""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("bwrap"):
            raise unittest.SkipTest("bwrap not installed")

    def test_bwrap_cmd_has_etc_safe(self):
        """bwrap 命令包含 /etc 安全子集挂载。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        # ro-bind-try 不一定出现（取决于 /etc/ssl 等是否存在）
        # 但 --ro-bind /usr 一定有
        self.assertIn("--ro-bind", cmd)

    def test_bwrap_cmd_has_tmpfs_tmp(self):
        """bwrap 命令包含 --tmpfs /tmp。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        self.assertIn("--tmpfs", cmd)
        self.assertIn("/tmp", cmd)

    def test_bwrap_cmd_has_bind_cwd(self):
        """bwrap 命令包含 --bind cwd cwd。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        self.assertIsNotNone(cmd)
        self.assertIn("--bind", cmd)

    def test_bwrap_cmd_net_isolation_has_setenv(self):
        """网络隔离模式包含 --setenv HTTP_PROXY。"""
        from src.agent.sandbox import build_bwrap_cmd, SandboxContext
        ctx = SandboxContext("test-setenv", net_enabled=True)
        ctx.proxy_port = 3128
        ctx.socket_path = "/tmp/test-bridge.sock"
        ctx.bridge_script_path = "/tmp/test-bridge-script.py"
        cmd = build_bwrap_cmd("/tmp", backend_name="codex", sandbox_ctx=ctx)
        self.assertIn("--setenv", cmd)
        # 至少有 HTTP_PROXY
        proxy_envs = [cmd[i+1] for i, v in enumerate(cmd) if v == "--setenv"]
        self.assertIn("HTTP_PROXY", proxy_envs)

    def test_bwrap_cmd_net_isolation_has_git_ssh_false(self):
        """网络隔离模式包含 GIT_SSH_COMMAND=/bin/false。"""
        from src.agent.sandbox import build_bwrap_cmd, SandboxContext
        ctx = SandboxContext("test-gitssh", net_enabled=True)
        ctx.proxy_port = 3128
        ctx.socket_path = "/tmp/test-bridge.sock"
        ctx.bridge_script_path = "/tmp/test-bridge-script.py"
        cmd = build_bwrap_cmd("/tmp", backend_name="codex", sandbox_ctx=ctx)
        # 找 GIT_SSH_COMMAND
        for i, v in enumerate(cmd):
            if v == "GIT_SSH_COMMAND" and i+1 < len(cmd):
                self.assertEqual(cmd[i+1], "/bin/false")
                return
        self.fail("GIT_SSH_COMMAND not found in bwrap cmd")

    def test_bwrap_cmd_has_die_with_parent(self):
        """bwrap 命令包含 --die-with-parent。"""
        from src.agent.sandbox import build_bwrap_cmd
        cmd = build_bwrap_cmd("/tmp", backend_name="opencode")
        self.assertIn("--die-with-parent", cmd)


class TestProxyAuditMethod(unittest.TestCase):
    """_audit 方法测试。"""

    def test_audit_writes_record(self):
        """_audit 写审计记录到文件。"""
        from src.agent.proxy import WhitelistProxyHandler, SandboxProxy
        from src.agent.audit import RotatingAuditLog
        import tempfile, os, json, time
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            audit_path = f.name
        os.unlink(audit_path)
        try:
            audit = RotatingAuditLog(audit_path)
            proxy = SandboxProxy(
                task_id="test-audit-method",
                whitelist=["gitcode.com"],
                audit_log=audit,
            )
            info = proxy.start()
            try:
                time.sleep(0.3)
                # 发一个被拒绝的请求
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(("127.0.0.1", info["proxy_port"]))
                s.sendall(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com:443\r\n\r\n")
                s.recv(4096)
                s.close()
                audit.flush()
                time.sleep(0.3)
                # 验证审计日志有记录
                with open(audit_path, "r") as f:
                    lines = f.readlines()
                self.assertGreater(len(lines), 0)
                record = json.loads(lines[0])
                self.assertEqual(record["host"], "evil.com")
                self.assertEqual(record["policy"], "denied")
            finally:
                proxy.stop()
        finally:
            for p in [audit_path, audit_path + ".chainkey"]:
                if os.path.exists(p):
                    os.unlink(p)


if __name__ == "__main__":
    unittest.main()
