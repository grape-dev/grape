"""仓库列表平台集合卡驱动化（2026-11-12）：gitee 绑定后能拉到仓库。

背景（实查）：仓库域把平台集合硬编码成 ("gitcode","github")，导致
- 已绑定 gitee 的用户 /repos/member?provider=gitee 静默返回非 gitee 仓；
- user_repos 零 gitee 行 → 前端 Gitee Tab 恒空；
- _fetch_and_cache 只有 github / else(gitcode) 两分支，拿 gitee token 也会去打 gitcode API。

收口口径（对齐 S62 provider 端口契约）：
1. 可列仓平台集合 = 平台卡目录（list_vcs_platforms），routes 不再持有平台字面量；
2. 拉取走 create_provider(provider).fetch_repos_with_role（平台差异在 Provider/卡里）；
3. repo_key 前缀由 provider 派生，归属判定用通用 `^<provider>:` 形态。
"""
import os

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC_DIR, "data", "test_sessions.db"))
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REPOS_PY = REPO_ROOT / "src/domains/mgmt/routes/repos.py"


def test_listable_providers_follow_platform_cards():
    """可列仓平台集合 = 平台卡目录（gitee 在册；源码无平台字面量二分）。"""
    from src.domains.mgmt.routes import repos

    assert "gitee" in repos._repo_listable_providers()
    src = REPOS_PY.read_text(encoding="utf-8")
    assert '("gitcode", "github")' not in src
    assert 'in ("gitcode", "github")' not in src


def test_fetch_and_cache_delegates_to_provider_port():
    """_fetch_and_cache 走 create_provider(provider).fetch_repos_with_role，前缀=provider。"""
    from src.domains.mgmt.routes import repos

    mock_prov = MagicMock()
    mock_prov.fetch_repos_with_role.return_value = [
        {"full_name": "acme/web", "name": "web", "owner": "acme", "is_admin": True}]
    with patch("src.domains.mgmt.vcs.create_provider", return_value=mock_prov) as m_factory:
        out = repos._fetch_and_cache(1, "T", "gitee")
    m_factory.assert_called_once_with("gitee")
    mock_prov.fetch_repos_with_role.assert_called_once_with("T")
    assert out[0]["repo_key"] == "gitee:acme/web"


def test_repo_key_matches():
    """归属判定：前缀精确匹配；裸键（历史数据）归 gitcode；他平台前缀不折算。"""
    from src.domains.mgmt.routes import repos

    assert repos._repo_key_matches("gitee:a/x", "gitee")
    assert not repos._repo_key_matches("gitee:a/x", "gitcode")
    assert repos._repo_key_matches("gitcode:a/x", "gitcode")
    assert repos._repo_key_matches("a/x", "gitcode")
    assert not repos._repo_key_matches("a/x", "gitee")
    assert not repos._repo_key_matches("gitlab:a/x", "gitcode")