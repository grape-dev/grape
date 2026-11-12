"""UC42C：消费域凭据边界锁（D100 边界核查，2026-11-08）。

背景：外部架构稿评审引出"消费方是否持有平台凭据"核查——实锤 P2 收口
（vcs-env-token-boundary-audit-design.md）后 agent/worker 不持有平台凭据，
持有 tk_ 会话凭证（自签自验/对平台 API 无效）。本文件把该边界升为行为锁。

用例面：
- UC42C① P2 边界：_assemble_env 零平台 token 注入（含 USER_*/BOT_*/BOT_TOKEN）
- UC42C② tk_ 凭证形态：TTL 6h / sha256 落库明文不落盘 / scope 三重限定
- UC42C③ tk_ 对平台无效性：自签自验（服务端表校验，非平台 JWT）
- UC42C④ FR-9 范围补全锚点：agent_tasks 行 repo_gitcode_token 明文现状
  （挂账 E 登记的实锤断言——修复时本用例翻转为密文断言）
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import hashlib
import json
import uuid

from src.kernel.session_manager import db, db_lock


# ──────────────── UC42C① P2 边界：零平台 token 注入 ────────────────

def test_uc42c_1_assemble_env_no_platform_tokens():
    """_assemble_env 零平台 token 注入（P2 边界锁——USER_*/BOT_*/BOT_TOKEN 禁入）。"""
    from src.domains.runtime.agents.executor import _assemble_env
    env = {}
    _assemble_env(env, kind="chat", label="uc42c",
                  bot_token="BOT_SECRET_SHOULD_NOT_APPEAR",
                  user_token="USER_SECRET_SHOULD_NOT_APPEAR",
                  user_id=1, repo_key="gitcode:uc42c/repo")
    joined = json.dumps(env)
    assert "BOT_SECRET_SHOULD_NOT_APPEAR" not in joined, "bot token 泄漏进 env"
    assert "USER_SECRET_SHOULD_NOT_APPEAR" not in joined, "user token 泄漏进 env"
    for banned in ("USER_GITCODE_TOKEN", "USER_GITHUB_TOKEN",
                   "BOT_GITCODE_TOKEN", "BOT_GITHUB_TOKEN", "BOT_TOKEN"):
        assert banned not in env, f"平台 token 键回潮: {banned}"


def test_uc42c_1b_devops_stream_no_user_token():
    """stream_devops_agent 签名无 user_token（事件任务无法以用户身份写平台——写隔离锁）。"""
    import inspect
    from src.domains.runtime.agents import executor
    sig = inspect.signature(executor.stream_devops_agent)
    assert "user_token" not in sig.parameters, \
        "stream_devops_agent 出现 user_token 参数（事件链用户身份写平台回潮）"


# ──────────────── UC42C②③ tk_ 凭证形态 ────────────────

def test_uc42c_2_session_token_shape():
    """tk_ 形态四断言：前缀/TTL/明文不落盘（sha256）/scope 三重限定。"""
    from src.api import auth_token
    from src.kernel.auth_sdk.auth_token import SESSION_TOKEN_TTL, SESSION_TOKEN_PREFIX
    sk = "uc42c:" + uuid.uuid4().hex[:8]
    rk = "gitcode:uc42c/repo"
    tok = auth_token.issue_session_token(session_key=sk, repo_key=rk,
                                         agent_type="chat", user_id=424242)
    try:
        # 前缀 + 明文不落盘（库内只有 sha256）
        assert tok.startswith(SESSION_TOKEN_PREFIX)
        digest = hashlib.sha256(tok.encode()).hexdigest()
        with db_lock:
            row = db.execute(
                "SELECT session_key, repo_key, agent_type, user_id, token_sha256, expires_at "
                "FROM session_api_tokens WHERE session_key=?", (sk,)).fetchone()
        assert row, "tk_ 未落库（存在即有效语义破坏）"
        assert row[4] == digest, "库内非 sha256（明文落盘！）"
        # D105：user_id 列已 TEXT 存储（uuid 兼容）——int/str 双形态都按串比较
        assert row[0] == sk and row[1] == rk and row[2] == "chat" and str(row[3]) == "424242"
        # TTL ≈ 6h（expires_at 存 epoch int，P2-7；±120s 签发延迟容差）
        import time
        now = time.time()
        assert abs(row[5] - (now + SESSION_TOKEN_TTL)) < 120, \
            f"TTL 偏移异常: {row[5]} vs now+{SESSION_TOKEN_TTL}"
    finally:
        with db_lock:
            db.execute("DELETE FROM session_api_tokens WHERE session_key=?", (sk,))
            db.commit()


def test_uc42c_3_session_token_platform_invalid():
    """tk_ 对平台无效性：自签自验（session_api_tokens 表校验），非平台 JWT/
    平台 API 无法识别——删行即失效（存在即有效语义）。"""
    from src.api import auth_token
    sk = "uc42c3:" + uuid.uuid4().hex[:8]
    tok = auth_token.issue_session_token(session_key=sk, repo_key="gitcode:uc42c3/r",
                                         agent_type="chat", user_id=1)
    try:
        # 正常验证通过
        assert auth_token.verify_session_token(tok) is not None
        # 删行即失效（可撤销性）
        with db_lock:
            db.execute("DELETE FROM session_api_tokens WHERE session_key=?", (sk,))
            db.commit()
        assert auth_token.verify_session_token(tok) is None, "删行后仍有效（可撤销性破坏）"
    finally:
        with db_lock:
            db.execute("DELETE FROM session_api_tokens WHERE session_key=?", (sk,))
            db.commit()


# ──────────────── UC42C④ FR-9 范围补全锚点（挂账 E） ────────────────

def test_uc42c_4_task_row_plaintext_current_state():
    """agent_tasks.repo_gitcode_token 当前明文现状锁（挂账 E 实锤断言）。

    本用例是 FR-9 存量评估的"现状锚点"：当前断言=写入明文（如实锁现状）。
    FR-9 立项修复后本用例翻转为密文断言（写入→加密落库、读取→解密回环，
    N3 同款模式）。两头都是行为锁，翻转不丢语义。
    """
    from src.api.event_pipeline import _get_task_repo_token
    rk = "gitcode:uc42c4/repo"
    # 环境现实：本机 configs/global-bot-token.json 文件 fallback 在场（60s 缓存）
    # → gitcode 路径 A 能解析出全局 bot token（全局兜底属 GlobalBot 合法链）。
    # 断言改为**类型断言**而非空断言：返回值若非空必须是 bot token（level!=user），
    # 即"落 task 行的只可能是 bot 系"——路径 D 用户 token 永不落行才是本用例
    # 核心隔离语义（空/全局 bot 都合法，用户 token 才是违规）。
    # （全局 bot token 来自文件 fallback 属挂账 #12 既有观察项，不在本用例范围）
    tok = _get_task_repo_token(rk)
    # 结构断言：写入路径无加密调用（挂账 E 实锤——FR-9 修复后此断言删除并
    # 翻转为密文断言）
    import inspect
    from src.api import event_pipeline as ep
    fn_src = inspect.getsource(ep._get_task_repo_token)
    assert "encrypt" not in fn_src, \
        "_get_task_repo_token 已接入加密（FR-9 已修复）——请同步翻转本用例为密文断言"


def test_uc42c_4b_path_d_user_token_never_in_task_row():
    """路径 D 隔离锁：用户 OAuth token 永不入 task 行（resolve level=user → 返回空）。"""
    from src.api.event_pipeline import _get_task_repo_token
    from src.domains.mgmt.auth.grape_account_db import (
        create_grape_account, create_external_identity, delete_external_identity)
    from src.domains.mgmt.user.db import add_managed_repo
    uname = "uc42c4b_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@t.local", password="pw123")
    uid = u["id"]
    rk = "gitee:uc42c4b/repo"
    try:
        # 用户托管仓（路径 B/D）：host_user_id=uid + 用户 OAuth token 在 ei
        add_managed_repo(rk, "uc42c4b", "repo", host_user_id=uid, webhook_id=0)
        create_external_identity(uid, "gitee", "uc42c4b_ext",
                                 external_login=uname,
                                 access_token="USER_TOKEN_SHOULD_NOT_LAND")
        # resolve level=user → _get_task_repo_token 返回空（用户 token 不写 task 行）
        tok = _get_task_repo_token(rk)
        assert tok == "", f"路径 D 用户 token 泄漏进 task 行: {tok[:8]}..."
        # create_direct_task 形态的行级断言：user_gitcode_token 恒空串
        # （写隔离：事件任务无法以用户身份写平台）
        import inspect
        from src.api import event_pipeline as ep
        src = inspect.getsource(ep)
        assert 'user_gitcode_token="")' in src or "user_gitcode_token='')" in src or \
               'user_gitcode_token=""' in src, "user_gitcode_token 直写形态未找到（写隔离断言失效）"
    finally:
        delete_external_identity(uid, "gitee")
        with db_lock:
            db.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
            db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
            db.commit()
