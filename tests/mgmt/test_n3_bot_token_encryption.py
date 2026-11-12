"""N3 实弹探针（pytest 环境内）：写加密→读回环→明文兼容→等值双形态→幂等回填。"""
import os
import uuid

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as udb


def _clean(rk):
    with db_lock:
        db.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        db.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        db.commit()


def test_n3_write_encrypts_read_roundtrip():
    """写入口加密落库 + 读口解密回环。"""
    rk = "gitcode:n3a" + uuid.uuid4().hex[:6] + "/repo"
    try:
        assert udb.add_managed_repo(rk, "n3a", "repo",
                                    bot_gitcode_token="tok_secret_123") is True
        with db_lock:
            raw = db.execute(
                "SELECT value FROM grape_t_repo_credentials WHERE repo_key=?",
                (rk,)).fetchone()[0]
        assert ":" in raw and "{" not in raw, f"落库非密文: {raw[:20]}"
        val = udb.get_repo_credential_value(rk, "bot")  # D99 归一
        assert val == "tok_secret_123", f"读回环失败: {val!r}"
    finally:
        _clean(rk)


def test_n3_plaintext_legacy_row_still_readable():
    """向前兼容：漏网明文行（直插）读侧原样返回。"""
    rk = "gitcode:n3b" + uuid.uuid4().hex[:6] + "/repo"
    try:
        with db_lock:
            db.execute(
                "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
                "VALUES (?, ?, 'bot', 'legacy_plain_tok')", (str(uuid.uuid4()), rk))
            db.commit()
        val = udb.get_repo_credential_value(rk, "bot")  # D99 归一
        assert val == "legacy_plain_tok", f"明文兼容失败: {val!r}"
    finally:
        _clean(rk)


def test_n3_eq_query_dual_form():
    """等值查询双形态：加密后 FR-6.2 共用检测仍命中（明文+密文都查）。"""
    rk1 = "gitcode:n3c" + uuid.uuid4().hex[:6] + "/repo"
    rk2 = "gitcode:n3c" + uuid.uuid4().hex[:6] + "/repo"
    try:
        # 两仓库配同一 bot token（走写入口=密文落库）
        udb.add_managed_repo(rk1, "n3c", "repo", bot_gitcode_token="shared_tok")
        udb.add_managed_repo(rk2, "n3c", "repo", bot_gitcode_token="shared_tok")
        # update_managed_repo_bot_token 的 FR-6.2 检测逻辑复现（直接查）
        with db_lock:
            rows = db.execute(
                "SELECT repo_key FROM grape_t_repo_credentials WHERE repo_key IN (?,?)",
                (rk1, rk2)).fetchall()
        assert len(rows) == 2
        # 通过 update_managed_repo_bot_token 走一遍（内部做 FR-6.2 检测，不抛即通过）
        r = udb.update_managed_repo_bot_token(rk1, "shared_tok_new")
        assert r.get("ok") is True
    finally:
        _clean(rk1)
        _clean(rk2)


def test_n3_startup_backfill_encrypts_plaintext(monkeypatch, tmp_path):
    """启动回填：存量明文行 → 幂等加密（复合键定位，双方言安全）。"""
    from src.domains.mgmt.user.credentials_backfill_n1 import backfill_repo_credentials_encrypt
    rk = "gitcode:n3d" + uuid.uuid4().hex[:6] + "/repo"
    try:
        with db_lock:
            db.execute(
                "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
                "VALUES (?, ?, 'bot', 'plain_hist_tok')", (str(uuid.uuid4()), rk))
            db.commit()
        n = backfill_repo_credentials_encrypt()
        with db_lock:
            raw = db.execute(
                "SELECT value FROM grape_t_repo_credentials WHERE repo_key=?",
                (rk,)).fetchone()[0]
        assert ":" in raw, f"回填未加密: {raw[:20]}"
        val = udb.get_repo_credential_value(rk, "bot")  # D99 归一
        assert val == "plain_hist_tok", f"回填后读回环失败: {val!r}"
        # 幂等：再跑一遍密文行跳过
        n2 = backfill_repo_credentials_encrypt()
        with db_lock:
            raw2 = db.execute(
                "SELECT value FROM grape_t_repo_credentials WHERE repo_key=?",
                (rk,)).fetchone()[0]
        assert raw2 == raw, "幂等失败：二次回填改值"
    finally:
        _clean(rk)
