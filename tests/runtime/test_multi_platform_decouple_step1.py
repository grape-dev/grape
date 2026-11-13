"""多平台+凭据解耦（multi-platform-decouple）第 1 步守卫用例。

对应需求 docs/weekly-09/multi-platform-decouple-requirements.md：
- S2/S3：OAuth 读取与 PAT 不刷新语义的回归锁（锁现状，D3 重构后必须继续绿）
- S8：git 传输层鉴权 scheme 与 askpass 链回归锁（D4 重构前锁现状）
- S22：K4 双 id 错位数据检测——只读检测+报告，零数据改动（删除兜底的前置）
- S5：结构守卫——K1/K4 符号全仓零残留（D3 完成后转绿；xfail strict 强制翻绿时摘标记）
"""
import base64
import json
import os
import time
import uuid

import pytest

from src.kernel.session_manager import db, db_lock


def _mk_account(prefix="mpd"):
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
    acc = create_grape_account(username=uname, email="", password="pw")
    assert acc, "create_grape_account failed"
    return acc


def _mk_identity(uid, provider, external_id, access_token, refresh_token=""):
    from src.domains.mgmt.auth.grape_account_db import create_external_identity
    assert create_external_identity(uid, provider, str(external_id),
                                     external_login=f"l{external_id}",
                                     access_token=access_token,
                                     refresh_token=refresh_token)


def _set_oauth_credential(uid, provider, cfg: dict):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (uid, f"{provider}_oauth", json.dumps(cfg)))
        db.commit()


def _cleanup_credential(uid, provider):
    with db_lock:
        db.cursor().execute("DELETE FROM user_credentials WHERE user_id=? AND provider=?",
                            (uid, f"{provider}_oauth"))
        db.commit()


# ---------------------------------------------------------------- S2：OAuth token 只从 external_identities 读（D32 锁）

def test_s2_oauth_token_read_from_identity_only():
    from src.api.deps import get_user_token
    acc = _mk_account()
    _mk_identity(acc["id"], "gitcode", 90001, "at_s2_read")
    assert get_user_token(acc["username"]) == "at_s2_read"
    _cleanup_credential(acc["id"], "gitcode")


def test_s2_github_token_read_from_identity_only():
    from src.api.deps import get_user_github_token
    acc = _mk_account()
    _mk_identity(acc["id"], "github", 90002, "gh_s2_read")
    assert get_user_github_token(acc["username"]) == "gh_s2_read"


# ---------------------------------------------------------------- S3：PAT/无 refresh_token 凭据不触发刷新

def test_s3_no_refresh_token_skips_refresh_even_if_expired():
    """R2/Issue #372 语义：无 refresh_token 的凭据（PAT/导入）不会过期，直接返回不刷新。

    token_expires_at 已过 10 分钟，但 refresh_token 为空 → 返回原 token、零网络调用。
    D3 重构后本用例必须继续绿（PAT 语义是解耦目标的一部分）。
    """
    from src.api.deps import get_user_token_with_refresh
    acc = _mk_account()
    _mk_identity(acc["id"], "gitcode", 90003, "at_s3_pat", refresh_token="")
    _set_oauth_credential(acc["id"], "gitcode", {
        "gitcode_id": "90003", "access_token": "at_s3_pat",
        "refresh_token": "", "token_expires_at": int(time.time()) - 600,
    })
    # 任何网络刷新尝试都会因 requests 调用真实端点而失败/超时；
    # 断言函数在无 refresh_token 分支短路：原样返回 token
    assert get_user_token_with_refresh(acc["username"], user_id=acc["id"]) == "at_s3_pat"
    _cleanup_credential(acc["id"], "gitcode")


# ---------------------------------------------------------------- S8：git 传输层鉴权 scheme 回归锁

def test_s8_basic_auth_scheme_is_x_access_token():
    """现状：clone 鉴权 Basic x-access-token:<token>（gitcode/github 语义），D4 改配置驱动后必须保持。"""
    from src.domains.runtime.engine.repogit import _basic_auth
    tok = "tok_s8"
    assert _basic_auth(tok) == base64.b64encode(f"x-access-token:{tok}".encode()).decode()


def test_s8_clone_url_gitcode_and_github():
    from src.domains.runtime.engine.repogit import clone_url_for
    assert clone_url_for("gitcode:o/r") == "https://gitcode.com/o/r.git"
    assert clone_url_for("github:o/r") == "https://github.com/o/r.git"


def test_s8_askpass_token_env_chain_order():
    """现状 fallback 链：github: BOT_GITHUB_TOKEN 优先；其余 BOT_GITCODE_TOKEN 优先。"""
    from src.domains.runtime.engine.repogit import _write_askpass
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p_gh = _write_askpass(td, "github:o/r")
        s_gh = open(p_gh).read()
        assert "BOT_GITHUB_TOKEN" in s_gh and "USER_GITHUB_TOKEN" in s_gh
        assert s_gh.index("BOT_GITHUB_TOKEN") < s_gh.index("BOT_TOKEN")
        p_gc = _write_askpass(td, "gitcode:o/r")
        s_gc = open(p_gc).read()
        assert s_gc.index("BOT_GITCODE_TOKEN") < s_gc.index("BOT_TOKEN")
        # askpass 脚本不得包含明文 token（只引用环境变量）
        assert "echo" in s_gc and "x-access-token" not in s_gc


# ---------------------------------------------------------------- S22：K4 双 id 错位检测（只读报告）

def _detect_k4_mismatches() -> list:
    """K4 错位检测（只读）：gitcode_oauth 凭据行的 user_id 与该 gitcode_id 当前
    归属账号（external_identities status IN (1,4)）不一致的清单。

    D3 删除 _get_gitcode_credential_by_gitcode_id 兜底前，用本函数对生产库出报告；
    本实现只执行 SELECT，零写操作（报告-不修数据，零危害）。
    """
    mismatches = []
    with db_lock:
        rows = db.cursor().execute(
            "SELECT user_id, config_json FROM user_credentials "
            "WHERE provider='gitcode_oauth'").fetchall()
    for user_id, raw in rows:
        try:
            cfg = json.loads(raw or "{}")
        except Exception:
            continue
        gid = str(cfg.get("gitcode_id") or "")
        if not gid or not cfg.get("access_token"):
            continue
        with db_lock:
            owner = db.cursor().execute(
                "SELECT grape_user_id FROM external_identities "
                "WHERE provider='gitcode' AND external_id=? AND status IN (1,4) LIMIT 1",
                (gid,)).fetchone()
        if owner and owner[0] != user_id:
            mismatches.append({"credential_user_id": user_id,
                               "current_owner_id": owner[0],
                               "gitcode_id": gid})
    return mismatches


def test_s22_k4_mismatch_detect_report_only():
    acc_new = _mk_account()
    _mk_identity(acc_new["id"], "gitcode", 90022, "at_new")
    # 旧世代凭据：gitcode_id 相同但挂在另一个 user_id 下（历史迁移错位）
    # D105：主键已 UUID 字符串化——旧世代错位账号不能再整数拼接，用独立字符串 id 构造
    legacy_uid = f"legacy-{acc_new['id']}"
    _set_oauth_credential(legacy_uid, "gitcode", {
        "gitcode_id": "90022", "access_token": "at_legacy",
        "refresh_token": "rt_legacy",
    })
    try:
        before = db.cursor().execute(
            "SELECT COUNT(*) FROM user_credentials WHERE provider='gitcode_oauth'").fetchone()[0]
        report = _detect_k4_mismatches()
        assert any(r["gitcode_id"] == "90022" and r["credential_user_id"] == legacy_uid
                   and r["current_owner_id"] == acc_new["id"] for r in report), report
        # 只读纪律：检测后数据零改动
        after = db.cursor().execute(
            "SELECT COUNT(*) FROM user_credentials WHERE provider='gitcode_oauth'").fetchone()[0]
        assert before == after
        row = db.cursor().execute(
            "SELECT config_json FROM user_credentials WHERE user_id=? AND provider='gitcode_oauth'",
            (legacy_uid,)).fetchone()
        assert row and json.loads(row[0])["access_token"] == "at_legacy"
    finally:
        _cleanup_credential(legacy_uid, "gitcode")


def test_s22_k4_no_mismatch_when_owner_matches():
    acc = _mk_account()
    _mk_identity(acc["id"], "gitcode", 90023, "at_ok")
    _set_oauth_credential(acc["id"], "gitcode", {
        "gitcode_id": "90023", "access_token": "at_ok", "refresh_token": ""})
    try:
        report = [r for r in _detect_k4_mismatches() if r["gitcode_id"] == "90023"]
        assert report == []
    finally:
        _cleanup_credential(acc["id"], "gitcode")


# ---------------------------------------------------------------- S5：结构守卫（D3 完成后翻绿）

def test_s5_structure_guard_k1_k4_symbols_removed():
    """S5：K1 分叉包装器与 K4 兜底符号已在 D3 中删除（统一为 get_user_token_for），
    本守卫防止符号回潮。

    K1 裁定（arch 基线守恒）：K1 符号 get_user_github_token_with_refresh 只允许在
    src/api/deps.py 的基线兼容定义处存活 ×1。
    - 证据：arch 基线（pristine 1938d6591）在 src/api/deps.py 定义它，且两个 arch-native
      用例直接依赖该符号——tests/runtime/test_issue_372_round8_audit.py::TestT25GithubPatGuard
      导入并调用它、tests/insight/test_probe_github_hooks_404.py::_run_probe patch 它；
      删符号会把这两处基线用例变红（破坏 arch 既有行为，非本合并目标）。
    - 我方 ours@64144d06a 已把该分叉统一进 get_user_token_for（src/api/deps.py 无该符号）；
      合并树保留基线定义 = 基线语义保留；本守卫收紧为「除基线定义处之外零出现」，
      删掉 templates.py 的 0 调用点导入后 K1 出现数 = 1。
    K4 兜底 _get_gitcode_credential_by_gitcode_id：src 内零调用方、零用例依赖，
    按我方 D3 删除并继续全局禁止（本守卫硬红）。
    """
    K1_SYM = "get_user_github_token_with_refresh"
    K1_ALLOWED = {("api/deps.py", 1)}  # (相对 src 的路径, 允许出现次数)
    hits = []
    for root, _dirs, files in os.walk(os.path.join(os.path.dirname(__file__), "..", "..", "src")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, os.path.join(os.path.dirname(__file__), "..", "..", "src")).replace(os.sep, "/")
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for sym in ("get_user_github_token_with_refresh",
                        "_get_gitcode_credential_by_gitcode_id"):
                n = text.count(sym)
                if not n:
                    continue
                if sym == K1_SYM and (rel, n) in K1_ALLOWED:
                    continue
                hits.append(f"{path}: {sym} x{n}")
    assert hits == [], "K1/K4 符号残留：\n" + "\n".join(hits)
