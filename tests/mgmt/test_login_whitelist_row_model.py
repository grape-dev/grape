"""行式登录白名单模型（weekly-11，2026-11-12）S1-S11 场景用例。

需求：docs/weekly-11/login-whitelist-requirements.md
设计：docs/weekly-11/login-whitelist-design.md

覆盖：
- S1  双读兼容（新行优先→老列兜底，逐人判定）
- S2  全渠道添加（登录域自治，VCS 卡轴退出判定）
- S3  行式登录校验（provider 隔离）
- S4  无声明渠道不再绕过（fail-open 状态消失）
- S5  大小写不敏感唯一
- S6  空表语义 fail-closed（开关开+表空=拒绝所有 OAuth 登录，2026-11-12 拍板）
- S7  管理面行式渲染（前端零平台字面量）
- S8  改名联动双写
- S9  退役锁（is_gitcode/github_user_allowed 符号不存在）
- S10 守卫（whitelist_column 卡字段读取归零）
- S11 存量条目删除粒度
"""
import os
import sys
import json
from pathlib import Path

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user.db import set_meta_value
from src.kernel.config import is_user_allowed, invalidate_login_whitelist_cache


def _wl_enable(on: bool = True):
    set_meta_value("login_whitelist_enabled", "true" if on else "false")
    invalidate_login_whitelist_cache()


def _clear():
    with db_lock:
        db.execute("DELETE FROM login_whitelist WHERE id > 0")  # F5：ratchet 禁无 WHERE 整表 DELETE
        db.commit()


def _add_row(provider: str, username: str, remark: str = ""):
    with db_lock:
        db.execute(
            "INSERT OR IGNORE INTO login_whitelist (provider, username, remark) VALUES (?,?,?)",
            (provider, username, remark))
        db.commit()


def _add_legacy_col(col: str, username: str, remark: str = ""):
    """老列式行（模拟存量部署数据——列在 DDL/历史动态建列中存在）。"""
    with db_lock:
        db.execute(
            f"INSERT OR IGNORE INTO login_whitelist ({col}, remark) VALUES (?,?)",
            (username, remark))
        db.commit()


# ───────────────────────── S1 双读兼容 ─────────────────────────

def test_login_whitelist_row_dual_read():
    """S1：老列条目与新行条目并存时逐人均放行（任一命中）；provider 隔离。"""
    _clear()
    _wl_enable(True)
    _add_legacy_col("gitcode_username", "alice", "legacy")
    _add_row("gitee", "bob")
    try:
        assert is_user_allowed("gitcode", "alice") is True   # 老列兜底命中
        assert is_user_allowed("gitee", "bob") is True       # 新行命中
        assert is_user_allowed("github", "alice") is False   # 老行其他列不命中
        assert is_user_allowed("gitcode", "bob") is False    # 新行 provider 隔离
    finally:
        _wl_enable(False)
        _clear()


# ───────────────────────── S2 全渠道添加（登录域自治） ─────────────────────────

def test_login_whitelist_add_all_channels():
    """S2：gitee（有渠道卡、无 VCS 卡）可加白名单；未在册渠道 400。

    跨域 VCS 卡判定已删——gitee 在 VCS 默认卡目录外，旧双轴判定下必被
    INVALID_PROVIDER 挡死，即耦合实锤；现在登录域自治放行。
    """
    from src.domains.mgmt.admin import db as admin_db
    from src.domains.mgmt.admin.routes import admin_add_login_whitelist, LoginWhitelistBody
    from src.domains.mgmt.auth.providers import list_providers
    from fastapi import HTTPException
    import inspect

    assert "gitee" in list_providers()
    # db 层：行式 INSERT 成功
    _clear()
    assert admin_db.add_login_whitelist("gitee", "s2_user") is True
    entries = admin_db.list_login_whitelist()
    hit = [e for e in entries if e["provider"] == "gitee" and e["username"] == "s2_user"]
    assert hit and hit[0]["legacy"] is False
    # 路由层：在册渠道放行；未在册渠道 400（无跨域 VCS 检查）
    src = inspect.getsource(admin_add_login_whitelist)
    assert "get_platform_card" not in src, "POST /whitelist 残留跨域 VCS 卡判定（S2 回潮）"
    body = LoginWhitelistBody(provider="nosuchchannel", username="x")
    try:
        admin_add_login_whitelist(body, admin={"id": "t"})
        raise AssertionError("未在册渠道未拒绝")
    except HTTPException as e:
        assert e.status_code == 400
    try:
        _clear()
        body = LoginWhitelistBody(provider="gitlab", username="x")
        admin_add_login_whitelist(body, admin={"id": "t"})
        raise AssertionError("gitlab 有 VCS 卡但无渠道卡——必须 400（渠道轴）")
    except HTTPException as e:
        assert e.status_code == 400


# ───────────────────────── S3 行式登录校验（provider 隔离） ─────────────────────────

def test_login_whitelist_row_check():
    """S3：(gitee, bob) 在表 → gitee 放行；gitcode 同名拒绝。"""
    _clear()
    _wl_enable(True)
    _add_row("gitee", "bob")
    try:
        assert is_user_allowed("gitee", "bob") is True
        assert is_user_allowed("gitcode", "bob") is False
    finally:
        _wl_enable(False)
        _clear()


# ───────────────────────── S4 无声明渠道不再绕过 ─────────────────────────

def test_login_whitelist_no_bypass():
    """S4：白名单开启且表非空时，无新行无老列的渠道拒绝（fail-open 状态消失）。"""
    _clear()
    _wl_enable(True)
    _add_row("gitcode", "someone")
    try:
        assert is_user_allowed("gitcode", "someone") is True
        # hwcloud：无新行、无 hwcloud_username 老列 → 拒绝（原 fail-open 放行）
        assert is_user_allowed("hwcloud", "someone") is False
    finally:
        _wl_enable(False)
        _clear()


# ───────────────────────── S5 大小写不敏感唯一 ─────────────────────────

def test_login_whitelist_ci_unique():
    """S5：(gitcode, Bob) 后再添加 (gitcode, bob) 幂等拒绝；登录 bob/Bob 均命中。"""
    from src.domains.mgmt.admin.db import add_login_whitelist
    _clear()
    _wl_enable(True)
    try:
        assert add_login_whitelist("gitcode", "Bob") is True
        assert add_login_whitelist("gitcode", "bob") is False   # CI 唯一索引拦
        assert is_user_allowed("gitcode", "bob") is True
        assert is_user_allowed("gitcode", "Bob") is True
    finally:
        _wl_enable(False)
        _clear()


# ───────────────────────── S6 空表 fail-closed 语义 ─────────────────────────

def test_login_whitelist_empty_denies_all():
    """S6：开关开启+表空 → 全拒（fail-closed，2026-11-12 拍板）。

    管理台走独立密码链（admin_accounts，admin/db.py verify_admin），
    不受此判定影响——可登入即时补条目，不构成不可逆锁死。
    """
    _clear()
    _wl_enable(True)
    try:
        assert is_user_allowed("gitcode", "anyone") is False
        assert is_user_allowed("hwcloud", "anyone") is False
    finally:
        _wl_enable(False)
        _clear()


def test_login_whitelist_empty_warn_banner():
    """S6：管理面空表红条在位（Vue 侧 v-if + 双语词条）。"""
    view = (SRC_ROOT / "api/frontend/app/src/views/AdminView.vue").read_text(encoding="utf-8")
    assert 'v-if="loginWlEnabled === \'1\' && !loginWl.length"' in view
    assert "login_whitelist.empty_warn" in view
    assert ".card.card-warn" in view
    for lang in ("zh", "en"):
        loc = json.loads(
            (SRC_ROOT / f"api/frontend/shell/locales/{lang}/admin.json").read_text(encoding="utf-8")
        )
        assert loc["login_whitelist"].get("empty_warn")
        assert loc["login_whitelist"].get("no_providers")


# ───────────────────────── S7 管理面行式渲染 ─────────────────────────

def test_login_whitelist_admin_ui():
    """S7：管理面行式渲染零平台字面量（Vue 管理台，2026-11-14 迁移后口径）。

    迁移说明：管理台已从 admin.html + admin.js 迁到 Vue（app/src/views/AdminView.vue，
    产物 static-vue/viewjs/admin-view.js），断言打到 SFC 源码与其缓存版本位。
    """
    view = (SRC_ROOT / "api/frontend/app/src/views/AdminView.vue").read_text(encoding="utf-8")
    i = view.index("login_whitelist.title")
    block = view[i:view.index("<!-- ===== pwd modal ===== -->", i)]
    # 下拉零字面量平台 option（选项由 GET /admin/whitelist 的 providers 动态填充）
    assert 'value="gitcode"' not in block and 'value="github"' not in block
    assert 'v-for="p in lwProviders"' in block
    # 表头行式化（平台/用户名），双平台列头退役
    assert "col_gitcode" not in block and "col_github" not in block
    assert "col_provider" in block and "col_username" in block
    # 存量列式条目可见 + 删除按 provider 定向（S11 删除粒度前端口径）
    assert "providerLabel(w.provider)" in block
    assert "w.legacy" in block
    assert "'?provider='" in view
    # 渠道目录回填 + 空表红条（fail-closed 显性提示）
    assert "lwProviders.value = d.providers" in view
    assert "login_whitelist.empty_warn" in view
    # 缓存版本 bump（只升不降；admin-view.js 入口版本位）
    import re as _re

    html = (SRC_ROOT / "api/frontend/shell/admin.html").read_text(encoding="utf-8")
    _m = _re.search(r"admin-view\.js\?v=(\d+)", html)
    assert _m, "admin-view.js 版本号缺失/格式漂移"
    assert int(_m.group(1)) >= 18, "admin-view.js 版本号回退/未 bump"


# ───────────────────────── S8 改名联动双写 ─────────────────────────

def test_login_whitelist_rename_sync():
    """S8：平台改名联动新行（provider+username）与老列（派生列名）双写。"""
    from src.domains.mgmt.auth.grape_account_db import sync_external_login_on_rename
    _clear()
    _add_row("gitcode", "old_login", "s8_new")
    _add_legacy_col("gitcode_username", "old_login", "s8_legacy")
    try:
        sync_external_login_on_rename(0, "gitcode", "new_login", "old_login")
        with db_lock:
            row_new = db.execute(
                "SELECT username FROM login_whitelist WHERE remark='s8_new'").fetchone()
            row_legacy = db.execute(
                "SELECT gitcode_username FROM login_whitelist WHERE remark='s8_legacy'").fetchone()
        assert row_new and row_new[0] == "new_login"
        assert row_legacy and row_legacy[0] == "new_login"
    finally:
        _clear()


# ───────────────────────── S9 退役锁 ─────────────────────────

def test_login_whitelist_retired_symbols():
    """S9：平台名特化包装函数退役（rationale=行式模型 provider 传参取代列名特化）。"""
    import src.kernel.config as cfg
    assert not hasattr(cfg, "is_gitcode_user_allowed")
    assert not hasattr(cfg, "is_github_user_allowed")
    # is_user_allowed 签名=行式（provider, login）
    import inspect
    sig = inspect.signature(cfg.is_user_allowed)
    assert list(sig.parameters) == ["provider", "login"]


# ───────────────────────── S10 守卫（防回潮） ─────────────────────────

def test_login_whitelist_guard():
    """S10：src 中 whitelist_column 卡字段读取/声明归零；跨域 VCS 判定不回潮。"""
    offenders_field = []
    offenders_gate = []
    for p in SRC_ROOT.rglob("*.py"):
        s = p.read_text(encoding="utf-8")
        # 卡字段：读取点（dict get）与声明点（卡 JSON 键）归零（退役说明注释豁免）
        if '.get("whitelist_column"' in s or "'whitelist_column'" in s \
                or '"whitelist_column":' in s or "'whitelist_column':" in s:
            offenders_field.append(str(p.relative_to(REPO_ROOT)))
        # POST /whitelist 函数体禁跨域 VCS 卡判定
        if "def admin_add_login_whitelist" in s:
            import re
            m = re.search(r"def admin_add_login_whitelist.*?(?=\n@router|\ndef )", s, re.S)
            if m and "get_platform_card" in m.group(0):
                offenders_gate.append(str(p.relative_to(REPO_ROOT)))
    assert not offenders_field, f"whitelist_column 残留读取/声明点: {offenders_field}"
    assert not offenders_gate, f"POST /whitelist 跨域 VCS 判定回潮: {offenders_gate}"


# ───────────────────────── S11 存量条目删除粒度 ─────────────────────────

def test_login_whitelist_delete_scoped():
    """S11：老列行按 provider 清列（一人多平台互不误伤）；行全空整行删；新行行式删。"""
    from src.domains.mgmt.admin.db import add_login_whitelist, delete_login_whitelist, list_login_whitelist
    _clear()
    # 老列行：同 row 双平台
    with db_lock:
        db.execute(
            "INSERT INTO login_whitelist (gitcode_username, github_username, remark) "
            "VALUES ('alice', 'alice', 's11')")
        row_id = db.execute(
            "SELECT id FROM login_whitelist WHERE remark='s11'").fetchone()[0]
        db.commit()
    # 只删 gitcode 条目 → github 条目保留
    assert delete_login_whitelist(row_id, provider="gitcode") is True
    with db_lock:
        row = db.execute(
            "SELECT gitcode_username, github_username FROM login_whitelist WHERE id=?",
            (row_id,)).fetchone()
    assert row is not None
    assert (row[0] or "") == "" and row[1] == "alice"
    # 再删 github 条目 → 行全空 → 整行删
    assert delete_login_whitelist(row_id, provider="github") is True
    with db_lock:
        gone = db.execute("SELECT id FROM login_whitelist WHERE id=?", (row_id,)).fetchone()
    assert gone is None
    # 新行：行式删
    assert add_login_whitelist("gitee", "s11_user") is True
    wid = [e["id"] for e in list_login_whitelist()
           if e["provider"] == "gitee" and e["username"] == "s11_user"][0]
    assert delete_login_whitelist(wid, provider="gitee") is True
    assert not [e for e in list_login_whitelist() if e["id"] == wid]
    # 列表展开：老列条目 legacy=True，新行 legacy=False
    _add_legacy_col("gitcode_username", "legacy_view_user", "s11b")
    assert add_login_whitelist("gitee", "row_view_user") is True
    entries = list_login_whitelist()
    lv = [e for e in entries if e["username"] == "legacy_view_user"]
    rv = [e for e in entries if e["username"] == "row_view_user"]
    assert lv and lv[0]["legacy"] is True and lv[0]["provider"] == "gitcode"
    assert rv and rv[0]["legacy"] is False
    _clear()
