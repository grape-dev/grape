"""组织数据访问层（Organization DAL）。

提供组织 CRUD、成员管理、组织上下文解析等数据库操作。
"""
from __future__ import annotations

from src.kernel.session_manager import db, db_lock
from src.kernel.config import log


class _OwnedCursor:
    """自带连接的游标：close 时把连接一并关掉。"""

    def __init__(self, conn, cursor):
        self._conn = conn
        self._cur = cursor

    def __getattr__(self, item):
        return getattr(self._cur, item)

    def close(self):
        try:
            self._cur.close()
        finally:
            try:
                self._conn.close()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")


class _MgmtDbProxy:
    """把 db.cursor()/commit()/rollback() 代理到 mgmt 域连接。

    mgmt 进程里 current_domain()=='mgmt' → 直接用主库（零回归）；
    其它进程（insight/runtime/event 做鉴权时）→ 用 GRAPE_DSN_MGMT 的新连接；
    单库/测试拓扑 → 回落进程库并保留显式提交。
    """

    def cursor(self, *a, **kw):
        from src.kernel import db_registry
        if db_registry.current_domain() == "mgmt":
            from src.kernel.session_manager import db as _main
            return _main.cursor(*a, **kw)
        if db_registry.uses_own_dsn("mgmt"):
            _conn = db_registry.new_connection("mgmt")
            return _OwnedCursor(_conn, _conn.cursor(*a, **kw))
        from src.kernel.session_manager import db as _main
        return _main.cursor(*a, **kw)

    def commit(self):
        from src.kernel import db_registry
        if db_registry.current_domain() == "mgmt":
            from src.kernel.session_manager import db as _main
            return _main.commit()
        if db_registry.uses_own_dsn("mgmt"):
            return None
        from src.kernel.session_manager import db as _main
        return _main.commit()

    def rollback(self):
        from src.kernel import db_registry
        if db_registry.current_domain() == "mgmt":
            from src.kernel.session_manager import db as _main
            return _main.rollback()
        if db_registry.uses_own_dsn("mgmt"):
            try:
                db_registry.domain_db("mgmt").rollback()
            except Exception as _e:
                log.debug(f"domain rollback failed: {_e}")
            return None
        from src.kernel.session_manager import db as _main
        return _main.rollback()

    def __getattr__(self, item):
        from src.kernel.session_manager import db as _main
        return getattr(_main, item)


_mg_db = _MgmtDbProxy()
db = _mg_db


def create_org(name: str, slug: str, owner_id: str, description: str = "",
               is_personal: int = 0) -> dict | None:
    """创建组织并添加创建者为 owner。D105 第三批：org.id 用 uuid7_str 显式生成
    （TEXT 主键，退役 lastrowid）；owner_id 入库前 str() 归一。"""
    try:
        from src.kernel.id_gen import uuid7_str
        with db_lock:
            c = db.cursor()
            owner_id = str(owner_id)
            org_id = uuid7_str()
            c.execute(
                "INSERT INTO organization (id, name, slug, description, owner_id, is_personal) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (org_id, name, slug, description, owner_id, is_personal)
            )
            c.execute(
                "INSERT OR IGNORE INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                (org_id, owner_id)
            )
            db.commit()
            return get_org(org_id)
    except Exception as e:
        log.warning(f"create_org failed: {e}")
        return None


def get_org(org_id: str) -> dict | None:
    """获取组织详情。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT * FROM organization WHERE id=?", (org_id,))
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row))


def get_org_by_slug(slug: str) -> dict | None:
    """按 slug 查组织。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT * FROM organization WHERE slug=?", (slug,))
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row))


def list_user_orgs(user_id: str) -> list[dict]:
    """列出用户加入的所有组织（含角色和成员数）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT o.*, m.role as my_role, "
            "(SELECT count(*) FROM org_member WHERE org_id=o.id) as member_count "
            "FROM organization o "
            "INNER JOIN org_member m ON m.org_id = o.id "
            "WHERE m.user_id=? ORDER BY o.is_personal DESC, o.created_at",
            (user_id,)
        )
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in rows]


def update_org(org_id: str, name: str = "", description: str = "",
               avatar_url: str = "", settings: str = "",
               max_members: int = 0, max_workers: int = 0) -> bool:
    """更新组织信息（仅非空/非零字段）。"""
    sets = []
    params = []
    if name:
        sets.append("name=?")
        params.append(name)
    if description is not None and description != "":
        sets.append("description=?")
        params.append(description)
    if avatar_url:
        sets.append("avatar_url=?")
        params.append(avatar_url)
    if settings:
        sets.append("settings=?")
        params.append(settings)
    if max_members and max_members > 0:
        sets.append("max_members=?")
        params.append(max_members)
    if max_workers and max_workers > 0:
        sets.append("max_workers=?")
        params.append(max_workers)
    if not sets:
        return True
    sets.append("updated_at=datetime('now')")
    params.append(org_id)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE organization SET {', '.join(sets)} WHERE id=?", params)
        db.commit()
        return c.rowcount > 0


def delete_org(org_id: str) -> bool:
    """删除组织（个人组织不可删）。"""
    with db_lock:
        c = db.cursor()
        # 检查是否个人组织
        c.execute("SELECT is_personal FROM organization WHERE id=?", (org_id,))
        row = c.fetchone()
        if not row:
            return False
        if row[0]:
            return False  # 个人组织不可删
        c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE id=?", (org_id,))
        db.commit()
        return True


def get_org_members(org_id: str) -> list[dict]:
    """列出组织成员（含用户信息）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT m.*, u.username, u.nickname "
            "FROM org_member m "
            "INNER JOIN grape_accounts u ON u.id = m.user_id "
            "WHERE m.org_id=? ORDER BY m.role, m.joined_at",
            (org_id,)
        )
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in rows]


def add_org_member(org_id: str, user_id: str, role: str = "member",
                   invited_by: str = "") -> bool:
    """添加成员到组织。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO org_member (org_id, user_id, role, invited_by) "
                "VALUES (?, ?, ?, ?)",
                (org_id, user_id, role, invited_by)
            )
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"add_org_member failed: {e}")
        return False


def update_member_role(org_id: str, user_id: str, role: str) -> bool:
    """修改成员角色。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE org_member SET role=? WHERE org_id=? AND user_id=?",
            (role, org_id, user_id)
        )
        db.commit()
        return c.rowcount > 0


def remove_org_member(org_id: str, user_id: str) -> bool:
    """移除成员（owner 不可移除）。"""
    with db_lock:
        c = db.cursor()
        # 检查是否 owner
        c.execute(
            "SELECT role FROM org_member WHERE org_id=? AND user_id=?",
            (org_id, user_id)
        )
        row = c.fetchone()
        if not row:
            return False
        if row[0] == "owner":
            return False  # owner 不可移除
        c.execute(
            "DELETE FROM org_member WHERE org_id=? AND user_id=?",
            (org_id, user_id)
        )
        db.commit()
        return True


def get_member_role(user_id: str, org_id: str) -> str:
    """获取用户在组织中的角色。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT role FROM org_member WHERE org_id=? AND user_id=?",
            (org_id, user_id)
        )
        row = c.fetchone()
        return row[0] if row else ""


def is_org_member(user_id: str, org_id: str) -> bool:
    """检查用户是否是组织成员。"""
    return bool(get_member_role(user_id, org_id))


def get_personal_org_id(user_id: str) -> str:
    """获取用户的个人组织 ID（未找到返回 ''）。D105 第三批：org_id 哨兵 0 → ''。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM organization WHERE owner_id=? AND is_personal=1",
            (str(user_id),)
        )
        row = c.fetchone()
        return str(row[0]) if row else ""


def is_org_personal(org_id: str) -> bool:
    """判断指定组织是否是个人组织。"""
    if not org_id:
        return False
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT is_personal FROM organization WHERE id=?",
            (org_id,)
        )
        row = c.fetchone()
        return bool(row and row[0])


def get_org_id_by_repo_key(repo_key: str) -> str:
    """通过 repo_key 查 managed_repos.org_id（未归属返回 ''）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT org_id FROM managed_repos WHERE repo_key=?", (repo_key,)
        )
        row = c.fetchone()
        return str(row[0]) if row and row[0] else ""
