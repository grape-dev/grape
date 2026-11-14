"""Issue #2462 B1：lib 读分支「取行位置」回归（同步 `arch-weekly-11` 引入的两处缺陷）。

两类缺陷同一个根因——把 `row = c.fetchone()` / `rows = c.fetchall()` 留在
`if lib is not None: … else: c.execute(…)` 的**外面**：

1. `upsert_skill_obs`（`src/domains/mgmt/user/db.py`）
   - `scope='repo'` 分支只执行 SQL 不取行 → `UnboundLocalError: row`（接口 409「记录写入失败」）；
   - `scope!='repo'` 且 lib 可用时，lib 返回的行被紧随其后的 `c.fetchone()` 覆盖成 None
     → 误判为"不存在"走 INSERT，撞同名唯一键。
2. `list_event_action_filters`（`src/domains/mgmt/routes/settings.py`）
   - 有 `block_changes` 列的分支不取行 → `UnboundLocalError: rows`（接口 500）；
   - lib 分支的结果被 `c.fetchall()` 覆盖 → lib 模式下列表恒空（视图退役后静默返回空）。

沙箱里 lib 默认关闭（`uses_own_dsn` 为 False），这类缺陷在常规用例里看不见，
因此两个方向都要测：lib 关闭时的本地兜底路径 + lib 打开时的 lib 结果必须被采用。
"""
import types

from src.kernel import domain_read
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.routes import settings as settings_routes
from src.domains.mgmt.user import db as user_db


def _stub_runtime_lib(monkeypatch, **attrs):
    """把 `domain_read.runtime_lib` 换成固定桩（模拟 lib 可用）。"""
    stub = types.SimpleNamespace(
        read_one=attrs.get("read_one", lambda *a, **k: None),
        read_all=attrs.get("read_all", lambda *a, **k: []),
    )
    for name, value in attrs.items():
        setattr(stub, name, value)
    monkeypatch.setattr(domain_read, "runtime_lib", lambda *a, **k: stub, raising=True)
    return stub


def _delete_skill(name: str) -> None:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name=?", (name,))
        db.commit()


class TestUpsertSkillObs:
    def test_repo_scope_inserts_and_returns_id(self):
        """repo 分支必须自己取行：修复前 `row` 未绑定 → 返回 None（接口 409）。"""
        _delete_skill("libreg-repo")
        sid = user_db.upsert_skill_obs(
            "repo", "owner/libreg-repo", "libreg-repo", "sre", "repo skill", "obs/libreg-repo")
        assert sid, "repo scope 应插入并返回新行 id"
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT id FROM skill WHERE skill_name='libreg-repo' AND scope='repo'").fetchall()
        assert [r[0] for r in rows] == [sid]
        _delete_skill("libreg-repo")

    def test_user_scope_uses_the_lib_row_instead_of_inserting(self, monkeypatch):
        """lib 返回的行不能被 `c.fetchone()` 覆盖：应走 UPDATE 原语，而不是新增一行。"""
        _delete_skill("libreg-user")
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO skill (skill_name, display_name, description, agent_type, file_path, "
                "scope, owner_id, created_by, updated_by, is_active, category_id, content_hash) "
                "VALUES ('libreg-user','libreg-user','old','','obs/old','user','77','t','t',1,0,'oldhash')")
            db.commit()
            sid = c.execute(
                "SELECT id FROM skill WHERE skill_name='libreg-user' AND scope='user'").fetchone()[0]

        updates = []
        _stub_runtime_lib(
            monkeypatch,
            read_one=lambda name, params: (sid,),
            update_skill_obs=lambda *a, **k: updates.append((a, k)) or {"ok": True},
        )
        got = user_db.upsert_skill_obs(
            "user", "77", "libreg-user", "", "new-desc", "obs/new", "t", 0, "newhash")

        assert got == sid, "应返回已存在的行 id"
        assert updates and updates[0][0][0] == sid, "lib 更新原语必须以查到的行 id 调用"
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT file_path FROM skill WHERE skill_name='libreg-user' AND scope='user'").fetchall()
            c.execute("DELETE FROM grape_t_xwrite_log WHERE action='skill_obs_update'")
            db.commit()
        assert len(rows) == 1, "lib 分支不得再插入同名的第二行"
        _delete_skill("libreg-user")


class _FakeCursor:
    def __init__(self, pragma_rows):
        self._pragma = pragma_rows
        self.executed: list[str] = []
        self._rows: list[tuple] = []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        if "PRAGMA table_info" in sql:
            self._rows = self._pragma
        else:
            self._rows = []
        return self

    def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, pragma_rows):
        self.cursor_obj = _FakeCursor(pragma_rows)

    def cursor(self):
        return self.cursor_obj


class TestListEventActionFilters:
    def test_rows_are_fetched_when_block_changes_column_exists(self, monkeypatch):
        """有 block_changes 列的分支必须自己取行（修复前 `rows` 未绑定 → 500）。"""
        fake = _FakeDB([(0, "id", "INTEGER"), (1, "block_changes", "TEXT")])
        fake.cursor_obj._rows = []
        monkeypatch.setattr("src.kernel.session_manager.db", fake, raising=True)
        monkeypatch.setattr("src.kernel.session_manager.db_lock", __import__("contextlib").nullcontext(),
                            raising=True)

        out = settings_routes.list_event_action_filters(user={})
        assert out == {"rules": []}
        assert any("block_changes" in sql and "SELECT" in sql for sql in fake.cursor_obj.executed)

    def test_lib_rows_survive_the_following_fetchall(self, monkeypatch):
        """lib 分支的结果不能被紧随其后的 `c.fetchall()` 覆盖（否则 lib 模式恒空）。"""
        fake = _FakeDB([(0, "id", "INTEGER")])  # 旧表：无 block_changes → 走 else 分支
        monkeypatch.setattr("src.kernel.session_manager.db", fake, raising=True)
        monkeypatch.setattr("src.kernel.session_manager.db_lock", __import__("contextlib").nullcontext(),
                            raising=True)
        _stub_runtime_lib(monkeypatch, read_all=lambda name, params: [
            (7, "issue.opened", "comment", 1, "any", "", "", "", 0, 1)])

        out = settings_routes.list_event_action_filters(user={})
        assert out["rules"] == [{
            "id": 7, "event_type": "issue.opened", "action": "comment", "allow": 1,
            "actor": "any", "require_changes": "", "block_changes": "",
            "handle_side_effect": "", "priority": 0, "enabled": 1,
        }]
        assert not any("handle_side_effect" in sql and "''" in sql for sql in fake.cursor_obj.executed), \
            "lib 路径不应再执行本地兜底 SQL"