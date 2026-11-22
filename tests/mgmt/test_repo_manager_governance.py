"""D105-17 批次 B：repo_manager 治理行为锁（HTTP mock + stub 隔离）。

定稿口径：docs/weekly-09/multi-platform-decouple/d105l-repo-manager-governance-design.md
（2026-11-09 用户裁定 fail-closed）。

锁定语义（批次 A 落盘后的目标形态）：
1. 未知平台 fail-closed 三点：_host_of_url 解析失败 / resolve_bot_username
   未知平台 / 模板 source_platform 未知平台——均显式 ValueError 且消息含平台名，
   不再静默回退 gitcode；
2. github webhook 分派经 get_hosting_provider_config 正常（函数指针集 +
   webhook 后缀 + API 面正确）；
3. 卡驱动 target host：模板 push 到非 gitcode/github 平台 → push URL 用卡 host。

隔离纪律：src/domains/mgmt/user/repo_manager.py 与 src/token_resolver.py 正被批次 A 并行
修改——本文件全部经 monkeypatch stub 依赖（platform_card / user.db / _run_git /
HTTP 层），不真调上游网络；不触碰 src 源码。fail-closed 三锁带 _batch_a_landed
自动门：批次 A 落盘前跳过（旧实现按设计会红），落盘后自动激活为硬锁。
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import inspect
from types import SimpleNamespace

import pytest

from src.domains.mgmt.user import repo_manager as rm


# ────────────────────────── 批次 A 落盘探测（自动激活门） ──────────────────────────

def _batch_a_landed(feature: str) -> bool:
    """探测批次 A 是否已移除对应存量回退分支；True = 行为锁应激活。"""
    if feature == "host":
        # 旧实现：_host_of_url 解析失败回退 "gitcode.com"
        return 'or "gitcode.com"' not in inspect.getsource(rm._host_of_url)
    if feature == "bot_username":
        # 旧实现：未知平台回退 gitcode pool
        return 'GLOBAL_BOT_USERNAME_SOURCES["gitcode"]' not in inspect.getsource(
            rm.resolve_bot_username)
    if feature == "template_source":
        # 旧实现：source_platform 缺省 "gitcode"
        return '"source_platform", "gitcode"' not in inspect.getsource(
            rm.push_template_to_repo)
    raise ValueError(feature)


# ────────────────────────── HTTP mock fixture ──────────────────────────

class _FakeVCSHTTP:
    """repo_manager HTTP 层打桩：记录 (method, url, token, body/params)，按脚本应答。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.script: dict[str, object] = {}  # url → 应答（dict/list）或 Exception

    def req(self, method, url, token="", body=None):
        self.calls.append((method, url, token, body))
        resp = self.script.get(url)
        if isinstance(resp, Exception):
            raise resp
        return {} if resp is None else resp

    def req_query(self, method, path, token, params=None, base=None):
        url = f"{base or rm.API_BASE}{path}"
        self.calls.append((method, url, token, params))
        resp = self.script.get(url)
        if isinstance(resp, Exception):
            raise resp
        return {} if resp is None else resp


@pytest.fixture()
def mock_vcs_http(monkeypatch):
    fake = _FakeVCSHTTP()
    monkeypatch.setattr(rm, "_req", fake.req)
    monkeypatch.setattr(rm, "_req_query", fake.req_query)
    return fake


@pytest.fixture()
def fake_bot_sources(monkeypatch):
    """stub 全局 bot 身份来源链（src.domains.mgmt.user.db），隔离 db 真实读取。"""
    import src.domains.mgmt.user.db as user_db
    sources = {"gitcode": lambda: ["gc-bot"], "github": lambda: ["gh-bot"]}
    monkeypatch.setattr(user_db, "GLOBAL_BOT_USERNAME_SOURCES", sources)
    return sources


@pytest.fixture()
def no_card(monkeypatch):
    """stub 平台卡为缺卡（get_platform_card → None）。"""
    import src.domains.runtime.vcs.platform_card as pc
    monkeypatch.setattr(pc, "get_platform_card", lambda p: None)
    return pc


# ────────────────────────── 行为锁 1：未知平台 fail-closed 三点 ──────────────────────────

@pytest.mark.skipif(not _batch_a_landed("host"),
                    reason="批次 A 未落盘：_host_of_url 仍有 gitcode.com 回退（落盘后本锁自动激活）")
@pytest.mark.skipif(not _batch_a_landed("host"),
                    reason="批次 A 未落盘：_host_of_url 仍有 gitcode.com 回退（落盘后本锁自动激活）")
def test_unknown_url_host_fail_closed():
    """锁 1a：URL host 解析失败 → 返回空串（上游 fail-closed），不再回退 gitcode.com。"""
    assert rm._host_of_url("::::not-a-url::::") == ""
    assert rm._host_of_url("") == ""


@pytest.mark.skipif(not _batch_a_landed("bot_username"),
                    reason="批次 A 未落盘：resolve_bot_username 仍有 gitcode pool 兜底")
@pytest.mark.skipif(not _batch_a_landed("bot_username"),
                    reason="批次 A 未落盘：resolve_bot_username 仍有 gitcode pool 兜底")
def test_unknown_platform_bot_username_fail_closed(fake_bot_sources, monkeypatch):
    """锁 1b：未知平台（无来源链、不在已知集、无卡）→ ValueError 含平台名。"""
    monkeypatch.setattr(rm, "_KNOWN_VCS_PROVIDERS", lambda: {"gitcode", "github", "gitlab"})
    monkeypatch.setattr(rm, "_card_bot_username", lambda p: "")
    with pytest.raises(ValueError) as ei:
        rm.resolve_bot_username("nosuchplatform")
    assert "nosuchplatform" in str(ei.value)


@pytest.mark.skipif(not _batch_a_landed("template_source"),
                    reason="批次 A 未落盘：source_platform 仍缺省 gitcode")
@pytest.mark.skipif(not _batch_a_landed("template_source"),
                    reason="批次 A 未落盘：source_platform 仍缺省 gitcode")
def test_unknown_template_source_platform_fail_closed(monkeypatch):
    """锁 1c：模板 source_platform 为未知平台 → ValueError 含平台名（不静默走 gitcode 下载器）。"""
    monkeypatch.setattr(rm, "_run_git", lambda args, cwd, timeout=120: "")
    template = {"source": "git", "source_repo": "own/repo",
                "source_platform": "nosuchplatform"}
    with pytest.raises(ValueError) as ei:
        rm.push_template_to_repo(template, "owner", "target", "tok")
    assert "nosuchplatform" in str(ei.value)


# ────────────────────────── 行为锁 2：github webhook 分派经 get_hosting_provider_config ──────────────────────────

def test_github_webhook_dispatch_via_provider_config(mock_vcs_http, fake_bot_sources):
    """锁 2：get_hosting_provider_config("github") 返回 github 专属函数集 + 卡派生后缀；
    find_webhook 分派真打 GitHub API 面（HTTP mock，不真联网）。"""
    import src.domains.mgmt.user.db as user_db
    import src.domains.runtime.vcs.platform_card as pc
    import src.kernel.config

    monkey_targets = [
        (user_db, "get_global_bot_token_for", lambda p: f"bot-{p}-tok"),
        (pc, "get_platform_card",
         lambda p: SimpleNamespace(host="github.com", webhook_path="/webhook/github",
                                   bot_username="gh-bot", dialect="github",
                                   webhook_dialect="github_web",
                                   api_base="https://api.github.com",
                                   token_storage_field="bot_github_token",
                                   webhook_token_field="github_webhook_token")),
    ]
    saved = [(m, n, getattr(m, n)) for m, n, _ in monkey_targets]
    try:
        for m, n, v in monkey_targets:
            setattr(m, n, v)
        cfg = rm.get_hosting_provider_config("github")
    finally:
        for m, n, v in saved:
            setattr(m, n, v)

    # D105-19：三件套按卡 webhook_dialect 分派（partial 绑定卡 api_base）
    assert cfg["setup_webhook"].func is rm.setup_github_webhook
    assert cfg["find_webhook"].func is rm.find_existing_github_webhook
    assert cfg["remove_webhook"].func is rm.remove_github_webhook
    assert cfg["find_webhook"].keywords == {"api_base": "https://api.github.com"}
    assert cfg["setup_webhook"].keywords == {"api_base": "https://api.github.com"}
    assert cfg["webhook_url_suffix"] == "/webhook/github"
    assert cfg["db_token_field"] == "github_webhook_token"
    assert cfg["bot_username"] == "gh-bot"

    # 分派实弹：经 cfg 取 find_webhook 调 GitHub hooks API（mock 层）
    monkey_src = rm._github_req
    try:
        rm._github_req = mock_vcs_http.req  # find_existing_github_webhook 走 _github_req
        assert cfg["find_webhook"]("gh-tok", "own", "repo", "https://x/hook") is None
    finally:
        rm._github_req = monkey_src
    assert mock_vcs_http.calls, "github webhook 查找应发出 API 请求"
    method, url, token, _body = mock_vcs_http.calls[0]
    assert method == "GET" and url == "https://api.github.com/repos/own/repo/hooks"
    assert token == "gh-tok"


# ────────────────────────── 行为锁 2b：新平台复用 webhook 家族（卡声明即分派） ──────────────────────────

def test_new_platform_reuses_webhook_family_via_card(mock_vcs_http, fake_bot_sources):
    """锁 2b（D105-19）：自定义平台（如 gitea.example.com，github_web 家族）零代码接入——
    卡声明 webhook_dialect=github_web + 自己的 api_base，三件套即绑定到该平台 API 面；
    webhooks 请求真打到自定义 base（不再回落 gitcode/github 硬编码）。"""
    import src.domains.mgmt.user.db as user_db
    import src.domains.runtime.vcs.platform_card as pc

    monkey_targets = [
        (user_db, "get_global_bot_token_for", lambda p: f"bot-{p}-tok"),
        (rm, "resolve_bot_username", lambda p: "gitea-bot"),
        (pc, "get_platform_card",
         lambda p: SimpleNamespace(host="gitea.example.com", webhook_path="/webhook/gitea",
                                   bot_username="gitea-bot", dialect="github",
                                   webhook_dialect="github_web",
                                   api_base="https://gitea.example.com/api/v1",
                                   token_storage_field="",
                                   webhook_token_field="")),
    ]
    saved = [(m, n, getattr(m, n)) for m, n, _ in monkey_targets]
    try:
        for m, n, v in monkey_targets:
            setattr(m, n, v)
        cfg = rm.get_hosting_provider_config("gitea")
    finally:
        for m, n, v in saved:
            setattr(m, n, v)

    assert cfg["find_webhook"].func is rm.find_existing_github_webhook
    assert cfg["find_webhook"].keywords == {"api_base": "https://gitea.example.com/api/v1"}
    assert cfg["webhook_url_suffix"] == "/webhook/gitea"

    monkey_src = rm._github_req
    try:
        rm._github_req = mock_vcs_http.req
        assert cfg["find_webhook"]("gitea-tok", "own", "repo", "https://x/hook") is None
    finally:
        rm._github_req = monkey_src
    assert mock_vcs_http.calls, "gitea webhook 查找应发出 API 请求"
    method, url, token, _body = mock_vcs_http.calls[-1]
    assert method == "GET" and url == "https://gitea.example.com/api/v1/repos/own/repo/hooks"
    assert token == "gitea-tok"


# ────────────────────────── 行为锁 2c：webhook_dialect 未声明 fail-closed ──────────────────────────

def test_webhook_dialect_undeclared_fail_closed(monkeypatch):
    """锁 2c（D105-19）：卡存在但 webhook_dialect 未声明且不可派生 → 显式 ValueError
    （含平台名与声明指引），不再静默回落 gitcode/github 函数打错平台。"""
    import src.domains.runtime.vcs.platform_card as pc
    monkeypatch.setattr(pc, "get_platform_card",
                        lambda p: SimpleNamespace(host="x.example.com", dialect="gitlab",
                                                  webhook_dialect="", api_base="https://x/api"))
    with pytest.raises(ValueError) as ei:
        rm.get_hosting_provider_config("xplatform")
    assert "xplatform" in str(ei.value) and "webhook_dialect" in str(ei.value)


# ────────────────────────── 行为锁 3：卡驱动 target host ──────────────────────────

def test_card_driven_target_host_on_template_push(tmp_path, monkeypatch):
    """锁 3：模板 push 到非 gitcode/github 平台 → remote push URL 用目标平台卡 host。

    全链隔离：_run_git 只记录不执行；平台卡 stub 注入卡 host；模板走 local 源。
    """
    import src.kernel.config

    # local 模板源：EXAMPLE_DIR/<id>/ 且已带 .git（跳过 git init）
    tmpl_dir = tmp_path / "tmpl_cardhost"
    tmpl_dir.mkdir()
    (tmpl_dir / "README.md").write_text("hi", encoding="utf-8")
    (tmpl_dir / ".git").mkdir()
    monkeypatch.setattr(src.kernel.config, "EXAMPLE_DIR", str(tmp_path))

    git_calls: list[list[str]] = []
    monkeypatch.setattr(rm, "_run_git",
                        lambda args, cwd, timeout=120: git_calls.append(args) or "")

    import src.domains.runtime.vcs.platform_card as pc
    monkeypatch.setattr(pc, "get_platform_card",
                        lambda p: SimpleNamespace(host="vcs.example.com"))

    ok = rm.push_template_to_repo({"source": "local", "id": "tmpl_cardhost"},
                                  "owner", "target", "user-tok", provider="acme")
    assert isinstance(ok, dict) and ok.get("ok") is True and ok.get("pushed_to") == "main"
    remote = [a for a in git_calls if a[:2] == ["remote", "add"]]
    assert remote, "应设置 push remote"
    push_url = remote[0][3]
    assert push_url.endswith("vcs.example.com/owner/target.git"), push_url
    # 提交邮戳 host 同源派生（D17）
    email = [a for a in git_calls if a[:2] == ["config", "user.email"]]
    assert email and email[0][2].endswith("@vcs.example.com"), email
