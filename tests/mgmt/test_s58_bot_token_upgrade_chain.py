"""D38（S58）用例：bot token 升级链卡化——user/db.py 写入/重建收口。

破绽（先测后改）：
1. update_managed_repo_bot_token：provider 口径 github-else-gitcode → gitee 折叠；
   _bot_token_col 硬编码双列 → gitee 查重打错列。
2. _update_repo_bot_token：SQL 列名硬编码 bot_gitcode_token/gitcode_webhook_token
   （github 分支另写一套）→ gitee 写错列。
3. _rebuild_webhook_with_bot_token：webhook 删/建 github/else 二分 import 存量
   函数 → gitee 走 GitCode API_BASE + password 形态（打错平台）。

解法（D22 端口复用，调用方零平台分支）：provider 派生走 parse_repo_key；
列名走 create_provider().token_storage_field()/webhook_token_field()（内置默认=
存量列名，github/gitcode SQL 逐字节等价有锁）；webhook 凭据形态（gitcode
password=bot_token / github secret="" / 卡平台 generic）下沉适配器新端口
setup_repo_webhook，_rebuild 删旧/建新走 remove_webhook/setup_repo_webhook。
"""
import ast
import inspect
import json
import uuid
from unittest.mock import patch, MagicMock

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "bot_username": "g_grape_bot",
    # S58 复核（2026-11-13）：内置 configs/platforms/gitee.json 已是 F4/D106 终态
    # （token_storage_field/webhook_token_field 显式空串=凭据只落 rc 单表），meta 卡
    # 与内置卡 per-key 合并后键缺席不复活退役列——"列平台语义"场景须 meta 显式
    # 声明列名覆盖（platform_card._explicit_or_default：键存在即尊重原值）。
    # 本组用例锁定的是"有列平台"的写路径行为（列写+rc 同步 D98 / related_repos /
    # _update_repo_bot_token 直调），终态无列行为见 test_s120。
    "token_storage_field": "bot_gitee_token",
    "webhook_token_field": "gitee_webhook_token",
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


def _ensure_col(col: str):
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(managed_repos)").fetchall()]
        if col not in cols:
            db.cursor().execute(f"ALTER TABLE managed_repos ADD COLUMN {col} TEXT DEFAULT ''")
            db.commit()


def _insert_managed_repo(repo_key: str, owner="acme", name="web", webhook_id=0):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, host_user_id, webhook_id) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (repo_key, owner, name, f"{owner}/{name}", webhook_id))
        db.commit()


def _delete_managed_repo(repo_key: str):
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (repo_key,))
        db.commit()


def _row(repo_key: str) -> dict:
    with db_lock:
        r = db.cursor().execute("SELECT * FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
        cols = [c[1] for c in db.cursor().execute("PRAGMA table_info(managed_repos)").fetchall()]
        db.commit()
        return dict(zip(cols, r)) if r else {}


# ------------------------------------------------ S58 列名卡驱动 + provider 口径

def _dec(v):
    """N3：列/rc 值通道加密，断言前解密（明文兼容原样返回）。"""
    from src.domains.mgmt.user.db import _decrypt_repo_secret
    return _decrypt_repo_secret(v or "")


def _rc_val(repo_key, cred_type):
    from src.domains.mgmt.user.db import get_repo_credential_value
    return get_repo_credential_value(repo_key, cred_type)


def test_s58_update_repo_bot_token_gitee_columns(gitee_card):
    """gitee 仓（meta 卡派生列名，未显式置空→列平台语义；N3 加密）：
    列写 + rc 'bot' 同步（D98）；F4 终态（卡置空）行为见 test_s120。"""
    from src.domains.mgmt.user import db as udb
    _ensure_col("bot_gitee_token")
    _ensure_col("gitee_webhook_token")
    rk = "gitee:acme/web"
    _insert_managed_repo(rk)
    try:
        with patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="77"):
            res = udb.update_managed_repo_bot_token(rk, "bt1")
        assert res["ok"] is True
        row = _row(rk)
        assert _dec(row["bot_gitee_token"]) == "bt1"
        assert _dec(row["gitee_webhook_token"]) == "bt1"
        assert _rc_val(rk, "bot") == "bt1"
        assert (row.get("bot_gitcode_token") or "") == ""  # 不再折叠 gitcode 列
    finally:
        _delete_managed_repo(rk)


def test_s58_update_repo_bot_token_gitcode_parity():
    """gitcode 仓：列名/清洗语义与旧版逐字段等价（bot_gitcode_token 等，N3 加密形态）。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitcode:acme/legacy%s" % uuid.uuid4().hex[:4]
    _insert_managed_repo(rk, name=rk.split("/")[-1], webhook_id=3)
    try:
        with patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="3"):
            res = udb.update_managed_repo_bot_token(rk, "bt2")
        assert res["ok"] is True
        row = _row(rk)
        assert _dec(row["bot_gitcode_token"]) == "bt2"
        assert _dec(row["gitcode_webhook_token"]) == "bt2"
        assert _rc_val(rk, "bot") == "bt2"  # D98：列+rc 同步
        assert row["webhook_id"] == 3
        assert row["webhook_secret"] == ""
        assert str(row["host_user_id"]) in ("", "0")
    finally:
        _delete_managed_repo(rk)


def test_s58_update_repo_bot_token_github_parity():
    """github 仓：仍写 bot_github_token/github_webhook_token（口径不回退，N3 加密形态）。"""
    from src.domains.mgmt.user import db as udb
    rk = "github:acme/gh%s" % uuid.uuid4().hex[:4]
    _insert_managed_repo(rk, name=rk.split("/")[-1])
    try:
        with patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="9"):
            res = udb.update_managed_repo_bot_token(rk, "bt3")
        assert res["ok"] is True
        row = _row(rk)
        assert _dec(row["bot_github_token"]) == "bt3"
        assert _dec(row["github_webhook_token"]) == "bt3"
        assert _rc_val(rk, "bot") == "bt3"
    finally:
        _delete_managed_repo(rk)


def test_s58_related_repos_gitee_token_share(gitee_card):
    """共用 bot token 检测查卡派生列（gitee 不再查 bot_gitcode_token）。"""
    from src.domains.mgmt.user import db as udb
    _ensure_col("bot_gitee_token")
    _ensure_col("gitee_webhook_token")
    rk1, rk2 = "gitee:acme/web", "gitee:acme/api"
    _insert_managed_repo(rk1)
    _insert_managed_repo(rk2, name="api")
    try:
        with db_lock:
            db.cursor().execute(
                "UPDATE managed_repos SET bot_gitee_token=? WHERE repo_key=?", ("shared", rk2))
            db.commit()
        with patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="1"):
            res = udb.update_managed_repo_bot_token(rk1, "shared")
        assert res["ok"] is True
        assert rk2 in res["related_repos"]
    finally:
        _delete_managed_repo(rk1)
        _delete_managed_repo(rk2)


def test_s58_update_bot_token_function_columns_gitee(gitee_card):
    """_update_repo_bot_token 直调（meta 卡派生列名→列平台语义，N3 加密）：
    gitee 列写 + rc 'bot' 同步（升级路径共用入口）；F4 终态行为见 test_s120。"""
    from src.domains.mgmt.user import db as udb
    _ensure_col("bot_gitee_token")
    _ensure_col("gitee_webhook_token")
    rk = "gitee:acme/web"
    _insert_managed_repo(rk)
    try:
        udb._update_repo_bot_token(rk, "btg", "gitee", webhook_token="btg", webhook_id="5")
        row = _row(rk)
        assert _dec(row["bot_gitee_token"]) == "btg"
        assert _dec(row["gitee_webhook_token"]) == "btg"
        assert _rc_val(rk, "bot") == "btg"
        assert row["webhook_id"] == 5
    finally:
        _delete_managed_repo(rk)


# ------------------------------------------------ S58 webhook 重建端口分派

def test_s58_rebuild_webhook_uses_provider_port(gitee_card):
    """gitee：删旧/建新走 create_provider 端口（remove_webhook/setup_repo_webhook）。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitee:acme/web"
    _insert_managed_repo(rk, webhook_id=11)
    try:
        mock_prov = MagicMock()
        mock_prov.setup_repo_webhook.return_value = 42
        with patch("src.domains.mgmt.vcs.create_provider", return_value=mock_prov) as mcp, \
                patch("src.domains.mgmt.user.db.get_webhook_url", return_value="https://srv/webhook/gitee"):
            got = udb._rebuild_webhook_with_bot_token(rk, "tok", "gitee")
        assert got == "42"
        # 删旧 + 建新两次端口分派，均按目标平台
        assert mcp.call_count == 2
        assert all(c.args == ("gitee",) for c in mcp.call_args_list)
        mock_prov.remove_webhook.assert_called_once_with("tok", "acme", "web", 11)
        mock_prov.setup_repo_webhook.assert_called_once_with("tok", "acme", "web", "https://srv/webhook/gitee")
    finally:
        _delete_managed_repo(rk)


def test_s58_rebuild_webhook_gitcode_parity():
    """gitcode：仍委托 repo_manager.setup_webhook，password=bot_token（形态不变）。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitcode:acme/legacy"
    _insert_managed_repo(rk, webhook_id=0)
    try:
        with patch("src.domains.mgmt.user.db.get_webhook_url", return_value="https://srv/webhook/gitcode"), \
                patch("src.domains.mgmt.user.repo_manager.setup_webhook", return_value=7) as m_setup:
            got = udb._rebuild_webhook_with_bot_token(rk, "tok", "gitcode")
        assert got == "7"
        m_setup.assert_called_once_with("tok", "acme", "legacy", "https://srv/webhook/gitcode",
                                         webhook_password="tok")
    finally:
        _delete_managed_repo(rk)


def test_s58_rebuild_webhook_github_parity():
    """github：仍委托 setup_github_webhook，webhook_secret=""（形态不变）。"""
    from src.domains.mgmt.user import db as udb
    rk = "github:acme/gh"
    _insert_managed_repo(rk, webhook_id=0)
    try:
        with patch("src.domains.mgmt.user.db.get_webhook_url", return_value="https://srv/webhook/github"), \
                patch("src.domains.mgmt.user.repo_manager.setup_github_webhook", return_value=8) as m_setup:
            got = udb._rebuild_webhook_with_bot_token(rk, "tok", "github")
        assert got == "8"
        m_setup.assert_called_once_with("tok", "acme", "gh", "https://srv/webhook/github", "")
    finally:
        _delete_managed_repo(rk)


# ------------------------------------------------ S58 结构守卫

def test_s58_structure_no_platform_literals_in_upgrade_chain():
    """三个收口函数体内不得出现平台名字面量分支（D36 纪律，防回潮）。"""
    from src.domains.mgmt.user import db as udb
    banned = {"github", "gitcode", "gitee"}
    for fn_name in ("update_managed_repo_bot_token", "_update_repo_bot_token",
                    "_rebuild_webhook_with_bot_token"):
        src = inspect.getsource(getattr(udb, fn_name))
        tree = ast.parse(src)
        fn = tree.body[0] if tree.body else None  # Module → FunctionDef
        assert isinstance(fn, ast.FunctionDef), fn_name
        # 只查函数体：签名默认值（provider="gitcode" 存量兜底语义）与 docstring
        # （提及存量平台是文档语义）不在守卫范围
        _body = fn.body
        if _body and isinstance(_body[0], ast.Expr) and isinstance(_body[0].value, ast.Constant):
            _body = _body[1:]
        for stmt in _body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Compare):
                    for cmp in node.comparators + [node.left]:
                        if isinstance(cmp, ast.Constant) and isinstance(cmp.value, str) and cmp.value in banned:
                            pytest.fail(f"{fn_name} 含平台名比较: {cmp.value}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in banned:
                    # 字符串字面量本身出现在函数体（含赋值/传参）也视为回潮
                    pytest.fail(f"{fn_name} 含平台名字面量: {node.value}")


def test_s58_provider_has_setup_repo_webhook_port():
    """适配器端口契约：三类 provider 均暴露 setup_repo_webhook（凭据形态内聚）。"""
    from src.domains.mgmt.vcs import create_provider
    for p in ("gitcode", "github"):
        assert callable(getattr(create_provider(p), "setup_repo_webhook", None)), p
    from src.domains.mgmt.vcs import GenericVcsProvider
    assert callable(getattr(GenericVcsProvider, "setup_repo_webhook", None))
