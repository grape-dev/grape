"""Issue #3740（先测后改）：bot 凭据回填路径 —— managed_repos 历史列 → rc（cred_type='bot'）。

覆盖：
- 丢失函数回归：``backfill_repo_credentials_from_columns()``（squash 合并丢失，3 个存量
  测试模块 import 即 ERROR）恢复后 8 个平台列均可回填。
- bot 列专用回填：dry-run 零写、apply 幂等、值归一（明文/密文统一回填为密文）、
  已有 rc 行不覆盖。
- 验证报告：回填前报缺口，回填后缺口为 0。
- 盖章：写 ``grape_t_schema_migrations`` 台账（幂等），满足回落分支下线 checklist ①。
- 轮换可见性：rc 写路径（insight upsert/clear）主动失效 kernel 解析缓存。
"""
import importlib.util
import json
import os
import uuid

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from src.kernel.session_manager import db, db_lock  # noqa: E402

_NS = "x3740"

_BOT_COLS = ("bot_gitcode_token", "bot_github_token", "bot_gitee_token", "bot_gitlab_token")
_ALL_COLS = _BOT_COLS + ("gitcode_webhook_token", "github_webhook_token",
                         "gitee_webhook_token", "gitlab_webhook_token")


def _rk(tag="", provider="gitcode"):
    return f"{provider}:{_NS}{tag}{uuid.uuid4().hex[:6]}/repo"


def _ensure_cols():
    """存量升级库形态：确保 8 个平台列物理存在（冷启动库 F4 后只留 gitcode/github）。"""
    with db_lock:
        have = {r[1] for r in db.cursor().execute("PRAGMA table_info(managed_repos)").fetchall()}
        for col in _ALL_COLS:
            if col not in have:
                db.cursor().execute(f"ALTER TABLE managed_repos ADD COLUMN {col} TEXT DEFAULT ''")
        db.commit()


def _insert_legacy(rk, **cols):
    _ensure_cols()
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES (?,?,?,?)", (rk, "o", "r", rk.split(":", 1)[-1]))
        for col, val in cols.items():
            db.cursor().execute(f"UPDATE managed_repos SET {col}=? WHERE repo_key=?", (val, rk))
        db.commit()


def _rc_rows(rk):
    from src.domains.mgmt.user.db import _decrypt_repo_secret
    with db_lock:
        rows = db.cursor().execute(
            "SELECT cred_type, value FROM grape_t_repo_credentials WHERE repo_key=?",
            (rk,)).fetchall()
    return {ct: _decrypt_repo_secret(v or "") for ct, v in rows}


def _clean():
    from src.kernel.token_resolver import invalidate_repo_bot_token_cache
    invalidate_repo_bot_token_cache()
    with db_lock:
        for table in ("managed_repos", "grape_t_repo_credentials"):
            db.cursor().execute(f"DELETE FROM {table} WHERE repo_key LIKE ?", (f"%{_NS}%",))
        db.commit()


@pytest.fixture(autouse=True)
def _isolated_rows():
    _clean()
    _ensure_cols()
    yield
    _clean()


# ────────────────────────────────────────── 1. 丢失函数回归

def test_restored_backfill_from_columns_fills_all_platforms():
    """恢复的 8 列回填：bot→cred_type='bot'，webhook→'webhook'，二次执行幂等。"""
    from src.api.db_setup import backfill_repo_credentials_from_columns

    rk = _rk("restore")
    _insert_legacy(rk, bot_gitcode_token="tok-bot", gitcode_webhook_token="tok-hook")
    assert backfill_repo_credentials_from_columns() >= 2
    assert _rc_rows(rk) == {"bot": "tok-bot", "webhook": "tok-hook"}
    assert backfill_repo_credentials_from_columns() == 0, "二次回填必须幂等（零写入）"


# ────────────────────────────────────────── 2. bot 列回填：dry-run / apply / 幂等

def test_bot_column_backfill_dry_run_writes_nothing():
    """dry-run 只报计划：rc 零写入，缺口清单可见。"""
    from src.api.db_setup import backfill_repo_bot_token_columns_to_rc

    rk = _rk("dry")
    _insert_legacy(rk, bot_gitcode_token="tok-dry")
    report = backfill_repo_bot_token_columns_to_rc(dry_run=True)
    assert report["dry_run"] is True
    assert report["filled"] == 0
    assert report["would_fill"] >= 1
    assert rk in report["missing"]
    assert _rc_rows(rk) == {}, "dry-run 不得写库"


def test_bot_column_backfill_apply_is_idempotent():
    """apply：rc bot 行落地 + 值归一（明文/密文均归一为密文）；二次 apply 零写入。"""
    from src.api.db_setup import backfill_repo_bot_token_columns_to_rc
    from src.domains.mgmt.user.db import _encrypt_repo_secret

    plain_rk = _rk("plain")
    cipher_rk = _rk("cipher", provider="github")
    _insert_legacy(plain_rk, bot_gitcode_token="tok-plain")
    _insert_legacy(cipher_rk, bot_github_token=_encrypt_repo_secret("tok-cipher"))

    first = backfill_repo_bot_token_columns_to_rc()
    assert first["filled"] >= 2
    assert first["dry_run"] is False
    assert _rc_rows(plain_rk) == {"bot": "tok-plain"}
    assert _rc_rows(cipher_rk) == {"bot": "tok-cipher"}
    # 值归一：rc 落库形态是密文（读侧解密才能还原）
    with db_lock:
        raw = db.cursor().execute(
            "SELECT value FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type='bot'",
            (plain_rk,)).fetchone()[0]
    assert raw != "tok-plain" and raw, "回填值应为密文形态（与写路径同构）"

    second = backfill_repo_bot_token_columns_to_rc()
    assert second["filled"] == 0, "幂等：二次回填零写入"


def test_bot_column_backfill_never_overwrites_existing_rc():
    """已有 rc bot 行是权威（可能已轮换）：回填只填空，不覆盖。"""
    from src.api.db_setup import backfill_repo_bot_token_columns_to_rc
    from src.domains.insight.public import upsert_repo_credential_value

    rk = _rk("keep")
    _insert_legacy(rk, bot_gitcode_token="tok-stale-column")
    upsert_repo_credential_value(rk, "bot", "tok-rotated-rc", "2026-11-22 10:00:00")
    backfill_repo_bot_token_columns_to_rc()
    assert _rc_rows(rk) == {"bot": "tok-rotated-rc"}


# ────────────────────────────────────────── 3. 验证报告 + 盖章

def test_verify_backfill_reports_gaps_then_zero():
    """verify：回填前缺口非空、覆盖率 <1；回填后缺口为 0（下线 checklist 前置）。"""
    from src.api.db_setup import (backfill_repo_bot_token_columns_to_rc,
                                  verify_repo_bot_credential_backfill)

    rk = _rk("verify")
    _insert_legacy(rk, bot_gitcode_token="tok-verify")

    before = verify_repo_bot_credential_backfill()
    assert rk in before["missing"], before
    assert before["coverage"] < 1.0

    backfill_repo_bot_token_columns_to_rc()
    after = verify_repo_bot_credential_backfill()
    assert rk not in after["missing"], after
    assert after["coverage"] == 1.0
    assert after["rc_bot_rows"] >= 1


def test_stamp_backfill_writes_migration_ledger():
    """盖章：version/name 落 grape_t_schema_migrations（幂等），供下线 checklist 核对。"""
    from src.api.db_setup import (REPO_BOT_BACKFILL_NAME, REPO_BOT_BACKFILL_VERSION,
                                  stamp_repo_bot_credential_backfill)

    first = stamp_repo_bot_credential_backfill()
    assert first["version"] == REPO_BOT_BACKFILL_VERSION
    with db_lock:
        row = db.cursor().execute(
            "SELECT name, kind FROM grape_t_schema_migrations WHERE version=?",
            (REPO_BOT_BACKFILL_VERSION,)).fetchone()
        if row:  # 测试库可能与其他用例共享台账，清理后再验幂等
            db.cursor().execute("DELETE FROM grape_t_schema_migrations WHERE version=?",
                                (REPO_BOT_BACKFILL_VERSION,))
            db.commit()
    stamp_repo_bot_credential_backfill()
    second = stamp_repo_bot_credential_backfill()
    assert second["already"] is True, "二次盖章应识别既有台账行"
    with db_lock:
        rows = db.cursor().execute(
            "SELECT name FROM grape_t_schema_migrations WHERE version=?",
            (REPO_BOT_BACKFILL_VERSION,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == REPO_BOT_BACKFILL_NAME


# ────────────────────────────────────────── 4. 轮换可见性（rc 写路径失效缓存）

def test_rc_upsert_invalidates_fetch_cache():
    """rc 写路径（upsert）后，解析缓存立即失效——轮换不必等 TTL。"""
    from src.domains.insight.public import upsert_repo_credential_value
    from src.kernel.token_resolver import RepoBotToken, create_provider_from_repo_key

    rk = _rk("rotate")
    _insert_legacy(rk, bot_gitcode_token="tok-legacy-era")
    strategy = RepoBotToken(create_provider_from_repo_key(rk))
    assert strategy.get_token(rk) == "tok-legacy-era"

    upsert_repo_credential_value(rk, "bot", "tok-rc-new", "2026-11-22 10:00:00")
    assert strategy.get_token(rk) == "tok-rc-new", "回填/轮换写入后应立即可见（缓存失效）"


# ────────────────────────────────────────── 5. CLI 可执行（默认 dry-run）

def _load_cli():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "backfill_repo_bot_credentials.py")
    spec = importlib.util.spec_from_file_location("backfill_repo_bot_credentials_3740", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_dry_run_by_default(capsys):
    """脚本默认只报计划（--apply 才写库）：sre 执行前必先 dry-run。"""
    cli = _load_cli()
    rk = _rk("cli")
    _insert_legacy(rk, bot_gitcode_token="tok-cli")
    code = cli.main(["--dry-run"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):]) if "{" in out else {}
    assert code == 0
    assert payload.get("mode") == "dry-run"
    assert payload["backfill"]["dry_run"] is True
    assert payload["backfill"]["filled"] == 0
    assert rk in payload["backfill"]["missing"]
    assert _rc_rows(rk) == {}, "默认路径不得写库"

# ────────────────────────────────────────── 6. 探列失败不得伪装成「无缺口」

_PROBE_FAIL = "managed_repos column probe failed: boom"


def _drop_stamp_row():
    from src.api.db_setup import REPO_BOT_BACKFILL_VERSION
    with db_lock:
        db.cursor().execute("DELETE FROM grape_t_schema_migrations WHERE version=?",
                            (REPO_BOT_BACKFILL_VERSION,))
        db.commit()


def test_verify_probe_failure_is_not_fake_pass(monkeypatch):
    """探列失败时 verify 必须报 probe_ok=False、coverage=0，不得报「1.0/无缺口」。"""
    import src.api.db_setup as dbs

    monkeypatch.setattr(dbs, "_managed_repos_columns_probe",
                        lambda c: (set(), _PROBE_FAIL))
    report = dbs.verify_repo_bot_credential_backfill()
    assert report["probe_ok"] is False
    assert "boom" in report["error"]
    assert report["coverage"] == 0.0
    assert report["missing"] == []


def test_backfill_probe_failure_is_reported(monkeypatch):
    """探列失败时回填报告必须 probe_ok=False（零写入不能被当成成功）。"""
    import src.api.db_setup as dbs

    monkeypatch.setattr(dbs, "_managed_repos_columns_probe",
                        lambda c: (set(), _PROBE_FAIL))
    report = dbs.backfill_repo_bot_token_columns_to_rc(dry_run=True)
    assert report["probe_ok"] is False
    assert report["filled"] == 0
    assert "boom" in report["error"]


def test_cli_stamp_refuses_when_column_probe_fails(monkeypatch, capsys):
    """探列失败时 --verify --stamp 必须 exit 2 且不写台账（零写入不得盖章）。"""
    import src.api.db_setup as dbs

    _drop_stamp_row()
    monkeypatch.setattr(dbs, "_managed_repos_columns_probe",
                        lambda c: (set(), _PROBE_FAIL))
    cli = _load_cli()
    code = cli.main(["--verify", "--stamp"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):]) if "{" in out else {}

    assert code == 2
    assert payload["verify"]["probe_ok"] is False
    assert payload["stamp"]["skipped"] is True
    with db_lock:
        row = db.cursor().execute(
            "SELECT 1 FROM grape_t_schema_migrations WHERE version=?",
            (dbs.REPO_BOT_BACKFILL_VERSION,)).fetchone()
    assert row is None, "探列失败不得盖章"


def test_cli_apply_probe_failure_exits_nonzero(monkeypatch, capsys):
    """探列失败时 --apply 必须 exit 2（不能把零写入当成功）。"""
    import src.api.db_setup as dbs

    monkeypatch.setattr(dbs, "_managed_repos_columns_probe",
                        lambda c: (set(), _PROBE_FAIL))
    cli = _load_cli()
    code = cli.main(["--apply"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):]) if "{" in out else {}
    assert code == 2
    assert payload["backfill"]["probe_ok"] is False
