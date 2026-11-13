"""IM 通道目录（S99/D74，um 退役批次 3，用户拍板方案 B）：feishu / gitcode_nickname 通道数据的唯一权威访问层。

背景：unified_user_mapping 中"身份域无对应物"的列（feishu_open_id/
feishu_name/app_id/gitcode_nickname）此前散落 6 处裸 SQL 读写
（prompt_builder / routes.settings / routes.chat / im.handler_async ×2 /
api.issues_center）。本模块把读写收敛为单一 API，um 列降级为本模块的
私有存储实现——um 表退役（存储切换批）时只改本模块后端。

护栏（设计评审钉死，实现逐条落实）：
1. S54 解析顺序内置：身份域优先（external_identities→user_id）→
   um 名字域兜底（FR-CQ-1，D39/S59 无列平台跳过）→ FR-20.8/FR-22.5
   注册名/github_login/账号回退链（session_manager.get_feishu_open_id）。
   调用方不感知存储后端。
2. app 级语义成对：bind()/write_back() 的 app_id 为必填关键字参数
   （可为空串，但必须显式传）——不存在"只写 open_id 不声明 app"的路径；
   解析接口 app_id 显式默认 ""（向后兼容语义：空=不按 app 过滤）。
3. 依赖方向单向：本模块可读身份域（grape_account_db），身份域/存储层
   禁止反向 import 本模块。
4. 零行为变化：全部函数为存量裸 SQL/既有 helper 的原样搬运（含
   FR-18.4 USER_ID>0 约束、FR-21.10 user_id 精确清空、app 变更清空语义）。

登记（存储切换批出口条件，见 D74 台账）：feishu_directory 回归全绿 +
存储后端切换方案评审通过 → 换后端 + IM 链路实弹验证。届时若
external_identities 已自然演进 app_id/provider 扩展位则并入身份域
（原方案 A），否则维持模块私有存储，不强行统一。
"""
from __future__ import annotations

from datetime import datetime

from src.kernel.config import log


# ============================== feishu 子域 ==============================

def bind(user_id: str, open_id: str, feishu_name: str = "", *, app_id: str) -> bool:
    """绑定飞书身份（p2p 确认/扫码链路，FR-18.7：只写飞书三列，不碰平台列）。"""
    if not user_id:
        return False
    from src.kernel.session_manager import bind_feishu_identity
    return bind_feishu_identity(user_id, open_id, feishu_name=feishu_name, app_id=app_id)


def write_back(user_id: str, open_id: str, feishu_name: str, *, app_id: str) -> None:
    """agent 引导回写（prompt_builder 语义，FR-18.4：USER_ID 必须>0，禁 0 占位行）。"""
    if not (user_id and str(user_id or "") not in ("", "0")):
        return
    # P3-5c：unified_user_mapping 是 mgmt 基表；runtime/IM 进程写入走 mgmt lib + 幂等意图
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_wb
    _mg_wb = _mgmt_lib_wb(foreign_only=True)
    if _mg_wb is not None:
        try:
            import hashlib as _hl_wb
            from src.kernel import cross_write as _xw_wb

            def _apply_wb() -> None:
                _mg_wb.upsert_user_mapping_feishu(str(user_id), open_id, feishu_name, app_id)

            _oid_wb = _hl_wb.sha1((open_id or "").encode("utf-8")).hexdigest()[:12]
            _res_wb = _xw_wb.submit(
                "mgmt", "user_mapping_feishu_upsert",
                _xw_wb.unique_key(f"um-feishu:{user_id}:{app_id}:{_oid_wb}"),
                {"user_id": str(user_id), "open_id": open_id,
                 "feishu_name": feishu_name, "app_id": app_id}, _apply_wb)
            if _res_wb.get("status") == "failed":
                log.warning(f"channel_directory.write_back queued for retry: {_res_wb.get('error')}")
            return
        except Exception as _e_wb:
            log.warning(f"channel_directory.write_back lib path failed, fallback to local SQL: {_e_wb}")
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", (user_id,))
        c.execute(
            "UPDATE unified_user_mapping SET feishu_open_id=?, feishu_name=?, app_id=?, "
            "updated_at=datetime('now') WHERE user_id=? AND (user_id IS NOT NULL AND user_id != '' AND user_id != '0')",
            (open_id, feishu_name, app_id, user_id))
        db.commit()


def resolve_open_id_by_login(gitcode_user: str, app_id: str = "") -> "str | None":
    """按平台 login/注册名解析 open_id（session_manager 既有回退链原样：
    gitcode_user → github_login（FR-20.8）→ 注册名→user_id（FR-22.5）；
    app_id 非空 → 仅该 App（app 级 open_id）。"""
    from src.kernel.session_manager import get_feishu_open_id
    return get_feishu_open_id(gitcode_user, app_id=app_id)


def resolve_open_id_by_user(user_id: str) -> str:
    """按 grape_accounts user_id 解析 open_id（assignee 为空场景，feishu-openid-by-user-id）。"""
    if not user_id:
        return ""
    from src.kernel.session_manager import db, db_lock
    from src.kernel.domain_read import mgmt_lib
    _mg = mgmt_lib()
    if _mg is not None:
        return _mg.get_feishu_open_id(user_id) or ""
    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import mgmt_lib as _mg_oid
        _oid = _mg_oid(foreign_only=True)
        if _oid is not None:
            row = _oid.read_one("um_feishu_open_id_by_user", (user_id,))
        else:
            c.execute(
                "SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=? AND feishu_open_id!='' LIMIT 1",
                (user_id,))
            row = c.fetchone()
        return row[0] if row and row[0] else ""


def resolve_open_id_for_login(gitcode_user: str, repo_key: str = "") -> str:
    """通知解析统一入口（S54/D34 顺序内置）：身份域优先（login→user_id→open_id），
    未命中回退 um 名字域（FR-CQ-1；D39/S59 无 um 列平台跳过）。"""
    if not gitcode_user:
        return ""
    # S54 身份优先
    try:
        from src.kernel.repo_key import parse_repo_key
        from src.domains.mgmt.auth.grape_account_db import resolve_user_id_by_platform_login
        _prov, _, _ = parse_repo_key(repo_key or "")
        _uid = resolve_user_id_by_platform_login(_prov, gitcode_user)
        if _uid:
            _oid = resolve_open_id_by_user(_uid)
            if _oid:
                return _oid
    except Exception as _e:
        log.warning(f"channel_directory.resolve_open_id_for_login: identity-first failed "
                    f"(user={gitcode_user!r}, repo={repo_key!r}): {_e}")
    # FR-CQ-1: um 名字域兜底
    from src.kernel.repo_key import um_column_for_repo
    _col = um_column_for_repo(repo_key)
    if not _col:
        return ""
    from src.kernel.session_manager import db, db_lock
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_cd
    _mg2 = _mgmt_lib_cd()
    if _mg2 is not None:
        _hit = _mg2.fetch_one("unified_user_mapping", ["feishu_open_id"], f"{_col}=%s AND feishu_open_id<>''", (gitcode_user,), "")
        return _hit[0] if _hit and _hit[0] else ""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT feishu_open_id FROM unified_user_mapping WHERE {_col}=? "
                      "AND feishu_open_id!='' LIMIT 1", (gitcode_user,))
            row = c.fetchone()
            return row[0] if row else ""
        except Exception:
            return ""


def reverse_lookup_by_open_id(open_id: str) -> dict:
    """open_id → {user_id, gitcode_user, feishu_name, gitcode_nickname}（IM 入站反查）。"""
    out = {"user_id": None, "gitcode_user": None, "feishu_name": "", "gitcode_nickname": ""}
    if not open_id:
        return out
    from src.kernel.session_manager import db, db_lock
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_rev
    _mg3 = _mgmt_lib_rev()
    if _mg3 is not None:
        _row = _mg3.fetch_one("unified_user_mapping", ["user_id", "gitcode_user", "feishu_name", "gitcode_nickname"], "feishu_open_id=%s", (open_id,))
        if _row:
            out["user_id"], out["gitcode_user"] = _row[0], _row[1]
            out["feishu_name"] = _row[2] or ""
            out["gitcode_nickname"] = _row[3] or ""
        return out
    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import mgmt_lib as _mg_umrow
        _umrow = _mg_umrow(foreign_only=True)
        if _umrow is not None:
            row = _umrow.read_one("um_row_by_feishu_open_id", (open_id,))
        else:
            c.execute(
                "SELECT user_id, gitcode_user, feishu_name, gitcode_nickname "
                "FROM unified_user_mapping WHERE feishu_open_id=? LIMIT 1", (open_id,))
            row = c.fetchone()
    if row:
        out["user_id"] = row[0]
        out["gitcode_user"] = row[1]
        out["feishu_name"] = row[2] or ""
        out["gitcode_nickname"] = row[3] or ""
    return out


def clear_for_app(old_app_id: str) -> None:
    """app 释放：清空该 app 全部用户的 open_id/feishu_name/app_id（open_id 为 app 级，app 释放即失效）。"""
    if not old_app_id:
        return
    # P3-5c：跨域写走 mgmt lib；app 作用域必须保留（WHERE app_id=?）
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_ca
    _mg_ca = _mgmt_lib_ca(foreign_only=True)
    if _mg_ca is not None:
        try:
            from src.kernel import cross_write as _xw_ca
            _res_ca = _xw_ca.submit(
                "mgmt", "user_mapping_feishu_clear",
                _xw_ca.unique_key(f"um-clear-app:{old_app_id}"),
                {"app_id": old_app_id},
                lambda: _mg_ca.clear_user_mapping_feishu(app_id=old_app_id))
            if _res_ca.get("status") == "failed":
                log.warning(f"channel_directory.clear_for_app queued for retry: {_res_ca.get('error')}")
            return
        except Exception as _e_ca:
            log.warning(f"channel_directory.clear_for_app lib path failed, fallback: {_e_ca}")
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', "
                  "updated_at=? WHERE app_id=?", (datetime.now().isoformat(), old_app_id))
        db.commit()


def clear_for_user(user_id: str) -> None:
    """按 user_id 精确清当前用户飞书映射（FR-21.10：不按用户名扫列，防误清同名 GitHub 用户）。"""
    if not user_id:
        return
    # P3-5c：跨域写走 mgmt lib；user 作用域必须保留（WHERE user_id=?）
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_cu
    _mg_cu = _mgmt_lib_cu(foreign_only=True)
    if _mg_cu is not None:
        try:
            from src.kernel import cross_write as _xw_cu
            _res_cu = _xw_cu.submit(
                "mgmt", "user_mapping_feishu_clear",
                _xw_cu.unique_key(f"um-clear-user:{user_id}"),
                {"user_id": str(user_id)},
                lambda: _mg_cu.clear_user_mapping_feishu(user_id=str(user_id)))
            if _res_cu.get("status") == "failed":
                log.warning(f"channel_directory.clear_for_user queued for retry: {_res_cu.get('error')}")
            return
        except Exception as _e_cu:
            log.warning(f"channel_directory.clear_for_user lib path failed, fallback: {_e_cu}")
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', "
                  "updated_at=? WHERE user_id=?", (datetime.now().isoformat(), user_id))
        db.commit()


# ============================ gitcode_nickname 子域 ============================

def set_gitcode_nickname(user_id: str, nickname: str) -> None:
    """更新 gitcode_nickname（settings update_credentials 同步语义）。"""
    if not user_id:
        return
    # P3-5c：跨域写走 mgmt lib
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_nk
    _mg_nk = _mgmt_lib_nk(foreign_only=True)
    if _mg_nk is not None:
        try:
            import hashlib as _hl_nk
            from src.kernel import cross_write as _xw_nk

            def _apply_nk() -> None:
                _mg_nk.set_user_mapping_nickname(str(user_id), nickname or "")

            _sig_nk = _hl_nk.sha1((nickname or "").encode("utf-8")).hexdigest()[:12]
            _res_nk = _xw_nk.submit(
                "mgmt", "user_mapping_nickname_set",
                _xw_nk.unique_key(f"um-nick:{user_id}:{_sig_nk}"),
                {"user_id": str(user_id), "nickname": nickname or ""}, _apply_nk)
            if _res_nk.get("status") == "failed":
                log.warning(f"channel_directory.set_gitcode_nickname queued for retry: "
                            f"{_res_nk.get('error')}")
            return
        except Exception as _e_nk:
            log.warning(f"channel_directory.set_gitcode_nickname lib path failed, fallback: {_e_nk}")
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", (user_id,))
        c.execute("UPDATE unified_user_mapping SET gitcode_nickname=?, updated_at=datetime('now') "
                  "WHERE user_id=?", (nickname or "", user_id))
        db.commit()
