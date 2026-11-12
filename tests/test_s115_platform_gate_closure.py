"""S115/D88：卡驱动改造漏网二元分叉收口（A2A fallback 判定 + templates provider 白名单）。

背景（D88 归因）：两处均为基线预存二元分叉，D71 全量清点时登记冻结，
卡驱动基础设施（VcsApiClient 卡驱动请求 / TokenResolver 卡工厂 / gitee 卡
backend）S107 后全部就绪，本批收口最后一米：
- 域A：webhook.py _a2a_repo_fallback_check `not in ("gitcode","github")` →
  get_platform_card 卡派生判定（gitee 仓 A2A 归属校验从直接拒变为真探测）；
- 域B：templates.py create_from_template `body.provider in ("gitcode","github")`
  白名单 → 卡在册即走显式平台（gitee 模板创建链路打通）。

测试 seam 对齐既有写法：
- 域A：test_issue_671_a2a_repo_fallback.py 的 mock 缝（TokenResolver.get_token +
  VcsApiClient.get_repo_info 方法级 mock，不 mock 端口类）；
- 域B：HTTP 响应 + get_user_token_for mock（token 缝）；
- 结构守卫：源码 grep 零 `("gitcode", "github")` 二元判定残留（RED 验证）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.api.deps import get_current_user_or_api_key  # noqa: E402

UID = 98702
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════
# 域A：_a2a_repo_fallback_check 卡派生判定
# ═══════════════════════════════════════════════════════════════════════

class TestA2AFallbackCardDriven:
    """gitee/gitlab 仓走 A2A 归属校验回退不再被二元判定拒为 unsupported。"""

    def _fallback_check(self):
        from src.domains.runtime.routes.webhook import _a2a_repo_fallback_check
        return _a2a_repo_fallback_check

    def _mock_reachable(self, monkeypatch, info=None):
        """mock 网络缝（对齐 test_issue_671 既有写法：TokenResolver + get_repo_info）。"""
        monkeypatch.setattr("src.kernel.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "tok-s115")
        monkeypatch.setattr("src.domains.runtime.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: info if info is not None else {})

    def test_gitee_repo_reaches_probe(self, monkeypatch):
        """gitee 仓：卡在册 → 不再 unsupported，走真探测返回 ok。"""
        self._mock_reachable(monkeypatch)
        assert self._fallback_check()("gitee:owner/repo") == "ok"

    def test_gitlab_repo_reaches_probe(self, monkeypatch):
        """gitlab 仓：卡在册 → 走真探测。"""
        self._mock_reachable(monkeypatch)
        assert self._fallback_check()("gitlab:owner/repo") == "ok"

    def test_gitee_repo_not_found_verdict(self, monkeypatch):
        """gitee 仓 404 → not_found（细分语义不因平台变化）。"""
        self._mock_reachable(monkeypatch, {"error": "HTTP 404", "status_code": 404})
        assert self._fallback_check()("gitee:owner/repo") == "not_found"

    def test_gitcode_github_unchanged(self, monkeypatch):
        """存量两平台行为逐字节不变（回归）。"""
        self._mock_reachable(monkeypatch)
        assert self._fallback_check()("gitcode:owner/repo") == "ok"
        assert self._fallback_check()("github:owner/repo") == "ok"

    def test_unknown_platform_still_unsupported(self, monkeypatch):
        """未知前缀（无卡）→ unsupported（防御语义保留）。"""
        self._mock_reachable(monkeypatch)
        assert self._fallback_check()("nosuchplatform:owner/repo") == "unsupported"

    def test_empty_owner_still_unsupported(self, monkeypatch):
        """owner/repo 残缺 → unsupported（与平台无关的防御保留）。"""
        self._mock_reachable(monkeypatch)
        assert self._fallback_check()("gitcode:") == "unsupported"


# ═══════════════════════════════════════════════════════════════════════
# 域B：templates create_from_template provider 白名单 → 卡在册判定
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def _tpl_client(monkeypatch):
    """依赖覆盖（org member 会话用户）+ 模板桩。"""
    from src.api.deps import require_org_member
    app.dependency_overrides[require_org_member] = lambda: {
        "id": UID, "username": "tpl_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "login_provider": "gitcode",
        "session_key": "s1", "repo_key": "gitcode:o/hello"}
    monkeypatch.setattr("src.domains.mgmt.routes.templates._find_template",
                        lambda tid: {"template": {"id": tid, "source": "git",
                                                  "source_repo": "gitcode:src/tpl",
                                                  "default_repo_name": "tpl-repo"},
                                     "scene_id": ""})
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestTemplateProviderCardDriven:
    """显式 provider 卡在册即走该平台 token 链；未知平台回落自动选择。"""

    def test_explicit_gitee_uses_gitee_token(self, monkeypatch, _tpl_client):
        """provider=gitee（卡在册）→ get_user_token_for 收到 provider='gitee'。"""
        seen = {}
        monkeypatch.setattr("src.domains.mgmt.routes.templates.get_user_token_for",
                            lambda username, user_id=0, provider="": (
                                seen.__setitem__("provider", provider), f"{provider}_tok")[1])
        r = _tpl_client.post("/api/project-templates/create",
                             json={"template_id": "t1", "provider": "gitee"})
        # 链路后续在无真实 gitee 凭据处失败，但 token 分派 provider 已锁定
        assert seen.get("provider") == "gitee", \
            "显式 gitee 必须走 gitee token 链（白名单卡点已收口）"

    def test_unknown_provider_falls_to_auto(self, monkeypatch, _tpl_client):
        """provider=nosuchplatform（无卡）→ 回落自动选择（会话主仓派生）。"""
        seen = {}
        monkeypatch.setattr("src.domains.mgmt.routes.templates.get_user_token_for",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应走显式链")))
        # _get_user_provider_and_token 是函数内 import——patch repos 模块源
        # （对齐已验证最小复现形态：lambda 带默认值 + 单表达式或语句体均可）
        import src.domains.mgmt.routes.repos as _repos_mod
        def _auto(username, login_provider="", user_id=0):
            seen["auto"] = True
            return "gitcode", "auto_tok"
        monkeypatch.setattr(_repos_mod, "_get_user_provider_and_token", _auto)
        r = _tpl_client.post("/api/project-templates/create",
                             json={"template_id": "t1", "provider": "nosuchplatform"})
        assert seen.get("auto") is True, "未知平台应回落自动选择分支（不 500、不按显式走）"

    def test_gitcode_explicit_unchanged(self, monkeypatch, _tpl_client):
        """存量显式 gitcode 行为不变（回归）。"""
        seen = {}
        monkeypatch.setattr("src.domains.mgmt.routes.templates.get_user_token_for",
                            lambda username, user_id=0, provider="": (
                                seen.__setitem__("provider", provider), f"{provider}_tok")[1])
        r = _tpl_client.post("/api/project-templates/create",
                             json={"template_id": "t1", "provider": "gitcode"})
        assert seen.get("provider") == "gitcode"


# ═══════════════════════════════════════════════════════════════════════
# 结构守卫：零二元判定残留（防回潮）
# ═══════════════════════════════════════════════════════════════════════

class TestNoBinaryProviderGate:
    """两处二元分叉收口后，源码不得再出现同形态判定（防回潮守卫）。"""

    def test_webhook_a2a_no_binary_gate(self):
        src = open(os.path.join(REPO_ROOT, "src/domains/runtime/routes/webhook.py"),
                   encoding="utf-8").read()
        # 只锁 _a2a_repo_fallback_check 函数体（其他 legitimately 二元语义不在本批范围）
        m = re.search(r"def _a2a_repo_fallback_check.*?(?=\ndef )", src, re.S)
        assert m, "函数定位失败"
        assert 'not in ("gitcode", "github")' not in m.group(0), \
            "A2A fallback 判定回潮为二元分叉（应为 get_platform_card 卡派生）"
        assert "get_platform_card" in m.group(0), \
            "A2A fallback 判定必须走卡派生"

    def test_templates_no_binary_gate(self):
        src = open(os.path.join(REPO_ROOT, "src/domains/mgmt/routes/templates.py"),
                   encoding="utf-8").read()
        assert 'body.provider in ("gitcode", "github")' not in src, \
            "templates provider 白名单回潮为二元分叉（应为卡在册判定）"


# ═══════════════════════════════════════════════════════════════════════
# 域C：admin verify-token provider 白名单 → 卡派生（D88 同族收口）
# ═══════════════════════════════════════════════════════════════════════

class TestAdminVerifyTokenCardDriven:
    """POST /api/admin/config/verify-token：卡在册平台即可验证（下游
    config_store.verify_token D30/S50 已卡驱动——任何已配平台可用，白名单
    是入口判定与下游能力脱节的最后一米）。whitelist 端点不收：login_whitelist
    表仅 gitcode/github 两列（物理边界，D43/S63 登记归渠道列 DDL 批次）。"""

    def _admin_client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import require_admin
        app.dependency_overrides[require_admin] = lambda: {
            "id": 1, "username": "admin", "token_type": "admin"}
        return TestClient(app, raise_server_exceptions=False)

    @pytest.fixture(autouse=True)
    def _clear_admin_override(self):
        """dependency_overrides 是 app 级全局态——不清理会绕过后续用例的鉴权
        （实测 test_admin_vcs_platforms::test_s18_requires_admin 组合挂、
        隔离绿：override 残留使匿名 GET /api/admin/vcs-platforms 返回 200）。"""
        yield
        from src.api.main import app
        app.dependency_overrides.clear()

    def _patch_verify(self, monkeypatch, fn):
        """verify_token 是函数内 import（from src.domains.mgmt.admin.config_store）——patch 源模块。

        必须按「模块对象」patch，不能用 monkeypatch 的点号字符串目标：点号目标经
        _pytest.monkeypatch.resolve() 走的是父包属性链（getattr(src, 'domains')…），
        而 tests/conftest.py 的 _isolate_src_modules_and_env 逐用例只快照/恢复
        sys.modules、不还原父包上的 config_store 属性——**实测**（本文件类内
        顺序跑 gitee→gitcode/github）字符串目标会把 mock 装到已废弃的模块对象上，
        路由 import 到的仍是真实 verify_token，打真实 /user 接口 401 假红，
        单条隔离跑则正常。按模块对象 patch 与路由 import 命中同一对象。
        """
        import src.domains.mgmt.admin.config_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "verify_token", fn)

    def test_gitee_token_verification_accepted(self, monkeypatch):
        """provider=gitee（卡在册）→ 不再 400 INVALID_PROVIDER，走 verify_token 链。"""
        seen = {}
        self._patch_verify(monkeypatch,
                           lambda provider, token: seen.__setitem__("provider", provider)
                           or {"login": "u@g", "name": "n"})
        c = self._admin_client()
        r = c.post("/api/admin/config/verify-token",
                   json={"provider": "gitee", "token": "tok-x"})
        assert seen.get("provider") == "gitee", \
            "gitee（卡在册）必须进入 verify_token 链（下游已卡驱动，白名单是唯一卡点）"
        assert r.status_code == 200 and r.json().get("ok") is True

    def test_gitcode_github_unchanged(self, monkeypatch):
        """存量双平台回归。"""
        self._patch_verify(monkeypatch, lambda provider, token: {"login": "u", "name": "n"})
        c = self._admin_client()
        for prov in ("gitcode", "github"):
            r = c.post("/api/admin/config/verify-token",
                       json={"provider": prov, "token": "t"})
            assert r.status_code == 200 and r.json().get("ok") is True, prov

    def test_unknown_provider_still_rejected(self, monkeypatch):
        """未知平台（无卡）→ 400 INVALID_PROVIDER（防御语义保留）。"""
        c = self._admin_client()
        r = c.post("/api/admin/config/verify-token",
                   json={"provider": "nosuchplatform", "token": "t"})
        assert r.status_code == 400
        assert r.json()["detail"].get("error_code") == "INVALID_PROVIDER"

    def test_empty_token_still_rejected(self, monkeypatch):
        """空 token → 400 TOKEN_REQUIRED（与平台无关的防御保留）。"""
        c = self._admin_client()
        r = c.post("/api/admin/config/verify-token",
                   json={"provider": "gitee", "token": ""})
        assert r.status_code == 400
        assert r.json()["detail"].get("error_code") == "TOKEN_REQUIRED"

    def test_admin_routes_no_binary_gate(self):
        """结构守卫：verify-token 函数体内零二元白名单（口径圈定函数体，S112 教训）。

        whitelist 端点的同形态判定属表结构冻结例外（login_whitelist 表仅
        gitcode/github 两列，D43/S63 登记归渠道列 DDL 批次）——不在本守卫范围。
        """
        src = open(os.path.join(REPO_ROOT, "src/domains/mgmt/admin/routes.py"),
                   encoding="utf-8").read()
        m = re.search(r"def verify_platform_token.*?(?=\n@router|\ndef )", src, re.S)
        assert m, "verify_platform_token 函数定位失败"
        body = m.group(0)
        assert 'not in ("gitcode", "github")' not in body, \
            "admin verify-token 白名单回潮为二元分叉（应为卡派生判定）"
        assert "get_platform_card" in body, "verify-token 判定必须走卡派生"
