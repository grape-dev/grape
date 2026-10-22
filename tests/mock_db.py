"""时间/SQL 方言场景的 mock 层——不依赖真实数据库与真实服务进程。

三层 mock（按用例需要取用）：

1. MockCursor / MockDB：内存假数据库。execute(sql, params) 时若 sql 命中
   已注册的 handler 则返回其结果（带 PRAGMA index_list / datetime('now')
   等方言行为的可编程模拟），否则记录到 self.queries 供断言。db_lock 用
   threading.Lock 模拟。用途：路由级/数据层用例（G2/G3/B-4 等）。

2. translate 断言：不连 MySQL，直接对 src.mysql_compat.translate_sql 的
   输出做文本断言（G1 的跨后端一致性=两后端各取自己的时钟源，mock 层
   分别注入 UTC/CST 时钟验证调用方比较逻辑）。

3. 方言级验收按需求文档用例清单在目标环境执行，验收工具不入仓库；
   CI/pytest 路径一律走本 mock 层，不碰 configs/db-backend.json。
"""
import threading


class MockCursor:
    def __init__(self, db):
        self._db = db
        self.queries = []
        self._rows = []
        self.rowcount = 0
        self.description = []

    def execute(self, sql, params=()):
        self.queries.append((sql, tuple(params or ())))
        handler = self._db._match(sql)
        if handler is not None:
            self._rows = list(handler(sql, params or ()))
            self.rowcount = len(self._rows)
        else:
            self._rows = []
            self.rowcount = 0
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class MockDB:
    """可编程内存数据库：register(pattern, handler) 注册 SQL 匹配器。"""

    def __init__(self):
        self._handlers = []  # (substring, callable(sql, params) -> rows)
        self.lock = threading.RLock()
        self._cursor = MockCursor(self)
        self.committed = 0

    def _match(self, sql):
        for sub, handler in self._handlers:
            if sub in sql:
                return handler
        return None

    def register(self, pattern, rows):
        """pattern 为 SQL 子串；rows 为静态结果集或 callable(sql, params)->rows。"""
        handler = rows if callable(rows) else lambda sql, params: rows
        self._handlers.append((pattern, handler))
        return self

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1

    def execute(self, sql, params=()):
        return self._cursor.execute(sql, params)


def install_mock_db(monkeypatch, mock_db):
    """把 src.session_manager 的 db/db_lock 替换为 MockDB（模块级引用同步替换）。

    注意：业务代码多为 `from src.session_manager import db, db_lock` 的模块级
    绑定，必须逐模块替换其模块属性。
    """
    import importlib
    target_modules = [
        "src.session_manager",
        "src.api.session_store",
        "src.api.event_pipeline",
        "src.api.event_store",
        "src.api.routes.feishu_onboard",
        "src.api.routes.grape_auth",
        "src.user.db",
        "src.auth.grape_account_db",
    ]
    for mod_name in target_modules:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "db"):
            monkeypatch.setattr(mod, "db", mock_db, raising=True)
        if hasattr(mod, "db_lock"):
            monkeypatch.setattr(mod, "db_lock", mock_db.lock, raising=True)
    return mock_db


# ---------------- 方言行为模拟（供 PRAGMA/datetime 断言） ----------------

def sqlite_now_rows(sql, params):
    """模拟 SQLite 后端：datetime('now') 返回 UTC。"""
    import time
    return [(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),)]


def pragma_index_list_issue(sql, params):
    """模拟 PRAGMA index_list('issue')：(seq, name, unique) 兼容结构。"""
    return [(0, "idx_tasks_repo_issue", 1), (0, "idx_tasks_user_status", 0)]
