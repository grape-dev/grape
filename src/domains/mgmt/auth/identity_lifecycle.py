"""external_identities 身份行生命周期服务（mgmt 域内，Issue #3054 P2）。

为什么单独成类
--------------
`grape_account_db.py` 已近 2000 行，而「第三方身份行」的状态机（1=活跃、
0=解绑、4=token 过期）是一套独立闭环：**登录决策 / 解绑 / 换绑 / 票据**
都在这套状态机上做条件更新，任何一处只认 status=1 就会把用户锁死
（Issue #3054 的「四不通」）。把这些语义收进一个类，调用方（路由、
账号模块）只做转发，避免 SQL/状态判断散落到路由里。

结构约束（实锤，2026-11-17 基线）
--------------------------------
`external_identities` 同时有 `UNIQUE(provider, external_id)` 与
`UNIQUE(grape_user_id, provider)` ⇒ **一人一平台只有一行**。因此：
- 换绑不能把旧 external_id 另起一行存「解绑态」；旧 id 只能随行写进
  `metadata.tombstones`（墓碑账本）；
- status=4 的行解绑时把 external_id 换成行级占位值（`unbound:<row-id>`），
  真正释放 `UNIQUE(provider, external_id)`，他人可立即重新绑定；
- 重绑/换绑/过期后重登一律**复用本行**（UPDATE），绝不 INSERT——否则唯一键
  冲突被吞成 False，上层回滚建号，产出 status=3 孤儿账号。

依赖注入（便于单测与将来换仓储实现）
------------------------------------
- ``repository``  —— 身份行数据访问（默认 ``IdentityAdminRepository``）；
- ``unit_of_work`` —— 事务边界（默认 kernel ``uow``）；
- ``cipher``       —— token 加解密（默认 ``credential_crypto``）；
- ``db_access``    —— 票据表裸 SQL 句柄 ``() -> (db, db_lock)``（默认 mgmt 主库）；
- ``clock``        —— 秒级时间源（票据 TTL 用）；
- ``log``          —— 日志器。

状态机事件（结构化日志前缀，观测侧按前缀抓）：
``identity_status4_recovered`` / ``identity_status4_unbound`` /
``identity_tombstone_restored`` / ``identity_tombstone_rebind``。
"""
from __future__ import annotations

import json
import time

from src.kernel.config import log

from src.domains.mgmt.auth.identity_unbind import (
    IdentityUnbindGateway,
    statement_for_backend,
)

TOMBSTONE_KEY = "tombstones"
VOID_EXTERNAL_ID_PREFIX = "unbound:"
TOMBSTONE_KEEP = 10          # 墓碑账本最多保留最近 10 条（一平台一行，够回溯）
TICKET_TTL = 600             # 一次性 binding_ticket：10 分钟

ACTIVE_STATUS = 1
UNBOUND_STATUS = 0
EXPIRED_STATUS = 4

# 登录/绑定判定用的状态口径
LIVE_STATUSES = (ACTIVE_STATUS, EXPIRED_STATUS)          # 「仍然绑定着」
ANY_STATUSES = (UNBOUND_STATUS, ACTIVE_STATUS, EXPIRED_STATUS)


def _default_repository():
    from src.kernel.persistence.repositories import identity_admin_repository
    return identity_admin_repository


def _default_uow():
    from src.kernel.persistence.uow import uow
    return uow


def _default_cipher():
    from src.domains.mgmt.auth import credential_crypto
    return credential_crypto


def _default_db_access():
    """票据表在 mgmt 库（configs/table_ownership.json: grape_mgmt_db）。

    延迟导入避免与 grape_account_db 形成模块级循环（后者在顶层 import 本服务）。
    """
    from src.domains.mgmt.auth import grape_account_db as _gad
    return _gad._mg_db, _gad.db_lock


def _identity_meta(raw: str) -> dict:
    """解析 metadata；坏 JSON 视为空账本（绝不让墓碑账本阻断登录）。"""
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def is_void_external_id(external_id: str) -> bool:
    return str(external_id or "").startswith(VOID_EXTERNAL_ID_PREFIX)


def void_external_id(identity_id: str) -> str:
    """释放 UNIQUE(provider, external_id) 的行级占位值（unique per row）。"""
    return f"{VOID_EXTERNAL_ID_PREFIX}{identity_id}"


class TombstoneStore:
    """墓碑账本（独立表 grape_t_external_identity_tombstones，UNIQUE(provider, external_id)）。

    为什么独立于 metadata（Issue #3054 QA）：metadata JSON 不可索引（每次未命中身份的
    登录要扫该 provider 全量行 + 逐行解析）、无唯一约束（同一 external_id 可在两个用户
    台账里同时存在，遍历命中第一条就可能登进错账号）、只保留最近 N 条（第 N+1 次换绑后
    最早那个账号回登会新建第二个 Grape 号）。

    过渡策略：写入双写（表 + metadata 最近 N 条），读取先查表、miss 再扫 metadata ——
    因此存量数据不需要一次性回填完毕也能生效。
    """

    TABLE = "grape_t_external_identity_tombstones"

    @staticmethod
    def _db():
        from src.kernel.session_manager import db
        return db

    def record(self, provider: str, external_id: str, grape_user_id: str,
               reason: str = "", now: str = "") -> bool:
        """登记一条墓碑（幂等：同一 provider+external_id 只保留一行，时间取最早）。

        失败只记日志，不影响调用方主事务（墓碑是观测/追溯用途）。
        """
        ext = str(external_id or "").strip()
        if not provider or not ext:
            return False
        try:
            import time as _time

            from src.kernel.id_gen import uuid7_str
            from src.kernel.session_manager import db_lock

            stamp = now or _time.strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                c = self._db().cursor()
                c.execute(f"SELECT id, unbound_at FROM {self.TABLE} WHERE provider=? AND external_id=?",
                          (provider, ext))
                row = c.fetchone()
                if row:
                    # 已存在：保留最早时间（墓碑语义是「何时开始可用」）
                    if not row[1]:
                        c.execute(f"UPDATE {self.TABLE} SET unbound_at=? WHERE id=?", (stamp, row[0]))
                        self._db().commit()
                    return True
                c.execute(
                    f"INSERT INTO {self.TABLE} (id, provider, external_id, grape_user_id, unbound_at, reason) "
                    f"VALUES (?,?,?,?,?,?)",
                    (uuid7_str(), provider, ext, str(grape_user_id or ""), stamp, reason or ""))
                self._db().commit()
                return True
        except Exception as e:  # noqa: BLE001  墓碑写入失败不阻断解绑/注销
            log.warning(f"tombstone record failed provider={provider} ext={ext}: {e}")
            return False

    def lookup(self, provider: str, external_id: str) -> "dict | None":
        """按 provider+external_id 精确查墓碑（O(1) 唯一索引）。"""
        ext = str(external_id or "").strip()
        if not provider or not ext:
            return None
        try:
            from src.kernel.session_manager import db_lock

            with db_lock:
                c = self._db().cursor()
                c.execute(f"SELECT grape_user_id, unbound_at, reason FROM {self.TABLE} "
                          f"WHERE provider=? AND external_id=?", (provider, ext))
                row = c.fetchone()
            if not row:
                return None
            return {"grape_user_id": str(row[0] or ""), "unbound_at": str(row[1] or ""),
                    "reason": str(row[2] or "")}
        except Exception as e:  # noqa: BLE001  表未建/不可读 → 交给调用方走 metadata 兜底
            log.debug(f"tombstone lookup failed provider={provider}: {e}")
            return None


class IdentityLifecycleService:
    """身份行状态机（status=4 恢复/解绑释放/换绑墓碑/一次性票据）。"""

    def __init__(self, repository=None, unit_of_work=None, cipher=None,
                 db_access=None, clock=None, log=None):
        self._repo_injected = repository is not None
        self._repo = repository or _default_repository()
        self._uow = unit_of_work or _default_uow()
        self._cipher = cipher
        self._db_access = db_access or _default_db_access
        self._clock = clock or (lambda: time.time())
        if log is None:
            from src.kernel.config import log as _log
            log = _log
        self._log = log

    # ------------------------------------------------------------- 内部工具
    def _key(self):
        if self._cipher is None:
            self._cipher = _default_cipher()
        return self._cipher

    def _encrypt(self, plain: str) -> str:
        return self._key().encrypt(plain) if plain else ""

    def _decrypt(self, cipher_text: str) -> str:
        """解密失败回落原值（历史明文行兼容）；与 _safe_decrypt 同口径。"""
        raw = cipher_text or ""
        if not raw:
            return ""
        try:
            return self._key().decrypt(raw)
        except Exception:
            return raw

    def _now_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _expires_at(seconds: int) -> str:
        if not seconds:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + seconds))

    # ------------------------------------------------------------- 墓碑账本
    def tombstones(self, raw_meta: str) -> list:
        lst = _identity_meta(raw_meta).get(TOMBSTONE_KEY)
        if not isinstance(lst, list):
            return []
        return [t for t in lst
                if isinstance(t, dict) and str(t.get("external_id") or "").strip()]

    def append_tombstone(self, raw_meta: str, old_external_id: str,
                         reason: str, now: str = "") -> str:
        """把旧平台账号写进本行墓碑账本（去重同 id，保留最近 N 条）。"""
        old = str(old_external_id or "").strip()
        if not old or is_void_external_id(old):
            return raw_meta or "{}"
        meta = _identity_meta(raw_meta)
        lst = [t for t in self.tombstones(raw_meta)
               if str(t.get("external_id")) != old]
        lst.append({"external_id": old, "unbound_at": now or self._now_str(),
                    "reason": reason})
        meta[TOMBSTONE_KEY] = lst[-TOMBSTONE_KEEP:]
        return json.dumps(meta, ensure_ascii=False)

    # ------------------------------------------------------- 注销级联（写）
    def release_on_deactivation(self, cursor, grape_user_id: str, now: str = "") -> dict:
        """账号注销级联：失效身份行**并释放** UNIQUE(provider, external_id)。

        Issue #3054：原实现只置 status=0，external_id 原样保留 → 该第三方账号在
        冷静期 30 天内对所有用户不可绑（撞唯一键），与原需求「不得留 owner 已注销
        仍占键」冲突。这里改用解绑同口径：旧值写进墓碑账本，external_id 改
        ``void:<行 id>``。原用户在冷静期内重新登录时按墓碑反查回原账号。

        返回 ``{"released": n}``；调用方负责 commit。
        """
        now = now or self._now_str()
        cursor.execute(
            "SELECT id, external_id, COALESCE(metadata,'') FROM external_identities "
            "WHERE grape_user_id=? AND status IN (1,4)", (grape_user_id,))
        rows = cursor.fetchall()
        released = 0
        for row in rows:
            row_id, ext, meta = row[0], row[1], row[2]
            new_meta = self.append_tombstone(meta, ext, "account_soft_delete", now)
            # 双写：独立表为真源（可索引/唯一），metadata 保留最近 N 条作降级读。
            # 失败只记日志（墓碑是追溯用途，不能阻断注销）。
            try:
                row_provider = str(cursor.execute(
                    "SELECT provider FROM external_identities WHERE id=?", (row_id,)).fetchone()[0] or "")
            except Exception:  # noqa: BLE001
                row_provider = ""
            if row_provider:
                TombstoneStore().record(row_provider, ext, grape_user_id,
                                        "account_soft_delete", now)
            cursor.execute(
                "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
                "token_expires_at='', external_id=?, metadata=?, updated_at=? WHERE id=?",
                (void_external_id(row_id), new_meta, now, row_id))
            released += 1
        return {"released": released}

    # ------------------------------------------------------------- 读
    def get(self, provider: str, external_id: str,
            statuses=LIVE_STATUSES) -> "dict | None":
        """按平台 ID 查身份行（含 token，已解密）。

        默认 ``LIVE_STATUSES``：status=1 与 status=4 都算「已绑定」——
        status=4 只是 token 过期，重新授权即恢复，不能当作未绑定。
        """
        try:
            with self._uow() as session:
                dto = self._repo.get_by_platform(session, provider, external_id,
                                                 statuses=tuple(statuses))
            if not dto:
                return None
            return {
                "id": dto.id, "grape_user_id": dto.grape_user_id,
                "provider": dto.provider, "external_id": dto.external_id,
                "external_login": dto.external_login,
                "external_name": dto.external_name,
                "external_avatar": dto.external_avatar,
                "status": dto.status,
                "access_token": self._decrypt(dto.access_token or ""),
                "refresh_token": self._decrypt(dto.refresh_token or ""),
            }
        except Exception as e:
            self._log.warning(f"Get external identity failed: {e}")
            return None

    def get_any_status(self, provider: str, external_id: str) -> "dict | None":
        """任意状态（1/0/4）的归属查询：用于绑定冲突与票据判定。"""
        try:
            with self._uow() as session:
                dto = self._repo.get_by_platform_any_status(session, provider, external_id)
            if not dto:
                return None
            return {"id": dto.id, "grape_user_id": dto.grape_user_id,
                    "status": dto.status}
        except Exception as e:
            self._log.warning(f"Get external identity any status failed: {e}")
            return None

    def find_tombstone(self, provider: str, external_id: str) -> "dict | None":
        """按墓碑反查归属行。

        Issue #3054：先查独立墓碑表（UNIQUE(provider, external_id)，O(1)）；表未命中
        （存量未回填/表未建）再退回 metadata 扫描 —— 渐进收敛，不依赖一次性回填。
        """
        hit = TombstoneStore().lookup(provider, external_id)
        if hit and hit.get("grape_user_id"):
            # 表命中即定论（唯一索引）；展示字段允许为空，调用方只依赖 grape_user_id。
            return {"grape_user_id": hit["grape_user_id"], "provider": provider,
                    "external_id": external_id, "external_login": "",
                    "external_name": "", "external_avatar": "", "status": 0,
                    "unbound_at": hit.get("unbound_at", "")}

        """按墓碑反查归属行：换绑/过期解绑后旧平台账号回登用。"""
        target = str(external_id or "").strip()
        if not target:
            return None
        try:
            with self._uow() as session:
                rows = self._repo.list_by_provider_statuses(session, provider,
                                                            statuses=ANY_STATUSES)
            for dto in rows:
                for tomb in self.tombstones(getattr(dto, "metadata_json", "")):
                    if str(tomb.get("external_id")) == target:
                        return {"id": dto.id, "grape_user_id": dto.grape_user_id,
                                "provider": dto.provider,
                                "external_id": dto.external_id,
                                "external_login": dto.external_login,
                                "external_name": dto.external_name,
                                "external_avatar": dto.external_avatar,
                                "status": dto.status,
                                "unbound_at": tomb.get("unbound_at", "")}
            return None
        except Exception as e:
            self._log.warning(f"find_identity_by_tombstone failed: {e}")
            return None

    # ------------------------------------------------------------- 写：绑定
    def bind(self, grape_user_id: str, provider: str, external_id: str,
             external_login: str = "", external_name: str = "",
             external_avatar: str = "", access_token: str = "",
             refresh_token: str = "", expires_in: int = 0, reason: str = "rebind") -> bool:
        """创建 / 恢复 / 换绑绑定（一人一平台一行，换绑旧 id 进墓碑）。

        三分支统一走「先查目标归属 → 复用自有行 → 否则 INSERT」：
        1. 目标身份归他人（任意状态）→ False（合法转移只走票据/transfer）；
        2. 自有行存在（status 0/1/4）→ 原地更新为 status=1，旧 id 记墓碑；
        3. 无自有行 → INSERT。
        """
        now = self._now_str()
        expires_at = self._expires_at(expires_in)
        try:
            at_cipher, rt_cipher = self._encrypt(access_token), self._encrypt(refresh_token)
            with self._uow() as session:
                holder = self._repo.get_by_platform_any_status(session, provider,
                                                               str(external_id))
                if (holder is not None
                        and str(getattr(holder, "grape_user_id", "") or "") != str(grape_user_id)):
                    self._log.warning(
                        "identity bind: target identity owned by another user "
                        "provider=%s external_id=%s owner=%s caller=%s",
                        provider, external_id,
                        getattr(holder, "grape_user_id", ""), grape_user_id)
                    return False
                own = self._repo.get_by_user_provider(session, grape_user_id, provider,
                                                      statuses=ANY_STATUSES)
                if own is not None:
                    prev_ext = str(getattr(own, "external_id", "") or "")
                    meta_json = getattr(own, "metadata_json", "") or "{}"
                    if holder is not None and str(holder.id) != str(own.id):
                        # 同用户同平台的历史脏行：吸进墓碑后硬删，释放唯一键位
                        meta_json = self.append_tombstone(
                            meta_json, getattr(holder, "external_id", ""), "replaced", now)
                        self._repo.delete_by_id(session, holder.id)
                    if prev_ext and prev_ext != str(external_id):
                        meta_json = self.append_tombstone(meta_json, prev_ext, reason, now)
                        self._log.info(
                            "identity_tombstone_rebind: provider=%s user=%s external_id %s -> %s",
                            provider, grape_user_id, prev_ext, external_id)
                    self._repo.update_by_id(
                        session, own.id, external_id, external_login,
                        external_name, external_avatar or (own.external_avatar or ""),
                        at_cipher, rt_cipher, expires_at, now, status=ACTIVE_STATUS)
                    if meta_json != (getattr(own, "metadata_json", "") or "{}"):
                        self._repo.update_metadata_by_id(session, own.id, meta_json)
                    return True
                self._repo.create(session, grape_user_id, provider, external_id,
                                  external_login, external_name, external_avatar or "",
                                  at_cipher, rt_cipher, expires_at,
                                  status=ACTIVE_STATUS, created_at=now)
            return True
        except Exception as e:
            self._log.warning(f"Create external identity failed: {e}")
            return False

    def restore_from_tombstone(self, grape_user_id: str, provider: str, external_id: str,
                               access_token: str = "", refresh_token: str = "",
                               external_name: str = "", external_avatar: str = "",
                               expires_in: int = 0) -> bool:
        """凭墓碑把旧平台账号重新绑回原账号（不新建第二个 Grape 号）。"""
        now = self._now_str()
        expires_at = self._expires_at(expires_in)
        try:
            with self._uow() as session:
                holder = self._repo.get_by_platform_any_status(session, provider,
                                                               str(external_id))
                if (holder is not None
                        and str(getattr(holder, "grape_user_id", "") or "") != str(grape_user_id)):
                    self._log.warning(
                        "restore_from_tombstone: target now owned by another user "
                        "provider=%s external_id=%s owner=%s caller=%s",
                        provider, external_id,
                        getattr(holder, "grape_user_id", ""), grape_user_id)
                    return False
                own = self._repo.get_by_user_provider(session, grape_user_id, provider,
                                                      statuses=ANY_STATUSES)
                if own is None:
                    return False
                prev_ext = str(getattr(own, "external_id", "") or "")
                meta_json = getattr(own, "metadata_json", "") or "{}"
                if holder is not None and str(holder.id) != str(own.id):
                    meta_json = self.append_tombstone(
                        meta_json, getattr(holder, "external_id", ""), "replaced", now)
                    self._repo.delete_by_id(session, holder.id)
                if prev_ext and prev_ext != str(external_id):
                    meta_json = self.append_tombstone(meta_json, prev_ext,
                                                     "rebind_reverted", now)
                self._repo.update_by_id(
                    session, own.id, external_id,
                    getattr(own, "external_login", "") or "",
                    external_name or (own.external_name or ""),
                    external_avatar or (own.external_avatar or ""),
                    self._encrypt(access_token) or (own.access_token or ""),
                    self._encrypt(refresh_token) or (own.refresh_token or ""),
                    expires_at or (own.token_expires_at or ""),
                    updated_at=now, status=ACTIVE_STATUS)
                if meta_json != (getattr(own, "metadata_json", "") or "{}"):
                    self._repo.update_metadata_by_id(session, own.id, meta_json)
            self._log.info("identity_tombstone_restored: provider=%s user=%s external_id=%s",
                           provider, grape_user_id, external_id)
            return True
        except Exception as e:
            self._log.warning(f"restore_from_tombstone failed: {e}")
            return False

    def update_tokens(self, grape_user_id: str, provider: str,
                      access_token: str = "", refresh_token: str = "",
                      expires_in: int = 0, external_name: str = "",
                      external_avatar: str = "") -> bool:
        """刷新 token；一次新授权即把 status=4 恢复为 status=1。"""
        now = self._now_str()
        try:
            with self._uow() as session:
                rows = self._repo.list_by_user_statuses(session, grape_user_id,
                                                        statuses=LIVE_STATUSES)
                target = next((d for d in rows if d.provider == provider), None)
                if target is None:
                    return False
                was_expired = int(target.status or 0) == EXPIRED_STATUS
                self._repo.update_by_id(
                    session, target.id, target.external_id, target.external_login,
                    external_name or target.external_name,
                    external_avatar or target.external_avatar,
                    self._encrypt(access_token) if access_token else target.access_token,
                    self._encrypt(refresh_token) if refresh_token else target.refresh_token,
                    self._expires_at(expires_in) if expires_in else target.token_expires_at,
                    updated_at=now, status=ACTIVE_STATUS)
            if was_expired:
                self._log.info("identity_status4_recovered: provider=%s user=%s",
                               provider, grape_user_id)
            return True
        except Exception as e:
            self._log.warning(f"Update external identity tokens failed: {e}")
            return False

    # ------------------------------------------------------------- 写：解绑
    def unbind(self, grape_user_id: str, provider: str, require_keep_one: bool = True,
               user_has_password: "bool | None" = None) -> bool:
        """软删绑定（status=0，清 token）。

        - status=4 与 status=1 同等可解绑（此前只认 1 → 404 死锁）；
        - status=4 解绑额外做两件事：旧 external_id 记墓碑 + 行 id 换成占位值，
          释放 UNIQUE(provider, external_id)（他人可直接重绑）；
        - status=1 解绑保持历史口径（status=0 保留 external_id，供票据/回登识别）；
        - P3-1：「至少保留一种登录方式」的判据在**同一条 UPDATE** 内求值（原读-判-写
          会让无密码账号并发解绑两个平台时双过 → 锁死账号），并发由语句原子性裁决，
          rowcount=0 即拒绝。
        """
        # 注：user_has_password 仅为签名兼容保留——默认仓储路径的密码判据改在
        # SQL 内求值，避免调用方读侧与写入之间的竞态（P3-1）。
        if self._repo_injected:
            # 依赖注入场景（测试替身/自定义仓储）：保持 ORM 路径，单语句原子
            # 判据只对默认仓储（生产库）生效。
            return self._unbind_via_repository(
                grape_user_id, provider, require_keep_one, user_has_password)
        now = self._now_str()
        db, lock = self._db_access()
        gateway = IdentityUnbindGateway(db, lock)
        try:
            target = gateway.load_target(grape_user_id, provider)
            if target is None:
                return False      # 无可用绑定（等价 rowcount=0）
            new_ext, new_meta = target.external_id, target.metadata or "{}"
            if target.is_expired:
                new_meta = self.append_tombstone(new_meta, target.external_id,
                                                 "unbind_expired", now)
                new_ext = void_external_id(target.identity_id)
            statement = statement_for_backend(
                target, grape_user_id, now=now, keep_one=require_keep_one,
                new_external_id=new_ext, new_metadata=new_meta)
            affected = gateway.execute(statement)
            if affected > 0:
                if target.is_expired:
                    # 双写：独立表（可索引/唯一）是墓碑真源，metadata JSON 只留最近
                    # N 条作降级读。注销级联早已双写，解绑这条此前漏了 → MySQL 上
                    # 「status=4 解绑释放了键但墓碑表没有记录」，追溯链断裂（QA 实锤）。
                    try:
                        TombstoneStore().record(provider, target.external_id,
                                                str(grape_user_id), "unbind_expired", now)
                    except Exception as exc:  # noqa: BLE001 - 墓碑只用于追溯，不得阻断解绑
                        self._log.warning(
                            f"tombstone record failed (unbind): provider={provider} "
                            f"external_id={target.external_id} error={exc}")
                    self._log.info(
                        "identity_status4_unbound: provider=%s user=%s released_key external_id=%s",
                        provider, grape_user_id, target.external_id)
                return True
            if require_keep_one:
                self._log.warning(
                    "unbind_atomic_rejected: user=%s provider=%s "
                    "reason=no_active_row_or_last_login_method", grape_user_id, provider)
            return False
        except Exception as e:
            self._log.warning(f"Delete external identity failed: {e}")
            return False

    def _unbind_via_repository(self, grape_user_id: str, provider: str,
                               require_keep_one: bool = True,
                               user_has_password: "bool | None" = None) -> bool:
        """ORM 仓储路径（仅注入场景）：读-判-写。

        默认仓储走 `unbind` 里的单条条件 UPDATE（P3-1 并发裁决）；本方法保留给
        依赖注入的仓储替身，行为与 P2 交付一致（status=4 墓碑 + 释放唯一键）。
        """
        now = self._now_str()
        try:
            with self._uow() as session:
                rows = self._repo.list_by_user_statuses(session, grape_user_id,
                                                        statuses=LIVE_STATUSES)
                if require_keep_one and len(rows) <= 1 and not user_has_password:
                    return False      # 拒绝删除：会失去所有登录方式
                target = next((d for d in rows if d.provider == provider), None)
                if target is None:
                    return False      # 无可用绑定（等价 rowcount=0）
                ext = str(target.external_id or "")
                status = int(target.status or 0)
                new_ext = ext
                meta_json = getattr(target, "metadata_json", "") or "{}"
                if status == EXPIRED_STATUS:
                    meta_json = self.append_tombstone(meta_json, ext, "unbind_expired", now)
                    new_ext = void_external_id(target.id)
                self._repo.update_by_id(
                    session, target.id, new_ext, target.external_login,
                    target.external_name, target.external_avatar, "", "", "",
                    updated_at=now, status=UNBOUND_STATUS)
                if meta_json != (getattr(target, "metadata_json", "") or "{}"):
                    self._repo.update_metadata_by_id(session, target.id, meta_json)
            if status == EXPIRED_STATUS:
                # 与默认仓储路径同口径双写独立墓碑表（注入场景也要一致）
                try:
                    TombstoneStore().record(provider, ext, str(grape_user_id),
                                            "unbind_expired", now)
                except Exception as exc:  # noqa: BLE001
                    self._log.warning(f"tombstone record failed (unbind-repo): {exc}")
                self._log.info(
                    "identity_status4_unbound: provider=%s user=%s released_key external_id=%s",
                    provider, grape_user_id, ext)
            return True
        except Exception as e:
            self._log.warning(f"Delete external identity failed: {e}")
            return False

    # ------------------------------------------------------------- 过期标记
    def mark_expired(self, grape_user_id: str, provider: str) -> bool:
        """标记 status=4（token 过期），保留 token 供用户查看原账号。"""
        db, lock = self._db_access()
        try:
            with lock:
                cursor = db.cursor()
                cursor.execute(
                    "UPDATE external_identities SET status=? "
                    "WHERE grape_user_id=? AND provider=? AND status=?",
                    (EXPIRED_STATUS, grape_user_id, provider, ACTIVE_STATUS))
                db.commit()
                if cursor.rowcount > 0:
                    self._log.info("mark_identity_expired: %s token expired for user=%s",
                                   provider, grape_user_id)
                    return True
                return False
        except Exception as e:
            self._log.warning(f"mark_identity_expired failed: {e}")
            return False

    def is_expired(self, grape_user_id: str, provider: str) -> bool:
        db, lock = self._db_access()
        try:
            with lock:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT status FROM external_identities "
                    "WHERE grape_user_id=? AND provider=? LIMIT 1",
                    (grape_user_id, provider))
                row = cursor.fetchone()
                return bool(row and row[0] == EXPIRED_STATUS)
        except Exception:
            return False

    # ------------------------------------------------------------- 一次性票据
    def issue_ticket(self, initiating_user_id: str, provider: str, external_id: str,
                     external_login: str = "", external_name: str = "",
                     external_avatar: str = "", original_user_id: str = "") -> "str | None":
        """发放一次性转移票据（单次消费 / 10 分钟 TTL / 绑 session 与目标 external_id）。

        票据不存 token（凭据不出库）；转移成功后新绑定 token 留空，下次授权补。
        """
        import secrets
        ticket = secrets.token_urlsafe(32)
        db, lock = self._db_access()
        now_epoch = self._clock()
        sql = ("INSERT INTO binding_tickets "
               "(ticket, initiating_user_id, provider, external_id, "
               "external_login, external_name, external_avatar, "
               "original_user_id, created_at, expires) VALUES (?,?,?,?,?,?,?,?,?,?)")
        params = (ticket, initiating_user_id, provider, external_id,
                  external_login, external_name, external_avatar,
                  original_user_id, now_epoch, now_epoch + TICKET_TTL)
        try:
            with lock:
                cursor = db.cursor()
                cursor.execute("DELETE FROM binding_tickets WHERE expires < ?", (now_epoch,))
                try:
                    cursor.execute(sql, params)
                except Exception as exc:
                    from src.kernel.session_manager import _is_constraint_error
                    if not _is_constraint_error(exc):
                        raise
                    cursor.execute("DELETE FROM binding_tickets WHERE ticket=?", (ticket,))
                    cursor.execute(sql, params)
                db.commit()
            return ticket
        except Exception as e:
            self._log.warning(f"save_binding_ticket failed: {e}")
            return None

    def validate_ticket(self, ticket: str, initiating_user_id: str,
                        consume: bool = True) -> "dict | None":
        """校验票据；consume=False 只校验（业务成功后再真正消费）。"""
        db, lock = self._db_access()
        row = None
        try:
            with lock:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT initiating_user_id, provider, external_id, "
                    "external_login, external_name, external_avatar, "
                    "original_user_id, expires FROM binding_tickets WHERE ticket=?",
                    (ticket,))
                row = cursor.fetchone()
                if row and consume:
                    cursor.execute("DELETE FROM binding_tickets WHERE ticket=?", (ticket,))
                    db.commit()
            if not row:
                return None
            if self._clock() > row[7]:
                return None
            # D105：grape_accounts.id 恒 TEXT、票据列归一后也是 text——双端 str()
            if str(row[0]) != str(initiating_user_id):
                self._log.warning(
                    f"binding_ticket user mismatch: ticket_user={row[0]}, "
                    f"session_user={initiating_user_id}")
                return None
            return {"provider": row[1], "external_id": row[2],
                    "external_login": row[3], "external_name": row[4],
                    "external_avatar": row[5], "original_user_id": row[6]}
        except Exception as e:
            self._log.warning(f"validate_binding_ticket failed: {e}")
            return None

    def consume_ticket(self, ticket: str) -> bool:
        """消费票据（删除）。幂等：不存在返回 False。"""
        db, lock = self._db_access()
        try:
            with lock:
                cursor = db.cursor()
                cursor.execute("DELETE FROM binding_tickets WHERE ticket=?", (ticket,))
                db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            self._log.warning(f"consume_binding_ticket failed: {e}")
            return False

    def pending_ticket(self, initiating_user_id: str) -> "dict | None":
        """当前用户最近一条待转移票据（只读，不消费；popup 模式用）。"""
        db, lock = self._db_access()
        try:
            with lock:
                cursor = db.cursor()
                now_epoch = self._clock()
                cursor.execute("DELETE FROM binding_tickets WHERE expires < ?", (now_epoch,))
                cursor.execute(
                    "SELECT ticket, provider, external_login, external_name, "
                    "external_avatar, original_user_id, expires "
                    "FROM binding_tickets WHERE initiating_user_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (initiating_user_id,))
                row = cursor.fetchone()
                db.commit()
            if not row:
                return None
            if self._clock() > row[6]:
                return None
            return {"binding_ticket": row[0], "provider": row[1],
                    "external_login": row[2], "external_name": row[3],
                    "external_avatar": row[4], "original_user_id": row[5]}
        except Exception as e:
            self._log.warning(f"get_pending_transfer_context failed: {e}")
            return None


identity_lifecycle = IdentityLifecycleService()