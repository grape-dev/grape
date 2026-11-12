"""S1（登录平台≠仓库平台）/ S19（bot 身份来源链）/ SC1（CLI 源来源链）/
平台名分支守卫（抽象不变量）用例。"""
import json
import uuid

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "bot_username": "grape_bot_gitee",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


# ---------------------------------------------------------------- S1

def test_s1_login_platform_independent(gitee_card):
    """gitcode OAuth 登录用户 + gitee 手配 PAT → gitee 仓库凭据可用，登录身份不参与。"""
    from src.domains.mgmt.auth.grape_account_db import create_grape_account, create_external_identity
    from src.kernel.token_resolver import create_provider
    acc = create_grape_account(username=f"s1_{uuid.uuid4().hex[:8]}", email="", password="pw")
    assert acc
    # 登录身份：gitcode OAuth（与 gitee 无关）
    create_external_identity(acc["id"], "gitcode", "91001", access_token="at_gitcode_login")
    # 仓库凭据：gitee PAT（user_credentials，D15 批次 0 存储）
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (acc["id"], "gitee_pat", json.dumps({"token": "pat_gitee_s1"})))
        db.commit()
    try:
        p = create_provider("gitee")
        token = p.fetch_user_token(acc["username"], acc["id"])
        assert token == "pat_gitee_s1"  # 取到的是 gitee PAT，不是 gitcode 登录 token
        # 反证：登录 token 不污染 gitee 凭据
        assert token != "at_gitcode_login"
        # gitcode 侧照常走登录身份（OAuth）
        gc = create_provider("gitcode")
        assert gc.fetch_user_token(acc["username"], acc["id"]) == "at_gitcode_login"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_credentials WHERE user_id=? AND provider='gitee_pat'",
                                (acc["id"],))
            db.commit()


# ---------------------------------------------------------------- S19：bot 身份来源链

def test_s19_bot_username_source_chain(gitee_card):
    from src.domains.mgmt.user.repo_manager import get_hosting_provider_config, resolve_bot_username
    # config-only 平台：卡 bot_identity
    assert resolve_bot_username("gitee") == "grape_bot_gitee"
    assert get_hosting_provider_config("github")["bot_username"] is not None
    # D105-17 fail-closed：未知平台不再静默降级空串，显式 ValueError（消息含平台名
    # + 配卡指引）——存量库无卡静默是错配隐患，测试锁定新契约（2026-11-08 裁定）。
    import pytest as _pytest
    with _pytest.raises(ValueError, match="未知 VCS 平台 'nonexist'"):
        resolve_bot_username("nonexist")


def test_s19_bot_token_source_chain(gitee_card):
    from src.domains.mgmt.user.db import get_global_bot_token_for
    # 统一 meta key 模式：config-only 平台读 meta global_bot_<p>_token
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('global_bot_gitee_token', 'gbt_gitee')")
        db.commit()
    try:
        assert get_global_bot_token_for("gitee") == "gbt_gitee"
        # 未配置平台 → 空
        assert get_global_bot_token_for("nonexist") == ""
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='global_bot_gitee_token'")
            db.commit()


# ---------------------------------------------------------------- SC1：CLI 源 env 覆盖

def test_sc1_cli_repo_env_override(gitee_card, monkeypatch, tmp_path):
    """基线形态：模块级常量 GRAPE_CLI_REPO 直接进 clone；env 覆盖即改常量值。

    CLI 源是安装面全局配置非平台维度（平台卡 cli_repo 死维度已按
    2026-11-11 用户裁定移除），内网/隔离环境走 env 覆盖。
    """
    import src.kernel.worker_package as wp

    # 默认：gitcode 官方源
    monkeypatch.setattr(wp, "GRAPE_CLI_REPO", "https://gitcode.com/grape-dev/grape-cli.git")
    monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    seen = []

    class _Stop(Exception):
        pass

    def _fake_git(args, **kw):
        seen.append(list(args))  # 记录 clone 命令，不真跑 git

    def _no_pip(*a, **kw):
        raise _Stop  # 拦在 pip wheel（异常被 _ensure 的兜底 except 吞掉，快速返回）

    monkeypatch.setattr(wp, "_git_retry", _fake_git)
    monkeypatch.setattr(wp.subprocess, "run", _no_pip)
    wp._ensure_grape_cli_wheel(str(wheels))
    assert seen[0][0:4] == ["clone", "--depth", "1", "https://gitcode.com/grape-dev/grape-cli.git"]

    # env 覆盖：镜像源原样进 clone 命令
    monkeypatch.setattr(wp, "GRAPE_CLI_REPO", "https://mirror.internal/grape-cli.git")
    seen.clear()
    wp._ensure_grape_cli_wheel(str(wheels))
    assert seen[0][0:4] == ["clone", "--depth", "1", "https://mirror.internal/grape-cli.git"]


# ---------------------------------------------------------------- 用户级平台目录（F1 数据源）

def test_f1_user_visible_platform_catalog(gitee_card):
    from fastapi.testclient import TestClient
    from src.api.main import app
    c = TestClient(app)
    r = c.get("/api/settings/vcs/platforms")
    assert r.status_code == 200
    plats = {p["provider"]: p for p in r.json()["platforms"]}
    # D102-N（2026-11-08 用户裁定）：GitCode/GitHub 默认恒显；其余平台按
    # meta oauth_<p>_client_id 配置显示（未配置不渲染，防引导页死链）。
    # gitee 已由本用例 gitee_card fixture 配卡，但未配 oauth_client_id → 不显示；
    # 显式播 oauth_gitee_client_id 后出现。gitlab 无 OAuth 配置 → 恒不显示。
    assert {"gitcode", "github"} <= set(plats)
    assert "gitlab" not in plats and "gitee" not in plats
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_gitee_client_id', 'cid_gitee')")
        db.commit()
    try:
        r2 = c.get("/api/settings/vcs/platforms")
        plats2 = {p["provider"]: p for p in r2.json()["platforms"]}
        assert "gitee" in plats2
        assert plats2["gitee"]["host"] == "gitee.com"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='oauth_gitee_client_id'")
            db.commit()
    assert set(plats2["gitee"].keys()) <= {"provider", "name", "host", "dialect", "pat_create_url", "brand_color"}  # 无敏感字段（brand_color 为 D107 登录页品牌色）


# ---------------------------------------------------------------- 抽象不变量守卫

def test_guard_no_new_platform_branches():
    """批次 0 抽象不变量：src/ 不得出现新平台名（gitee）的代码分支。

    存量三平台（gitcode/github/gitlab）属既有豁免；方言层 header 名
    （x-gitee-signature 属 github 方言族语义）不在此列。违反 = 有人绕过
    平台卡写点对点分支，应改为来源链/卡配置。
    """
    import os
    import re
    root = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if re.search(r'==\s*["\']gitee["\']|["\']gitee["\']\s*==', line):
                        hits.append(f"{path}:{i}: {line.strip()[:80]}")
    assert hits == [], "出现 gitee 平台名分支（违反配置驱动不变量）：\n" + "\n".join(hits)


# ---------------------------------------------------------------- 扫雷第四轮（D17）

def test_d17_commit_identity_host_derived():
    """模板推送提交身份 email 的 host 从目标 URL 派生（github 仓库不再盖 gitcode 邮戳）。"""
    from src.domains.mgmt.user.repo_manager import _host_of_url
    assert _host_of_url("https://github.com/o/r.git") == "github.com"
    assert _host_of_url("https://gitee.com/o/r.git") == "gitee.com"
    assert _host_of_url("https://oauth2:tk@gitcode.com/o/r.git") == "gitcode.com"
    # D105-17 fail-closed：解析失败返回空串由调用方显式报错——不再回退
    # "gitcode.com"（老兜底会把非 gitcode 平台的失败静默伪装成 gitcode 语义）。
    assert _host_of_url("bad") == ""


def test_d17_templates_repo_env_respected(monkeypatch):
    """TEMPLATES_REPO 外部设置不再被写死值覆盖（内网镜像可配）。"""
    import inspect
    from src.api import main as _main
    src_text = inspect.getsource(_main)
    assert '_os.environ.get("TEMPLATES_REPO")' in src_text
    assert 'env={**_os.environ, "TEMPLATES_REPO": "https://gitcode.com' not in src_text
