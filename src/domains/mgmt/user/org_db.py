"""组织数据访问层（Organization DAL）。

提供组织 CRUD、成员管理、组织上下文解析等数据库操作。
"""
from __future__ import annotations

from src.kernel.session_manager import db, db_lock
from src.kernel.config import log
from src.kernel.lock import (
    acquire_lock_with_retry,
    release_lock,
    repo_dispatch_lease_key,
)


# rationale（数值登记 docs/org-pipeline/org-runtime-budget.md §2）：绑定/解绑是单条
# 事务内的写操作（实测本地 P95 毫秒级，见 tests/mgmt/test_org_manual_bind.py 的并发用例），
# 30s 仅用于覆盖慢 SQL / 瞬时锁竞争；内核默认 60s 面向分钟级任务，绑定场景取更短值
# 让用户重试不被长时间挡住。attempts=20 × interval=0.05s = 1s 退避上限，与
# src/kernel/lock.py:68 默认一致。
_ORG_REPO_BIND_LEASE_TTL = 30  # rationale: docs/org-pipeline/org-runtime-budget.md §2
_ORG_REPO_BIND_LOCK_ATTEMPTS = 20  # rationale: 同 kernel/lock.py 默认重试次数
_ORG_REPO_BIND_LOCK_INTERVAL = 0.05  # rationale: 同 kernel/lock.py 默认退避间隔
_REPO_DISPATCH_LEASE_TTL = 30  # rationale: 与绑定租约同口径（单事务级写操作）


def _seed_org_overview_basic(org: dict) -> None:
    """组织创建时自动写一条「组织概览」上下文（Issue #2269）。

    只写团队组织；幂等（brief 单 key）。provisioning 完成后再补充仓库/场景信息。
    """
    try:
        from src.domains.mgmt import org_context_store as _ocs
        content = (
            "# 组织概览\n\n"
            f"- 名称：{org.get('name') or ''}\n"
            f"- Slug：{org.get('slug') or ''}\n"
            f"- 描述：{org.get('description') or '（未填写）'}\n"
            "- 类型：团队组织\n"
            f"- Owner：{org.get('owner_id') or ''}\n"
            f"- 创建时间：{org.get('created_at') or ''}\n"
            "\n> 系统自动生成；组织仓库、流水线场景与关联仓库会在 provisioning 完成后补充。\n"
        )
        _ocs.upsert_context(
            str(org.get("id") or ""), "brief", "overview", "组织概览", content,
            owner_id=str(org.get("owner_id") or ""), user_id=str(org.get("owner_id") or ""))
    except Exception as e:
        log.warning(f"seed org overview failed org={org.get('id')}: {e}")


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
               is_personal: int = 0, extra_fields: dict | None = None) -> dict | None:
    """创建组织并添加创建者为 owner。D105 第三批：org.id 用 uuid7_str 显式生成
    （TEXT 主键，退役 lastrowid）；owner_id 入库前 str() 归一。

    extra_fields：额外列（仅 _PROVISION_FIELDS 白名单），与组织行**同一条 INSERT**
    原子写入。2026-11-23 事故：建组织先提交组织行，再单独 UPDATE provisioning
    选项；目标库缺列时 UPDATE 1054 → 接口 500，留下 provision_status=NULL 的
    半成品组织。调用方应把 provisioning 初始状态与仓库选项一起从这里写入。
    """
    extra = dict(extra_fields or {})
    invalid = set(extra) - _PROVISION_FIELDS
    if invalid:
        raise ValueError(f"invalid organization create fields: {sorted(invalid)}")
    try:
        from src.kernel.id_gen import uuid7_str
        with db_lock:
            c = db.cursor()
            owner_id = str(owner_id)
            org_id = uuid7_str()
            cols = ["id", "name", "slug", "description", "owner_id", "is_personal"]
            values = [org_id, name, slug, description, owner_id, is_personal]
            for key, value in extra.items():
                cols.append(key)
                values.append(value)
            c.execute(
                f"INSERT INTO organization ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                values,
            )
            c.execute(
                "INSERT OR IGNORE INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                (org_id, owner_id)
            )
            db.commit()
        org = get_org(org_id)
        if org and not is_personal:
            _seed_org_overview_basic(org)
        return org
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
        data = dict(zip(cols, row))
        # D105 第三批：存量行 org_id 可能仍为 0/'0'（未归属哨兵），读侧归一为 ''
        if str(data.get("org_id") or "") in ("", "0"):
            data["org_id"] = ""
        return data


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
               max_members: int = 0, max_workers: int = 0,
               max_repos: int = 0) -> bool:
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
    if max_repos and max_repos > 0:
        sets.append("max_repos=?")
        params.append(max_repos)
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


_PROVISION_FIELDS = frozenset({
    "org_repo_key",
    "org_repo_provider",
    "org_repo_owner",
    "org_repo_name",
    "org_repo_path",
    "org_repo_private",
    "org_scene_id",
    "provision_status",
    "provision_error",
    "provisioned_at",
    "provision_attempt",
})


_PROVISION_INTERMEDIATE_STATUSES = (
    "pending",
    "repo_creating",
    "hosting",
    "scene_creating",
)


def update_org_provision_fields(org_id: str, **fields) -> bool:
    """更新 organization provisioning 字段（仅白名单列）。"""
    invalid = set(fields) - _PROVISION_FIELDS
    if invalid:
        raise ValueError(f"invalid organization provision fields: {sorted(invalid)}")
    if not fields:
        return True
    sets = []
    params = []
    for key, value in fields.items():
        sets.append(f"{key}=?")
        params.append(value)
    params.append(org_id)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE organization SET {', '.join(sets)} WHERE id=?", params)
        db.commit()
        return c.rowcount > 0


def fail_org_provision_attempt(org_id: str, attempt: str, error: str) -> bool:
    """CAS this attempt's unfinished intermediate state to failed.

    Only applies when the row still carries the same ``provision_attempt`` token
    and is still in an intermediate state, so a newer lease holder's progress is
    never overwritten.
    """
    if not attempt:
        return False
    placeholders = ", ".join("?" for _ in _PROVISION_INTERMEDIATE_STATUSES)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE organization SET provision_status='failed', provision_error=? "
            f"WHERE id=? AND provision_attempt=? AND provision_status IN ({placeholders})",
            (error, org_id, attempt, *_PROVISION_INTERMEDIATE_STATUSES),
        )
        db.commit()
        if c.rowcount > 0:
            return True
        row = c.execute(
            "SELECT provision_attempt, provision_status FROM organization WHERE id=?",
            (org_id,),
        ).fetchone()
        return bool(
            row
            and str(row[0] or "") == attempt
            and (row[1] or "") == "failed"
        )


def claim_org_provision_retry(org_id: str) -> bool:
    """CAS retryable provision states to pending; ready/active states are untouched.

    NULL 也认：读侧一直把 NULL 归一成 "none"（provision_status 列可空），
    但旧 SQL 的 IN ('none','failed') 匹配不到 NULL。缺列事故（2026-11-23）留下的
    「组织已建、状态 NULL」半成品会因此 CAS 失败。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE organization SET provision_status='pending', provision_error='' "
            "WHERE id=? AND (provision_status IN ('none', 'failed') "
            "OR provision_status IS NULL)",
            (org_id,),
        )
        db.commit()
        return c.rowcount > 0


def get_managed_repo_binding(repo_key: str) -> dict | None:
    """读取 managed_repos 的组织关联字段。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, "
            "org_id, is_org_repo, linked_by, linked_at, created_at "
            "FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        )
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row))


def bind_repo_as_org_repo(repo_key: str, org_id: str, user_id: str) -> bool:
    """将已托管仓库指定为组织仓库（仅未归属或已属本组织时生效）。

    Issue #2258：手动绑定兜底通道，保留 org_id=''（未归属）可绑定语义。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos "
            "SET org_id=?, is_org_repo=1, linked_by=?, "
            "linked_at=COALESCE(NULLIF(linked_at, ''), datetime('now')) "
            "WHERE repo_key=? AND COALESCE(org_id, '') IN ('', ?)",
            (org_id, user_id, repo_key, org_id),
        )
        db.commit()
        return c.rowcount > 0


def clear_org_repo_flag(repo_key: str, org_id: str) -> int:
    """清除旧组织仓库的 is_org_repo 标记（替换成功后保持单组织仓库语义）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos SET is_org_repo=0 "
            "WHERE repo_key=? AND org_id=? AND COALESCE(is_org_repo, 0)=1",
            (repo_key, org_id),
        )
        db.commit()
        return int(c.rowcount or 0)


def delete_org_repo_records(repo_key: str) -> None:
    """删除组织自动仓库的本地托管记录（远端仓库删除由调用方负责）。

    Issue #2261：删组织后残留 managed_repos/user_repos 会让仓库在个人下拉里
    以「未归属」形态继续出现，并保留已失效 webhook，属孤儿数据。
    """
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()


def unbind_org_repo_local(repo_key: str, org_id: str) -> int:
    """把手工关联仓库退回「未归属」（不删远端、不删托管记录）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos SET org_id='', is_org_repo=0, linked_by='', linked_at='' "
            "WHERE repo_key=? AND org_id=? AND COALESCE(is_org_repo, 0)=0",
            (repo_key, org_id),
        )
        db.commit()
        return c.rowcount


def delete_org_scenes(org_id: str) -> int:
    """删除组织 provisioning 生成/归属该组织的场景行（Issue #2261）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE org_id=?", (org_id,))
        db.commit()
        return c.rowcount


def has_running_org_dispatch(org_id: str, repo_key: str,
                             statuses: tuple[str, ...]) -> bool:
    """判断是否有运行中的下派批次仍引用指定组织仓库（Issue #2258 替换保护）。"""
    if not statuses:
        return False
    placeholders = ",".join("?" for _ in statuses)
    with db_lock:
        c = db.cursor()
        row = c.execute(
            f"SELECT 1 FROM grape_t_org_dispatch WHERE org_id=? AND org_repo_key=? "
            f"AND status IN ({placeholders}) LIMIT 1",
            (org_id, repo_key, *statuses),
        ).fetchone()
        return bool(row)


def list_org_repos(org_id: str) -> list[dict]:
    """列出组织关联的托管仓库（附带目录渲染所需的 dev_scene/推荐配置）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT repo_key, repo_owner, repo_name, full_name, webhook_id, "
            "org_id, is_org_repo, linked_by, linked_at, created_at, "
            "COALESCE(dev_scene,'') AS dev_scene, "
            "COALESCE(scene_recommend_config,'') AS scene_recommend_config "
            "FROM managed_repos WHERE org_id=? ORDER BY full_name",
            (org_id,),
        )
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in rows]


def list_bindable_repos(org_id: str, user_id: str) -> list[dict]:
    """列出可关联到目标组织的托管仓库（该用户名下，且未被其它团队组织占用）。

    Issue #2263：关联仓库 UI 需要「此人名下可关联的仓库列表」供多选，避免用户
    手输 repo_key。候选口径与 bind_repo 的放行条件一致：
    - 该用户 user_repos 里有记录（自己托管/加入的仓库）；
    - org_id 为空/0（未归属）或属于个人组织（可从个人组织转入团队）；
    - 或已属于当前组织（列表里继续展示，前端标「已关联」）；
    - 排除组织自动仓库（is_org_repo=1 / organization.org_repo_key），它由
      provisioning 管理，不支持手工关联。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT m.repo_key, m.repo_owner, m.repo_name, m.full_name, m.webhook_id, "
            "m.org_id, COALESCE(m.is_org_repo, 0) AS is_org_repo, "
            "m.linked_by, m.linked_at, m.created_at "
            "FROM managed_repos m "
            "WHERE EXISTS (SELECT 1 FROM user_repos u WHERE u.repo_key=m.repo_key "
            "              AND u.user_id=? AND (u.is_join=1 OR u.is_admin=1)) "
            "AND (COALESCE(m.org_id,'') IN ('', '0') "
            "     OR m.org_id=? "
            "     OR m.org_id IN (SELECT id FROM organization WHERE is_personal=1)) "
            "AND COALESCE(m.is_org_repo, 0)=0 "
            "AND m.repo_key NOT IN (SELECT org_repo_key FROM organization "
            "                       WHERE org_repo_key IS NOT NULL AND org_repo_key<>'') "
            "ORDER BY m.full_name",
            (user_id, org_id),
        )
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in rows]


def count_org_repos(org_id: str) -> int:
    """统计组织当前占用的仓库配额。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT COUNT(*) FROM managed_repos WHERE org_id=?",
            (org_id,),
        ).fetchone()
        return int(row[0] or 0) if row else 0


def bind_managed_repo_to_org(repo_key: str, org_id: str, user_id: str,
                             is_org_repo: int = 0, *, max_repos: int = 0,
                             allowed_source_org_id: str = "") -> str:
    """Bind a hosted repo and enforce quota under a DB lease.

    Returns: bound / already_bound / conflict / quota / not_hosted / busy.
    """
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT COALESCE(org_id, 0), COALESCE(is_org_repo, 0) "
            "FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        ).fetchone()
    if not row:
        return "not_hosted"
    current_org = str(row[0] or "")
    current_is_org_repo = int(row[1] or 0)
    target_org_id = str(org_id)
    force_org_repo = bool(is_org_repo) and not current_is_org_repo
    if current_org == target_org_id and not force_org_repo:
        return "already_bound"
    if current_is_org_repo and current_org != target_org_id and current_org:
        return "protected"
    if current_is_org_repo and not is_org_repo and current_org != target_org_id:
        return "protected"
    if current_org and current_org != str(allowed_source_org_id):
        return "conflict"

    lock_key = f"org-repo-bind:{target_org_id}"
    if not acquire_lock_with_retry(
        lock_key,
        ttl=_ORG_REPO_BIND_LEASE_TTL,
        attempts=_ORG_REPO_BIND_LOCK_ATTEMPTS,
        interval=_ORG_REPO_BIND_LOCK_INTERVAL,
    ):
        return "busy"
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT COALESCE(org_id, 0), COALESCE(is_org_repo, 0) "
                "FROM managed_repos WHERE repo_key=?",
                (repo_key,),
            ).fetchone()
            if not row:
                return "not_hosted"
            latest_org = str(row[0] or "")
            latest_is_org_repo = int(row[1] or 0)
            force_org_repo = bool(is_org_repo) and not latest_is_org_repo
            if latest_org == target_org_id and not force_org_repo:
                return "already_bound"
            if latest_is_org_repo and latest_org != target_org_id and latest_org:
                return "protected"
            if latest_is_org_repo and not is_org_repo and latest_org != target_org_id:
                return "protected"
            if latest_org and latest_org != str(allowed_source_org_id):
                return "conflict"
            if max_repos > 0 and latest_org != target_org_id:
                count = c.execute(
                    "SELECT COUNT(*) FROM managed_repos WHERE org_id=?",
                    (target_org_id,),
                ).fetchone()[0]
                if int(count or 0) >= max_repos:
                    return "quota"
            c.execute(
                "UPDATE managed_repos "
                "SET org_id=?, is_org_repo=?, linked_by=?, linked_at=datetime('now') "
                "WHERE repo_key=? AND COALESCE(org_id, 0)=?",
                (target_org_id, int(bool(is_org_repo)), user_id, repo_key, latest_org),
            )
            if c.rowcount <= 0:
                db.rollback()
                return "conflict"
            db.commit()
            return "bound"
    finally:
        release_lock(lock_key)


def unbind_managed_repo_from_org(repo_key: str, org_id: str) -> str:
    """解绑组织的普通关联仓库；组织自动仓库由调用方提前拦截。

    派发任务复查与解绑写在同一事务内，避免 check-then-act 竞态。
    Returns: unbound / dispatch_running / protected / busy / not_found.
    """
    bind_lock_key = f"org-repo-bind:{org_id}"
    dispatch_lock_key = repo_dispatch_lease_key(repo_key)
    if not acquire_lock_with_retry(
        bind_lock_key,
        ttl=_ORG_REPO_BIND_LEASE_TTL,
        attempts=_ORG_REPO_BIND_LOCK_ATTEMPTS,
        interval=_ORG_REPO_BIND_LOCK_INTERVAL,
    ):
        return "busy"
    try:
        if not acquire_lock_with_retry(
            dispatch_lock_key,
            ttl=_REPO_DISPATCH_LEASE_TTL,
            attempts=_ORG_REPO_BIND_LOCK_ATTEMPTS,
            interval=_ORG_REPO_BIND_LOCK_INTERVAL,
        ):
            return "busy"
        try:
            with db_lock:
                c = db.cursor()
                row = c.execute(
                    "SELECT COALESCE(org_id, 0), COALESCE(is_org_repo, 0) "
                    "FROM managed_repos WHERE repo_key=?",
                    (repo_key,),
                ).fetchone()
                if not row or str(row[0] or "") != str(org_id):
                    db.rollback()
                    return "not_found"
                if int(row[1] or 0):
                    db.rollback()
                    return "protected"
                active = False
                try:
                    from src.domains.runtime import public as _runtime_public

                    active = _runtime_public.has_inflight_repo(repo_key)
                except Exception:
                    active = True
                if active:
                    db.rollback()
                    return "dispatch_running"
                c.execute(
                    "UPDATE managed_repos "
                    "SET org_id='', linked_by='', linked_at='' "
                    "WHERE repo_key=? AND org_id=? AND COALESCE(is_org_repo, 0)=0",
                    (repo_key, org_id),
                )
                if c.rowcount <= 0:
                    db.rollback()
                    return "not_found"
                db.commit()
                return "unbound"
        finally:
            release_lock(dispatch_lock_key)
    finally:
        release_lock(bind_lock_key)


def transfer_managed_repo_to_org(repo_key: str, source_org_id: str,
                                 target_org_id: str, user_id: str,
                                 *, max_repos: int = 0,
                                 include_rows: bool = False) -> str | tuple[str, int]:
    """迁移普通关联仓库及其 org_id 关联数据。

    配额判定与写入共用 ``org-repo-bind:{target_org_id}`` 租约，和 bind 串行化，
    避免并发 transfer 突破 ``max_repos``。
    Returns: transferred / quota / busy / protected / conflict / not_found.
    """
    # 分域口径（P3-5f-2，与 repos/migrate 同源）：mgmt 表本地改，runtime 表由组合原语改。
    # 脏库单 DSN 部署时 runtime_lib(foreign_only=True) 返回 None，再退回本地改。
    local_tables = (
        "devops_pipeline_scene", "events", "agent_shared_knowledge",
        "scene_market", "session_api_tokens",
    )
    runtime_tables = (
        "issue", "agent_tasks", "agent_runtimes", "chat_conversations",
        "devops_agent_session", "runtime_credentials",
    )
    lock_key = f"org-repo-bind:{str(target_org_id)}"
    if not acquire_lock_with_retry(
        lock_key,
        ttl=_ORG_REPO_BIND_LEASE_TTL,
        attempts=_ORG_REPO_BIND_LOCK_ATTEMPTS,
        interval=_ORG_REPO_BIND_LOCK_INTERVAL,
    ):
        return ("busy", 0) if include_rows else "busy"

    def _result(status: str, rows: int = 0):
        return (status, rows) if include_rows else status

    try:
        with db_lock:
            c = db.cursor()
            updated = 0
            try:
                row = c.execute(
                    "SELECT COALESCE(org_id, 0), COALESCE(is_org_repo, 0) "
                    "FROM managed_repos WHERE repo_key=?",
                    (repo_key,),
                ).fetchone()
                if (
                    not row
                ):
                    db.rollback()
                    return _result("not_found")
                if int(row[1] or 0):
                    db.rollback()
                    return _result("protected")
                if str(row[0] or "") != str(source_org_id):
                    db.rollback()
                    return _result("conflict")
                if max_repos > 0:
                    count = c.execute(
                        "SELECT COUNT(*) FROM managed_repos WHERE org_id=?",
                        (target_org_id,),
                    ).fetchone()[0]
                    if int(count or 0) >= max_repos:
                        db.rollback()
                        return _result("quota")
                c.execute(
                    "UPDATE managed_repos SET org_id=?, linked_by=?, linked_at=datetime('now') "
                    "WHERE repo_key=? AND org_id=? AND COALESCE(is_org_repo, 0)=0",
                    (target_org_id, user_id, repo_key, source_org_id),
                )
                if c.rowcount <= 0:
                    db.rollback()
                    return _result("conflict")
                updated += c.rowcount
                for table in local_tables:
                    try:
                        c.execute(
                            f"UPDATE {table} SET org_id=? WHERE repo_key=? AND org_id=?",
                            (target_org_id, repo_key, source_org_id),
                        )
                        if c.rowcount and c.rowcount > 0:
                            updated += c.rowcount
                    except Exception as exc:
                        log.warning(f"transfer_managed_repo_to_org: update {table} skipped: {exc}")
                db.commit()
                # runtime 域表：拆分部署下必须走组合原语（同库部署原语返回 None，退回本地改）
                _rt_foreign = None
                try:
                    from src.kernel.domain_read import runtime_lib as _rt_lib
                    _rt_foreign = _rt_lib(foreign_only=True)
                except Exception as exc:
                    log.warning(f"transfer_managed_repo_to_org: runtime_lib unavailable: {exc}")
                if _rt_foreign is not None:
                    try:
                        _res = _rt_foreign.reassign_repo_org_id(repo_key, target_org_id)
                        updated += sum(int(v or 0) for v in (_res or {}).values())
                        log.info(f"transfer_managed_repo_to_org: runtime 侧 org_id 改写 {_res}")
                    except Exception as exc:
                        log.warning(f"transfer_managed_repo_to_org: runtime 侧改写失败: {exc}")
                else:
                    for table in runtime_tables:
                        try:
                            c.execute(
                                f"UPDATE {table} SET org_id=? WHERE repo_key=? AND org_id=?",
                                (target_org_id, repo_key, source_org_id),
                            )
                            if c.rowcount and c.rowcount > 0:
                                updated += c.rowcount
                        except Exception as exc:
                            log.warning(f"transfer_managed_repo_to_org: update {table} skipped: {exc}")
                    db.commit()
                return _result("transferred", updated)
            except Exception:
                db.rollback()
                raise
    finally:
        release_lock(lock_key)
