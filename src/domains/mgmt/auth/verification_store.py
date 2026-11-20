"""验证码仓储——``verification_codes`` 表的唯一 SQL 落点。

归属 mgmt 域：本仓库只读写 ``verification_codes`` 一张表，不跨域、不碰其它
表；应用侧（验证码领域服务）不得再出现该表的 SQL 字面量。

连接来源通过 ``connection_provider`` 注入（返回 ``(db, db_lock)``），这样服务层
可以把「当前连接句柄」保持在自己的模块命名空间里，便于测试替换与后续换库；
默认回退到 ``src.kernel.session_manager``，且是**惰性** import，避免模块间循环依赖。
"""


class VerificationStoreError(Exception):
    """仓储层错误（连接/SQL/提交失败）。业务语义由服务层翻译。"""


def _default_connection():
    from src.kernel.session_manager import db, db_lock
    return db, db_lock


class VerificationCodeStore:
    """``verification_codes`` 表仓储。

    行语义：``code_hash=''`` 且 ``used=1`` 的是**影子记录**（只用于限流计数，
    不可用于校验，Issue #3469 H）；其余行是真实签发的验证码。
    """

    def __init__(self, connection_provider=None):
        self._connection_provider = connection_provider or _default_connection

    # -- 基础设施 -----------------------------------------------------
    def _connection(self):
        return self._connection_provider()

    def _run(self, work):
        """在锁内执行 ``work(cursor)`` 并提交；异常统一包成仓储错误。"""
        db, db_lock = self._connection()
        try:
            with db_lock:
                c = db.cursor()
                result = work(c)
                db.commit()
                return result
        except VerificationStoreError:
            raise
        except Exception as e:
            raise VerificationStoreError(str(e)) from e

    def _query(self, work):
        """只读查询：锁内执行，不提交（与原实现一致）。"""
        db, db_lock = self._connection()
        try:
            with db_lock:
                return work(db.cursor())
        except VerificationStoreError:
            raise
        except Exception as e:
            raise VerificationStoreError(str(e)) from e

    # -- 写入 ---------------------------------------------------------
    def insert(self, email: str, code_hash: str, scene: str, ip: str,
               expires_at: str, created_at: str) -> None:
        """插入一条新签发的验证码（``used=0``）。失败抛异常由服务层判定。"""
        def _work(c):
            c.execute(
                "INSERT INTO verification_codes "
                "(email, code_hash, scene, ip, attempts, used, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (email, code_hash, scene, ip, 0, 0, expires_at, created_at),
            )
        self._run(_work)

    def insert_shadow(self, email: str, scene: str, ip: str, created_at: str) -> None:
        """落一条影子记录（``code_hash=''``、``used=1``）——只参与限流计数。"""
        def _work(c):
            c.execute(
                "INSERT INTO verification_codes "
                "(email, code_hash, scene, ip, attempts, used, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (email, "", scene, ip, 0, 1, created_at, created_at),
            )
        self._run(_work)

    def mark_all_used(self, email: str, scene: str) -> bool:
        """原子作废该 key+scene 下全部未使用行。

        返回 rowcount>0：并发下两个请求同时校验同一个码，只有一个能拿到 True，
        另一个返回 False 并由门面转成 CODE_INVALID（复审指出的判断+消费非原子问题）。
        """
        def _work(c):
            c.execute(
                "UPDATE verification_codes SET used=1 WHERE email=? AND scene=? AND used=0",
                (email, scene),
            )
            return c.rowcount > 0
        return self._run(_work)

    def mark_used(self, vc_id: int) -> bool:
        """原子标记单条已使用（``UPDATE ... WHERE used=0``）。"""
        def _work(c):
            c.execute(
                "UPDATE verification_codes SET used=1 WHERE id=? AND used=0",
                (vc_id,),
            )
            return c.rowcount > 0
        return self._run(_work)

    def increment_attempts(self, vc_id: int, max_attempts: int) -> None:
        """原子递增失败次数，达上限即作废。

        ``UPDATE ... WHERE id=? AND used=0`` 消除先读后写的 TOCTOU；
        上限判断放进 SQL（``attempts+1 >= max_attempts`` 即置 ``used=1``），
        并发输错也不会漏判。``SET`` 顺序必须是 used 先算（MySQL 单表 UPDATE
        后一项能读到前一项的新值），否则 attempts 已 +1 会提前一行封顶。
        """
        def _work(c):
            c.execute(
                "UPDATE verification_codes "
                "SET used = CASE WHEN attempts + 1 >= ? THEN 1 ELSE used END, "
                "    attempts = attempts + 1 "
                "WHERE id = ? AND used = 0",
                (max_attempts, vc_id),
            )
        self._run(_work)

    def purge_expired(self, now: str, shadow_cutoff: str) -> None:
        """删除过期验证码 + 老化影子记录（best-effort 由调用方决定）。

        影子记录不能按 ``expires_at`` 立即删——它承载限流计数，过早删除会让
        60s 间隔/窗口次数检查失效；改为按 ``shadow_cutoff`` 的 created_at 老化。
        """
        def _work(c):
            c.execute(
                "DELETE FROM verification_codes "
                "WHERE (code_hash <> '' AND expires_at < ?) "
                "   OR (code_hash = '' AND created_at < ?)",
                (now, shadow_cutoff),
            )
        self._run(_work)

    # -- 读取 ---------------------------------------------------------
    def active_rows(self, email: str, scene: str) -> list:
        """取该 key+scene 下全部未使用行 ``(id, code_hash, attempts, expires_at)``。"""
        def _work(c):
            c.execute(
                "SELECT id, code_hash, attempts, expires_at FROM verification_codes "
                "WHERE email=? AND scene=? AND used=0 "
                "ORDER BY created_at DESC, id DESC",
                (email, scene),
            )
            return list(c.fetchall() or [])
        return self._query(_work)

    def has_any_code(self, email: str, scene: str) -> bool:
        """该 key+scene 是否**真的签发过**验证码（影子记录不算）。"""
        def _work(c):
            c.execute(
                "SELECT COUNT(*) FROM verification_codes "
                "WHERE email=? AND scene=? AND code_hash <> ''",
                (email, scene),
            )
            row = c.fetchone()
            return bool(row and row[0] > 0)
        return self._query(_work)

    def count_sends_since(self, email: str, scene: str, since: str) -> int:
        """统计邮箱（含影子记录）在窗口内的发送/请求次数；``scene`` 可空。"""
        def _work(c):
            if scene:
                c.execute(
                    "SELECT COUNT(*) FROM verification_codes "
                    "WHERE email=? AND scene=? AND created_at>=?",
                    (email, scene, since),
                )
            else:
                c.execute(
                    "SELECT COUNT(*) FROM verification_codes "
                    "WHERE email=? AND created_at>=?",
                    (email, since),
                )
            return c.fetchone()[0]
        return self._query(_work)

    def count_ip_since(self, ip: str, since: str) -> int:
        """统计 IP 在窗口内的请求次数。"""
        def _work(c):
            # Issue #3469 复审 P2：一次改绑会写多行同 IP 记录（target 影子行 +
            # 码 + 原邮箱确认码），若全算会把 5 次/10min 的 IP 上限放大成「两次就锁」。
            # 这里排除 change_email 的目标地址影子行（code_hash=''），真发码仍照常计数。
            c.execute(
                "SELECT COUNT(*) FROM verification_codes "
                "WHERE ip=? AND created_at>=? "
                "AND NOT (scene='change_email' AND COALESCE(code_hash,'')='')",
                (ip, since),
            )
            return c.fetchone()[0]
        return self._query(_work)

    def last_created_at(self, email: str, scene: str):
        """该 key+scene 最近一次写入的 ``created_at`` 串；无记录返回 ``None``。"""
        def _work(c):
            c.execute(
                "SELECT created_at FROM verification_codes "
                "WHERE email=? AND scene=? ORDER BY created_at DESC LIMIT 1",
                (email, scene),
            )
            row = c.fetchone()
            return row[0] if row else None
        return self._query(_work)