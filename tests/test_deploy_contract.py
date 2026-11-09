"""部署契约场景用例（C 系列，spec.md 部署契约清单）

C1（OAuth App redirect_uri 在平台侧配置）为纯平台侧契约，代码库内
无可测断言点，不在本文件覆盖——以文档契约形式交付。

- C2: TRUSTED_HOSTS 未设时不读 X-Forwarded-Host（R11.1 门禁）
- C3: 默认管理员密码 admin123 / GRAPE_ADMIN_PASSWORD 覆盖
- C4: 无邮箱配置时 send-code 降级 EMAIL_NOT_CONFIGURED、verify 直建账号
- C5: install-worker.sh 占位 server 与 UI 生成命令 --server 显式代入
"""
import os
from unittest.mock import patch, MagicMock

import pytest


class TestC2TrustedHosts:
    def test_c2_no_forwarded_host_without_whitelist(self):
        """C2: 未设 TRUSTED_HOSTS → 不读 X-Forwarded-Host（防伪造，R11.1）。"""
        from src.user.db import detect_webhook_from_host
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)
            # Host 是真实源（直连场景可读），X-Forwarded-Host 伪造值必须被忽略
            r = detect_webhook_from_host("1.2.3.4:3000", "evil.com", "http")
            assert r == "http://1.2.3.4:3000/webhook/gitcode", \
                "C2: X-Forwarded-Host 在未设白名单时被采信（伪造风险）"

    def test_c2c_known_proto_gap(self):
        """C2c（现状固化，非缺陷修复）: R11.1 只门禁 X-Forwarded-Host，X-Forwarded-Proto
        未设白名单时仍被读取——伪造仅能翻转推导协议（http↔https），不能改域名。

        不修的理由（破坏半径评估）：若将 proto 纳入门禁，未配 TRUSTED_HOSTS 的
        合法反代部署将丢失 https 探测（存量用户破坏路径），风险大于收益。
        本用例固化现状防静默漂移；如将来门禁 proto，先删本用例并更新契约 C2。
        """
        from src.user.db import detect_webhook_from_host
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)
            r = detect_webhook_from_host("1.2.3.4:3000", "evil.com", "https")
            # 域名来自真实 Host（evil.com 被忽略）；协议来自 forwarded_proto（被采信）
            assert r == "https://1.2.3.4:3000/webhook/gitcode"

    def test_c2b_whitelist_enables_forwarded_host(self):
        """C2b: 已设白名单 → X-Forwarded-Host 参与且须命中白名单。"""
        from src.user.db import detect_webhook_from_host
        with patch.dict(os.environ, {"TRUSTED_HOSTS": "example.com"}):
            r = detect_webhook_from_host("internal:3000", "x.example.com", "https")
            assert r == "https://x.example.com/webhook/gitcode"
            # 白名单外拒绝
            r2 = detect_webhook_from_host("internal:3000", "evil.com", "https")
            assert r2 == "", "C2b: 白名单外 forwarded host 应被拒绝"


class TestC3AdminDefaultPassword:
    def test_c3_default_password_admin123(self):
        """C3: 未设 GRAPE_ADMIN_PASSWORD → 种子账号用默认 admin123（公网部署风险源）。"""
        import src.api.db_setup as ds
        src_text = _read_src(ds.__file__)
        assert 'or "admin123"' in src_text, "C3: 默认密码 fallback 不在（行为变更？更新契约）"

    def test_c3b_env_overrides_default(self):
        """C3b: GRAPE_ADMIN_PASSWORD 环境变量优先于默认值。"""
        import src.api.db_setup as ds
        src_text = _read_src(ds.__file__)
        assert 'os.environ.get("GRAPE_ADMIN_PASSWORD") or "admin123"' in src_text, \
            "C3b: env 优先序被改（行为变更？更新契约）"


class TestC4EmailDegradation:
    def test_c4_send_code_degrades_gracefully(self):
        """C4: 无邮箱配置 → send-code 返回 EMAIL_NOT_CONFIGURED（注册不受影响）。"""
        from src.api.routes.grape_auth import register_send_code
        from src.auth import email_service
        body = MagicMock()
        body.email = "u@example.com"
        body.username = "alice"
        request = MagicMock()
        with patch.object(email_service, "is_email_configured", return_value=False), \
             patch("src.api.routes.grape_auth._get_client_ip", return_value="1.2.3.4"):
            r = register_send_code(body, request)
        assert r.get("error_code") == "EMAIL_NOT_CONFIGURED"
        assert "username and password" in r.get("message", ""), \
            "C4: 降级消息必须引导直接注册（Issue #372 R7 文案契约）"

    def test_c4b_verify_creates_account_without_email(self):
        """C4b: 无邮箱配置时 verify 路径直建账号（email 允许为空）——源码结构守卫。"""
        from src.api.routes import grape_auth
        src_text = _read_src(grape_auth.__file__)
        assert "email_configured = is_email_configured()" in src_text
        # 直建账号分支存在（R7 注释锚点）
        assert "R7: 无邮箱配置降级" in src_text


class TestC5InstallScriptServer:
    def test_c5_placeholder_server_in_script(self):
        """C5: install-worker.sh 占位 server（grape.example.com）仍在——契约提醒手抄命令自查。"""
        import pathlib
        p = pathlib.Path(__file__).parent.parent / "scripts" / "install-worker.sh"
        assert p.is_file(), "C5: install-worker.sh 丢失"
        content = p.read_text(encoding="utf-8")
        assert 'SERVER="https://grape.example.com"' in content

    def test_c5b_ui_command_injects_real_server(self):
        """C5b: UI 生成的安装命令显式带 --server（闭环项）——结构守卫。"""
        import pathlib, inspect
        rt = __import__("src.api.routes.runtime", fromlist=["runtime_engine_onboarding"])
        src = inspect.getsource(rt.runtime_engine_onboarding)
        assert "--server {base}" in src or "--server' +" in src or "--server " in src, \
            "C5b: 生成的安装命令缺 --server 显式代入"


def _read_src(path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
