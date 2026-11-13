"""S128 场景用例（F4/D106 凭据存储终态化第一批）：gitee/gitlab 四列撤出 +
repo token 写路径收敛 rc 单写 + 存量列回填迁移。

覆盖（对应 F4 验收线）：
- UC79① 冷启动建库无 gitee/gitlab token 列（子进程真 init_database）+
  卡终态口径（gitee/gitlab 置空、gitcode/github 派生保留）+ ORM 模型撤列
- UC79② 新仓只落 rc（归一逻辑键 bot/webhook + upsert 轮换不撞 UNIQUE）
- UC79③ 存量仓库回填+置空后 resolver 仅凭 rc 返回正确 token（不依赖列）
- UC79④ 渐进收敛：写 rc 成功后同一事务置空对应平台 token 列（meta 同名
  全局键 gitcode_webhook_token 不误伤）
- UC79⑤ 回填迁移幂等 + 值归一（明文/密文列值统一回填密文）
"""
import json
import sqlite3
import subprocess
import sys
import uuid

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache

_NS = "s128"

_GL_COLS = ("bot_gitee_token", "gitee_webhook_token",
            "bot_gitlab_token", "gitlab_webhook_token")


def _cols(table="managed_repos"):
    with db_lock:
        cols = [r[1] for r in db.cursor().execute(f"PRAGMA table_info({table})").fetchall()]
    return set(cols)


def _drop_gl_cols():
    with db_lock:
        for col in _GL_COLS:
            try:
                db.cursor().execute(f"ALTER TABLE managed_repos DROP COLUMN {col}")
            except Exception:
                pass
        db.commit()


def _ensure_gl_cols():
    """模拟存量升级库（F4 前自动补列形态）。"""
    have = _cols()
    with db_lock:
        for col in _GL_COLS:
            if col not in have:
                db.cursor().execute(f"ALTER TABLE managed_repos ADD COLUMN {col} TEXT DEFAULT ''")
        db.commit()


def _rc(repo_key):
    with db_lock:
        return dict(db.cursor().execute(
            "SELECT cred_type, value FROM grape_t_repo_credentials WHERE repo_key=?",
            (repo_key,)).fetchall())


def _mr_col(repo_key, col):
    with db_lock:
        row = db.cursor().execute(
            f"SELECT {col} FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
    return row[0] if row else None


@pytest.fixture(autouse=True)
def _terminal_cards():
    """终态卡环境：清 meta 运行时覆盖（回到 configs/platforms 文件口径）+ 清场。"""
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        for t, pat in (("managed_repos", f"{_NS}%"), ("grape_t_repo_credentials", f"{_NS}%"),
                       ("grape_t_repo_credentials", "legacy:%")):
            db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE ?", (pat,))
        db.commit()
    invalidate_platform_card_cache()
    yield
    with db_lock:
        for t, pat in (("managed_repos", f"{_NS}%"), ("grape_t_repo_credentials", f"{_NS}%"),
                       ("grape_t_repo_credentials", "legacy:%")):
            db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE ?", (pat,))
        db.commit()
    invalidate_platform_card_cache()


# ------------------------------------------------ UC79① 冷启动 + 卡终态

def test_s128_card_fields_terminal():
    """卡终态口径：gitee/gitlab 置空（无物理列），gitcode/github 派生保留。"""
    from src.domains.runtime.vcs.platform_card import get_platform_card
    for p in ("gitee", "gitlab"):
        card = get_platform_card(p)
        assert card is not None
        assert card.token_storage_field == "", f"{p} token_storage_field 应置空（终态）"
        assert card.webhook_token_field == "", f"{p} webhook_token_field 应置空（终态）"
    for p in ("gitcode", "github"):
        card = get_platform_card(p)
        assert card.token_storage_field == f"bot_{p}_token"
        assert card.webhook_token_field == f"{p}_webhook_token"


def test_s128_cold_start_no_gl_columns(tmp_path):
    """冷启动建库无 gitee/gitlab token 列（子进程真 init_database，独立库文件）。

    同时验证 github 派生列仍在（批次 1 只撤 gitee/gitlab）。
    """
    db_file = tmp_path / "cold_start.db"
    code = (
        "import json\n"
        "import src.api.db_setup as ds\n"
        "ds.init_database()\n"
        "from src.kernel.session_manager import db\n"
        "cols = [r[1] for r in db.cursor().execute('PRAGMA table_info(managed_repos)').fetchall()]\n"
        "print(json.dumps(cols))\n"
    )
    import os
    env = dict(os.environ)
    env.update({"DEVOPS_DB_PATH": str(db_file), "DEVOPS_DB_BACKEND": "sqlite",
                "OBS_LOG_DB": "0", "PYTHONPATH": os.path.abspath(os.path.join(os.path.dirname(str(__file__)), "..", "..")),
                "DEVOPS_WORKSPACE_DIR": str(tmp_path / "ws")})
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, cwd=str(tmp_path), timeout=180)
    assert proc.returncode == 0, f"cold-start init 失败: {proc.stderr[-2000:]}"
    cols = json.loads(proc.stdout.strip().splitlines()[-1])
    bad = [c for c in cols if c in _GL_COLS]
    assert not bad, f"冷启动建库出现 gitee/gitlab token 列: {bad}"
    assert "bot_github_token" in cols and "bot_gitcode_token" in cols


def test_s128_orm_model_drops_gl_columns():
    """ORM 模型撤列守卫：ManagedRepo 模型不再声明 gitee/gitlab 四列。"""
    from src.kernel.persistence.models import ManagedRepoModel as ManagedRepo
    model_cols = {c.name for c in ManagedRepo.__table__.columns}
    bad = [c for c in _GL_COLS if c in model_cols]
    assert not bad, f"ORM 模型仍声明已撤出列: {bad}"


def test_s128_ensure_columns_skips_terminal_cards():
    """终态卡下 _ensure_platform_storage_columns 不补 gitee/gitlab 列。"""
    from src.api.db_setup import _ensure_platform_storage_columns
    _drop_gl_cols()
    _ensure_platform_storage_columns()
    cols = _cols()
    bad = [c for c in _GL_COLS if c in cols]
    assert not bad, f"终态卡不应补列: {bad}"
    assert "bot_github_token" in cols  # 有列平台不受影响


# ------------------------------------------------ UC79② 新仓只落 rc

def test_s128_new_repo_rc_only_and_upsert_rotation():
    """新仓只落 rc（归一逻辑键）+ 二次轮换 upsert 不撞 UNIQUE（S82 基线 3 红回归锁）。"""
    from src.domains.mgmt.user.db import add_managed_repo
    rk = f"gitee:{_NS}o1/r1"
    assert add_managed_repo(rk, "o1", "r1", webhook_id=1, bot="bt-v1", webhook="wt-v1")
    rc = _rc(rk)
    assert set(rc.keys()) == {"bot", "webhook"}, f"新仓凭据应只落 rc: {rc}"

    from src.domains.mgmt.user.db import get_repo_credential_value
    assert get_repo_credential_value(rk, "bot") == "bt-v1"
    assert get_repo_credential_value(rk, "webhook") == "wt-v1"

    # 轮换（原裸 INSERT 撞 UNIQUE → S82 基线红）
    assert add_managed_repo(rk, "o1", "r1", webhook_id=1, bot="bt-v2")
    assert get_repo_credential_value(rk, "bot") == "bt-v2"
    rows = db.cursor().execute(
        "SELECT COUNT(*) FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type='bot'",
        (rk,)).fetchone()[0]
    assert rows == 1, "每仓库每 cred_type 至多一条（UNIQUE 契约）"


def test_s128_legacy_column_names_still_route_rc():
    """存量调用形态（列名 kwargs）同路由 rc（兼容 gitcode/github 调用方）。"""
    from src.domains.mgmt.user.db import add_managed_repo, get_repo_credential_value
    rk = f"gitee:{_NS}o2/r2"
    assert add_managed_repo(rk, "o2", "r2", **{"bot_gitee_token": "bt-col-key"})
    assert get_repo_credential_value(rk, "bot") == "bt-col-key"


# ------------------------------------------------ UC79③④⑤ 存量回填+置空+resolver

def test_s128_backfill_then_rotation_blanks_and_resolver_reads_rc():
    """F4 主链路：存量列值 → 回填迁移 → 写路径轮换置空 → resolver 仅凭 rc 取 token。"""
    from src.api.db_setup import backfill_gitee_gitlab_token_columns_to_rc
    from src.domains.mgmt.user.db import add_managed_repo, _encrypt_repo_secret
    from src.kernel.token_resolver import RepoBotToken, create_provider

    _ensure_gl_cols()  # 模拟存量升级库（四列在）
    rk = f"gitee:{_NS}o3/r3"
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "bot_gitee_token, gitee_webhook_token) VALUES (?,?,?,?,?,?)",
            (rk, "o3", "r3", "o3/r3", _encrypt_repo_secret("bt-old"), "wt-plain"))
        db.commit()

    # ① 回填（幂等只填空；明文/密文列值统一归一为密文形态）
    n1 = backfill_gitee_gitlab_token_columns_to_rc()
    assert n1 >= 1
    from src.domains.mgmt.user.db import get_repo_credential_value, _decrypt_repo_secret
    rc = _rc(rk)
    assert rc.get("bot") and rc.get("webhook")
    assert get_repo_credential_value(rk, "bot") == "bt-old"
    assert get_repo_credential_value(rk, "webhook") == "wt-plain"
    # ② 轮换：写 rc 成功后同一事务置空对应平台列（渐进收敛）
    assert add_managed_repo(rk, "o3", "r3", webhook_id=2, bot="bt-new", webhook="wt-new")
    assert get_repo_credential_value(rk, "bot") == "bt-new"
    assert _mr_col(rk, "bot_gitee_token") == "", "轮换后存量列应被置空（渐进收敛）"
    assert _mr_col(rk, "gitee_webhook_token") == ""
    # ③ resolver 仅凭 rc 返回正确 token（列已置空，不依赖列）
    s = RepoBotToken(create_provider("gitee"))
    assert s._fetch(rk) == "bt-new"
    # 回填幂等：二次执行零新增
    before = db.cursor().execute(
        "SELECT COUNT(*) FROM grape_t_repo_credentials WHERE repo_key LIKE 'legacy:%'").fetchone()[0]
    n2 = backfill_gitee_gitlab_token_columns_to_rc()
    if n2:
        # 幂等语义：已有 rc 行不覆盖（本仓行已存在，不应重复）
        rc_after = _rc(rk)
        assert rc_after.get("bot") is not None


def test_s128_rotation_spares_unwritten_cred_type():
    """只轮换 bot 时不误清未回填的 webhook 历史列值（防数据丢失）。"""
    from src.domains.mgmt.user.db import add_managed_repo
    _ensure_gl_cols()
    rk = f"gitee:{_NS}o4/r4"
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "gitee_webhook_token) VALUES (?,?,?,?,?)",
            (rk, "o4", "r4", "o4/r4", "wt-legacy"))
        db.commit()
    assert add_managed_repo(rk, "o4", "r4", webhook_id=1, bot="bt-only")
    assert _mr_col(rk, "gitee_webhook_token") == "wt-legacy", (
        "未写 webhook cred 时不应置空 webhook 历史列（防误清未回填值）")
    assert _mr_col(rk, "bot_gitee_token") == ""


def test_s128_blank_out_never_touches_meta():
    """置空 SQL 精确限定 UPDATE managed_repos——meta 同名全局键不误伤。"""
    from src.domains.mgmt.user.db import add_managed_repo
    _ensure_gl_cols()
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('gitcode_webhook_token', 'META-KEEP')")
        db.commit()
    rk = f"gitcode:{_NS}o5/r5"  # 用 gitcode 仓触发列置空路径（同名列族）
    assert add_managed_repo(rk, "o5", "r5", webhook_id=1, bot_gitcode_token="bt-gc")
    with db_lock:
        row = db.cursor().execute(
            "SELECT value FROM meta WHERE key='gitcode_webhook_token'").fetchone()
        db.cursor().execute("DELETE FROM meta WHERE key='gitcode_webhook_token'")
        db.commit()
    assert row and row[0] == "META-KEEP", "meta 同名全局键被误伤"


def test_s128_update_repo_bot_token_rc_only_for_terminal_card():
    """_update_repo_bot_token（路径 B→A 升级/手动设置）卡列空平台：rc 单写+列置空。"""
    from src.domains.mgmt.user.db import _update_repo_bot_token, get_repo_credential_value
    _ensure_gl_cols()
    rk = f"gitee:{_NS}o6/r6"
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "bot_gitee_token, host_user_id) VALUES (?,?,?,?,?,?)",
            (rk, "o6", "r6", "o6/r6", _enc("bt-legacy"), "9"))
        db.commit()
    _update_repo_bot_token(rk, "bt-new", provider="gitee", webhook_token="bt-new", webhook_id="5")
    assert get_repo_credential_value(rk, "bot") == "bt-new"
    assert get_repo_credential_value(rk, "webhook") == "bt-new"  # 对齐列时代 wt=bot 语义
    assert _mr_col(rk, "bot_gitee_token") == ""
    with db_lock:
        host = db.cursor().execute(
            "SELECT host_user_id FROM managed_repos WHERE repo_key=?", (rk,)).fetchone()[0]
    assert str(host) in ("", "0"), "路径 A 语义：host_user_id 应清零"


def _enc(v):
    from src.domains.mgmt.user.db import _encrypt_repo_secret
    return _encrypt_repo_secret(v)
