"""测试 P1-8 敏感数据令牌化。

测试覆盖：
- 精确值 redact（secret_values 精确替换）
- 正则模式令牌化（10 类敏感数据格式）
- 顺序：精确值先跑，正则后跑
- 占位符统一 [REDACTED]
- 幂等性（[REDACTED] 再跑不变）
- fail-open（运行时异常保留原文）
- 输入边界（None/bytes/非 str/超长截断）
- matches 不含 preview（不泄漏明文）
- clear() 清除 secret_values
- NullRedactEngine
- _redact 向后兼容
- 模块加载 smoke test
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestExactValueRedact(unittest.TestCase):
    """精确值 redact。"""

    def test_exact_value_replaced(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["secret_token_12345"])
        result = engine.redact("use secret_token_12345 to auth")
        self.assertIn("[REDACTED]", result.text)
        self.assertNotIn("secret_token_12345", result.text)
        self.assertGreater(result.redacted_count, 0)

    def test_multiple_occurrences(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["my_token"])
        result = engine.redact("my_token and my_token again")
        self.assertEqual(result.text.count("[REDACTED]"), 2)

    def test_no_match_no_change(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["nonexistent_token"])
        result = engine.redact("hello world")
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.redacted_count, 0)

    def test_short_secret_ignored(self):
        """长度 < 4 的 secret 不做精确替换。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["ab"])
        result = engine.redact("ab cd ab")
        self.assertEqual(result.text, "ab cd ab")

    def test_multiple_secrets(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["token_aaaa", "key_bbbb"])
        result = engine.redact("token_aaaa and key_bbbb")
        self.assertNotIn("token_aaaa", result.text)
        self.assertNotIn("key_bbbb", result.text)
        self.assertIn("[REDACTED]", result.text)


class TestClearMethod(unittest.TestCase):
    """clear() 清除 secret_values。"""

    def test_clear_removes_secrets(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["secret_token_12345"])
        # clear 前能替换
        result1 = engine.redact("use secret_token_12345")
        self.assertIn("[REDACTED]", result1.text)
        # clear 后不能替换
        engine.clear()
        result2 = engine.redact("use secret_token_12345")
        self.assertNotIn("[REDACTED]", result2.text)
        self.assertIn("secret_token_12345", result2.text)

    def test_clear_still_does_regex(self):
        """clear 后正则模式仍生效。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["my_secret"])
        engine.clear()
        result = engine.redact("sk-abcdefghijklmnopqrstuvwxyz")
        self.assertIn("[REDACTED]", result.text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", result.text)

    def test_clear_idempotent(self):
        """多次 clear 不报错。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["my_secret"])
        engine.clear()
        engine.clear()
        engine.clear()


class TestRegexPatterns(unittest.TestCase):
    """10 类正则模式。"""

    def test_openai_key(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("api key is sk-abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("openai_key", types)

    def test_aws_ak(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("AWS key: AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("aws_ak", types)

    def test_huawei_ak(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("HUAWEI AK: HPUA1RGZONSTYDPQLPZR")
        self.assertNotIn("HPUA1RGZONSTYDPQLPZR", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("huawei_ak", types)

    def test_private_key(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...")
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("private_key", types)

    def test_jwt(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryA68vOJZns6X5g")
        self.assertIn("[REDACTED]", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("jwt", types)

    def test_db_connection(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("DB: postgres://user:passw0rd@192.168.0.59:5432/db")
        self.assertNotIn("passw0rd", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("db_connection", types)

    def test_password_pattern(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact('password=supersecretpass123')
        self.assertNotIn("supersecretpass123", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("password", types)

    def test_ssh_path(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("key at ~/.ssh/id_rsa")
        self.assertIn("[REDACTED]", result.text)
        types = [m["type"] for m in result.matches]
        self.assertIn("ssh_path", types)

    def test_no_false_positive_normal_text(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("The quick brown fox jumps over the lazy dog")
        self.assertEqual(result.text, "The quick brown fox jumps over the lazy dog")
        self.assertEqual(result.redacted_count, 0)

    def test_no_false_positive_long_varname(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("my_variable_name_is_very_long_and_ordinary")
        self.assertNotIn("[REDACTED]", result.text)


class TestOrderAndPlaceholder(unittest.TestCase):
    """顺序 + 占位符统一。"""

    def test_exact_before_regex(self):
        """精确值先跑——被替换后正则不会再匹配到。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        engine = RedactEngine(secret_values=[secret])
        result = engine.redact(f"key: {secret}")
        self.assertEqual(result.text, "key: [REDACTED]")
        types = [m["type"] for m in result.matches]
        self.assertIn("exact_value", types)
        self.assertNotIn("openai_key", types)

    def test_placeholder_unified(self):
        """占位符统一 [REDACTED]。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["secret_value"])
        result = engine.redact("secret_value and sk-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(result.text.count("[REDACTED]"), 2)

    def test_idempotent(self):
        """幂等：[REDACTED] 再跑一遍不变。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["secret_value"])
        result1 = engine.redact("secret_value here")
        result2 = engine.redact(result1.text)
        self.assertEqual(result1.text, result2.text)
        self.assertEqual(result2.redacted_count, 0)


class TestMatchesNoLeak(unittest.TestCase):
    """matches 不含 preview/原文片段。"""

    def test_matches_no_preview_field(self):
        """matches 中没有 preview 字段。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["secret_token"])
        result = engine.redact("secret_token and sk-abcdefghijklmnopqrstuvwxyz")
        for m in result.matches:
            self.assertNotIn("preview", m)
            self.assertNotIn("raw", m)
            self.assertNotIn("value", m)
            self.assertNotIn("match", m)

    def test_matches_have_required_fields(self):
        """matches 有 type + confidence + count。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["secret_token"])
        result = engine.redact("secret_token and sk-abcdefghijklmnopqrstuvwxyz")
        for m in result.matches:
            self.assertIn("type", m)
            self.assertIn("confidence", m)
            self.assertIn("count", m)

    def test_matches_no_plaintext_secret(self):
        """matches 的字段值中不含明文 secret。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        secret = "secret_token_abcdef"
        engine = RedactEngine(secret_values=[secret])
        result = engine.redact(f"use {secret} now")
        for m in result.matches:
            for k, v in m.items():
                self.assertNotIn(secret, str(v))


class TestFailOpen(unittest.TestCase):
    """fail-open 策略 + 输入边界。"""

    def test_none_input(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact(None)
        self.assertEqual(result.text, "")
        self.assertEqual(result.redacted_count, 0)

    def test_bytes_input_decoded(self):
        """bytes 走 decode，不做 str(bytes)。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact(b"hello world")
        self.assertEqual(result.text, "hello world")
        self.assertNotIn("b'", result.text)

    def test_bytes_with_secret_decoded(self):
        """bytes 中的 secret 被 decode 后正确 redact。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine(secret_values=["my_secret_token"])
        result = engine.redact(b"use my_secret_token here")
        self.assertIn("[REDACTED]", result.text)
        self.assertNotIn("my_secret_token", result.text)

    def test_empty_string(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        result = engine.redact("")
        self.assertEqual(result.text, "")

    def test_error_count_zero_on_normal(self):
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        engine = RedactEngine()
        engine.redact("normal text")
        self.assertEqual(engine.error_count, 0)

    def test_max_input_len_truncation(self):
        """超长输入截断不崩溃。"""
        from src.domains.runtime.agents.sandbox.redact import RedactEngine, _MAX_INPUT_LEN
        engine = RedactEngine()
        long_text = "a" * (_MAX_INPUT_LEN + 100)
        result = engine.redact(long_text)
        self.assertEqual(len(result.text), _MAX_INPUT_LEN)


class TestNullRedactEngine(unittest.TestCase):
    """NullRedactEngine。"""

    def test_returns_original(self):
        from src.domains.runtime.agents.sandbox.redact import NullRedactEngine
        engine = NullRedactEngine()
        result = engine.redact("sk-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(result.text, "sk-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(result.redacted_count, 0)

    def test_none_input(self):
        from src.domains.runtime.agents.sandbox.redact import NullRedactEngine
        engine = NullRedactEngine()
        result = engine.redact(None)
        self.assertEqual(result.text, "")

    def test_bytes_input(self):
        from src.domains.runtime.agents.sandbox.redact import NullRedactEngine
        engine = NullRedactEngine()
        result = engine.redact(b"hello")
        self.assertEqual(result.text, "hello")

    def test_clear_noop(self):
        from src.domains.runtime.agents.sandbox.redact import NullRedactEngine
        engine = NullRedactEngine()
        engine.clear()
        self.assertEqual(engine.error_count, 0)

    def test_error_count_zero(self):
        from src.domains.runtime.agents.sandbox.redact import NullRedactEngine
        engine = NullRedactEngine()
        self.assertEqual(engine.error_count, 0)


class TestRedactBackwardCompat(unittest.TestCase):
    """_redact 向后兼容。"""

    def test_redact_with_tokens(self):
        from src.domains.runtime.agents.executor import _redact
        result = _redact("use my_secret_token here", ["my_secret_token"])
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("my_secret_token", result)

    def test_redact_without_tokens(self):
        """无 tokens 时仍跑正则模式。"""
        from src.domains.runtime.agents.executor import _redact
        result = _redact("sk-abcdefghijklmnopqrstuvwxyz", [])
        self.assertIn("[REDACTED]", result)

    def test_redact_none(self):
        from src.domains.runtime.agents.executor import _redact
        result = _redact(None, [])
        self.assertEqual(result, "")

    def test_redact_regex_pattern(self):
        from src.domains.runtime.agents.executor import _redact
        result = _redact("AWS: AKIAIOSFODNN7EXAMPLE", [])
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", result)


class TestModuleImportSmoke(unittest.TestCase):
    """模块加载 smoke test——正则编译 fail-fast 保障。"""

    def test_module_imports_cleanly(self):
        """所有 10 条正则编译成功。"""
        from src.domains.runtime.agents.sandbox.redact import _SensitivePatterns, _PATTERN_MAP
        self.assertTrue(hasattr(_SensitivePatterns, 'OPENAI_KEY'))
        self.assertTrue(hasattr(_SensitivePatterns, 'SSH_PATH'))
        self.assertEqual(len(_PATTERN_MAP), 10)

    def test_all_patterns_are_compiled(self):
        """所有模式都是编译后的 re.Pattern。"""
        import re
        from src.domains.runtime.agents.sandbox.redact import _SensitivePatterns
        attrs = [a for a in dir(_SensitivePatterns)
                 if not a.startswith('_') and not callable(getattr(_SensitivePatterns, a, None))]
        for attr in attrs:
            val = getattr(_SensitivePatterns, attr)
            self.assertIsInstance(val, re.Pattern, f"{attr} is not re.Pattern")


if __name__ == "__main__":
    unittest.main()
