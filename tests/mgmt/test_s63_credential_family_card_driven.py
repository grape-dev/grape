"""D43（S63）用例：凭据/列名族卡化——admin 面 + settings + bot 健康 + 路径 B 查询。

D42 后剩余 30 处调用方站点的第一族（凭据/列名，11 处，与 D38 卡派生
列同型）：
- config_store：swap 触发 provider 口径（field 名派生）/列名/权限默认
- settings：repo config 列名 / token 保存前探测 URL
- bot_token_health：仓库级 token 列名
- user/db：_query_path_b_repos 前缀子句（裸键=默认平台特例保 parity）
- admin/db：登录白名单列映射表（未知平台拒绝）

验收门：五文件 AST 守卫——平台名字面量不得出现在比较节点。
"""
import ast
import json
from unittest.mock import patch, MagicMock

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
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


# ------------------------------------------------ S63 文件级守卫（验收门）

_GUARD_FILES = [
    "src/domains/mgmt/admin/config_store.py",
    "src/domains/mgmt/admin/db.py",
    "src/domains/mgmt/routes/settings.py",
    "src/domains/runtime/engine/bot_token_health.py",
]
# domains/mgmt/user/db.py 含平台名访问器（get_gitcode_token 等）存量函数，只守本单收口的函数
_GUARD_USER_DB_FUNCS = ["_path_b_prefix_clause", "_query_path_b_repos", "_rebuild_webhook_url_for_provider"]


def _cmp_platform_operands(node, banned):
    """Compare 节点的【直接操作数】中的平台名常量。

    S63 守卫修正：原实现对整棵 Compare 子树做 walk，把比较表达式里嵌套调用的
    平台键（如 _identities.get("gitcode", {}).get("status", 0) == 4 的 "gitcode"
    ——settings.py legacy 兼容键，勿删）误判成平台名比较。守卫本义=比较值本身
    是平台名（provider == "github" 形态），故只扫 left/comparators 直接操作数。
    """
    out = []
    for op in [node.left] + list(node.comparators):
        if isinstance(op, ast.Constant) and isinstance(op.value, str) and op.value in banned:
            out.append(op)
    return out


def test_s63_guard_no_platform_compare_in_credential_family():
    import inspect
    banned = {"github", "gitcode", "gitee"}
    offenders = []
    for path in _GUARD_FILES:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for sub in _cmp_platform_operands(node, banned):
                    offenders.append(f"{path}:L{sub.lineno}:{sub.value}")
    from src.domains.mgmt.user import db as _udb
    for fn_name in _GUARD_USER_DB_FUNCS:
        fn = getattr(_udb, fn_name)
        tree = ast.parse(inspect.getsource(fn))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for sub in _cmp_platform_operands(node, banned):
                    offenders.append(f"domains/mgmt/user/db.py:{fn_name}:L{sub.lineno}:{sub.value}")
    assert not offenders, f"凭据族文件仍有平台名比较 {len(offenders)} 处: {offenders[:12]}"


# ------------------------------------------------ S63 列名卡派生

def test_s63_swap_columns_card_driven(gitee_card):
    """global_bot_token_swap：gitee → grape_t_repo_credentials 口径（S82/D58 + N3 解密比对）。

    步骤 3 拉 rc cred_type='bot' 全量行 Python 侧解密等值比对（N3 加密随机
    nonce，密文等值不命中）；步骤 6 凭据单写 rc 两步法 UPDATE→INSERT。
    平台隔离由 repo_key 前缀 + token 本身平台专属保证（D99），不再引用
    managed_repos 物理列（bot_gitee_token 已冻结退役）。
    """
    from src.domains.mgmt.admin import config_store as cs
    _prov = MagicMock()
    _prov.verify_token.return_value = {"login": "new_bot"}
    _cursor = MagicMock()
    # 拆 JOIN 后的两查都是二列形态：rc.public.repo_bot_credential_pairs = (repo_key, value 密文)，
    # managed_repos 本地读 = (repo_key, bot_username)；同一 mock 游标喂两次 fetchall。
    # （旧注释「rc 三列形态」是单条跨域 JOIN 时代的期望，实现拆 JOIN 后此用例一直
    #  因 3 元组解包失败而红——修 mock 形状，断言口径不变。）
    _cursor.fetchall.return_value = [("gitee:acme/web", "ENCOLD")]
    _cursor.execute.return_value.rowcount = 1
    with patch("src.kernel.token_resolver.create_provider", return_value=_prov) as m_cp, \
            patch("src.domains.mgmt.user.db.get_webhook_url", return_value="http://wh/webhook/gitee"), \
            patch("src.domains.mgmt.user.db._get_managed_webhook_id", return_value=0), \
            patch("src.domains.mgmt.user.db._rebuild_webhook_url_for_provider",
                  side_effect=lambda u, p: u), \
            patch("src.domains.mgmt.user.db._decrypt_repo_secret", return_value="old_tok"), \
            patch("src.domains.mgmt.user.db._encrypt_repo_secret", return_value="ENCNEW"), \
            patch("src.kernel.session_manager.db") as _mdb, \
            patch("src.kernel.session_manager.db_lock"):
        _mdb.cursor.return_value = _cursor
        cs.global_bot_token_swap("gitee", "old_tok", "new_tok")
    m_cp.assert_called_with("gitee")
    sql = " ".join(str(c.args[0]) for c in _cursor.execute.call_args_list)
    _all_args = [a for c in _cursor.execute.call_args_list for a in (c.args[1:] or ())]
    # S82/D58：凭据唯一权威 = grape_t_repo_credentials（cred_type 归一 {'bot','webhook'}）
    assert "grape_t_repo_credentials" in sql and "cred_type" in sql, \
        "swap 步骤 3/6 未走 grape_t_repo_credentials 口径（S82/D58 破坏）"
    # 平台隔离：token 等值命中 gitee 仓（解密后比对）+ 步骤 6 写回新 token
    assert any("gitee:acme/web" in str(a) for a in _all_args) or \
        any("gitee:acme/web" == str(a) for a in _all_args), \
        "swap 未命中 gitee 凭据域（平台隔离破坏）"
    assert any("ENCNEW" in str(a) for a in _all_args), "swap 未把新 token 加密写回 rc"
    assert {"bot", "webhook"} <= {a for t in _all_args if isinstance(t, tuple)
                                  for a in map(str, t)}, \
        "swap 步骤 6 应对 bot/webhook 两 cred_type 各写一行"
    # 列冻结：managed_repos 物理列名不得再出现（bot_gitee_token 已退役）
    assert "bot_gitee_token" not in sql and "bot_gitcode_token" not in sql
    # 权限默认值走适配器（调用方不传平台差异权限）
    _prov.add_collaborator.assert_called()
    _prov.setup_webhook.assert_called()


def test_s63_query_path_b_repos_gitee(gitee_card):
    """_query_path_b_repos：gitee → rc 单表口径（F4/D106：空卡列平台无物理列）。

    断言（2026-11-12 D106 终态口径）：LIKE 'gitee:%' 仓过滤 + NOT EXISTS(
    grape_t_repo_credentials cred_type='bot' 非空行)；不再出现 bot_gitee_token
    物理列（旧口径断言已随实现退役）。
    """
    from src.domains.mgmt.user import db as udb
    from src.kernel.token_resolver import create_provider
    create_provider("gitee").token_storage_field()  # 预热卡缓存（防 db mock 干扰目录读）
    _cursor = MagicMock()
    _cursor.fetchall.return_value = []
    with patch.object(udb, "db") as _mdb, patch.object(udb, "db_lock"):
        _mdb.cursor.return_value = _cursor
        udb._query_path_b_repos("gitee")
    sql = str(_cursor.execute.call_args.args[0])
    assert "LIKE 'gitee:%'" in sql
    assert "cred_type='bot'" in sql, "gitee 应走 rc 单表 bot 凭据判定（D106 终态）"
    assert "bot_gitee_token" not in sql, "gitee 无物理列（旧列口径已退役）"


def test_s63_query_path_b_repos_gitcode_parity():
    """gitcode parity：列 bot_gitcode_token + 裸键/非 github 前缀语义保持。"""
    from src.domains.mgmt.user import db as udb
    _cursor = MagicMock()
    _cursor.fetchall.return_value = []
    with patch.object(udb, "db") as _mdb, patch.object(udb, "db_lock"):
        _mdb.cursor.return_value = _cursor
        udb._query_path_b_repos("gitcode")
    sql = str(_cursor.execute.call_args.args[0])
    # 语义等价：gitcode 列 + 裸键归入（旧 NOT LIKE 'github:%' 的语义泛化形态）
    assert "bot_gitcode_token" in sql and "NOT LIKE '%:%'" in sql and "LIKE 'gitcode:%'" in sql


def test_s63_bot_token_health_columns(gitee_card):
    """_verify_single_repo_token：gitee → rc 优先（F4/D106），legacy 列回落仅 rc 空。"""
    from src.domains.runtime.engine import bot_token_health as bth

    _prov = MagicMock()
    _prov.token_storage_field.return_value = "bot_gitee_token"

    # 分支 A：rc 命中 → 不再触碰 legacy 列
    _cursor = MagicMock()
    _cursor.fetchone.return_value = ["tok-rc"]
    _cursor.rowcount = 0
    with patch("src.kernel.token_resolver.create_provider", return_value=_prov), \
            patch("src.domains.mgmt.user.db.get_repo_credential_value", return_value="tok-rc"), \
            patch("src.kernel.session_manager.db") as _mdb, patch("src.kernel.session_manager.db_lock"):
        _mdb.cursor.return_value = _cursor
        bth._verify_single_repo_token("gitee:acme/web", "gitee")
    sql = " ".join(str(c.args[0]) for c in _cursor.execute.call_args_list)
    assert "bot_gitee_token" not in sql, "rc 命中后不应回落 legacy 列（F4 rc 优先）"

    # 分支 B：rc 空 → legacy 列回落（gitcode/github 存量行形态）
    _cursor2 = MagicMock()
    _cursor2.fetchone.return_value = ["tok"]
    _cursor2.rowcount = 0
    with patch("src.kernel.token_resolver.create_provider", return_value=_prov), \
            patch("src.domains.mgmt.user.db.get_repo_credential_value", return_value=""), \
            patch("src.kernel.session_manager.db") as _mdb, patch("src.kernel.session_manager.db_lock"):
        _mdb.cursor.return_value = _cursor2
        bth._verify_single_repo_token("gitee:acme/web", "gitee")
    sql2 = " ".join(str(c.args[0]) for c in _cursor2.execute.call_args_list)
    assert "bot_gitee_token" in sql2, "rc 空时应回落 legacy 列（历史回落保留）"


def test_s63_settings_repo_config_columns(gitee_card):
    """get_gitcode_repo_config：gitee 仓 → bot_gitee_token 列。"""
    import inspect
    from src.domains.mgmt.routes import settings as st
    src = inspect.getsource(st.get_gitcode_repo_config)
    assert "token_storage_field" in src  # 列名卡派生替代字面量三元


def test_s63_whitelist_row_insert_no_column_lookup():
    """行式化（2026-11-12）：白名单 INSERT 直写 (provider, username) 行，
    零列名映射/零卡读取（列驱动回潮守卫）。"""
    from src.domains.mgmt.admin.db import add_login_whitelist
    _cursor = MagicMock()
    _cursor.rowcount = 1
    with patch("src.domains.mgmt.admin.db.db") as _mdb, patch("src.domains.mgmt.admin.db.db_lock"):
        _mdb.cursor.return_value = _cursor
        _mdb.commit.return_value = None
        ok = add_login_whitelist("gitee", "someone", "r")
    assert ok is True
    sql = str(_cursor.execute.call_args.args[0])
    assert "INSERT OR IGNORE INTO login_whitelist (provider, username, remark)" in sql
    assert "username" in sql and "gitee_username" not in sql, "列名映射回潮"
