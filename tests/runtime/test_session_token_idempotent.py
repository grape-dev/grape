"""session_api_tokens 幂等签发/轮换（#990 类缺陷的回归锁）。

背景（现场实证）：
  · 部署库 `session_api_tokens` 上有 `UNIQUE(session_key)`（索引名 uniq_session_key，
    该索引不在代码 DDL 里，属部署侧/迁移侧手工加）；
  · 旧实现 `session_token_rotate` 只把旧行 expires_at 缩短后**裸 INSERT**，
    `session_token_insert`（issue_session_token 底层）同样是裸 INSERT；
  · 同一 task_id/session_key 第二次签发 → 1062 Duplicate entry；
  · 上层把该异常吞成一条 warning「task continues」，于是出现
    「agent_tasks.env_extra 里有 GRAPE_SESSION_TOKEN，子进程 environ 里没有」的漂移，
    A2A 补发 / issues detail / tasks complete 全部 Missing token。

本用例锁三件事：
  1. 同 session_key 连续 rotate 两次不抛异常，表内仍只有 1 行；
  2. 同 session_key 连续 issue_session_token 两次不抛异常，表内仍只有 1 行；
  3. rotate 与 insert 混用同一 key 也不冲突（两者同源 upsert）。
"""
from __future__ import annotations

import hashlib
import uuid

import pytest

from src.domains.mgmt.public import session_token_insert, session_token_rotate
from src.kernel.auth_sdk.auth_token import issue_session_token
from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitcode:idem/r"
KEY_PREFIX = "idem:test:"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _rows(session_key: str) -> list:
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT session_key, repo_key, agent_type, token_sha256, expires_at "
            "FROM session_api_tokens WHERE session_key=?",
            (session_key,),
        )
        return c.fetchall()


@pytest.fixture()
def unique_session_key_index():
    """复现部署库形态：session_key 单列唯一（代码 DDL 里没有这条约束）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens WHERE session_key LIKE 'idem:test:%'")
        try:
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_session_key_probe "
                "ON session_api_tokens(session_key)"
            )
        except Exception:  # pragma: no cover - 库上已有等价约束时视为已复现
            pass
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens WHERE session_key LIKE 'idem:test:%'")
        db.commit()


@pytest.fixture()
def no_unique_session_key_index():
    """原设计形态：库里没有 uniq_session_key（grape_dev_mgmt 就是这样）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens WHERE session_key LIKE 'multi:test:%'")
        # 同文件的 unique_session_key_index fixture 建的是 uniq_session_key_probe 且不 drop，
        # 两个名字都要清掉，否则「无唯一索引」的库形态根本没复现出来
        for _idx in ("uniq_session_key", "uniq_session_key_probe"):
            try:
                c.execute(f"DROP INDEX IF EXISTS {_idx}")
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens WHERE session_key LIKE 'multi:test:%'")
        db.commit()


def test_rotate_twice_keeps_single_row(unique_session_key_index):
    key = KEY_PREFIX + uuid.uuid4().hex
    now = 1_700_000_000

    session_token_rotate(key, REPO_KEY, "issue", "42", _sha("tk-first"), now + 600, now)
    # 旧实现这里会 1062；新实现必须静默覆盖
    session_token_rotate(key, REPO_KEY, "issue", "42", _sha("tk-second"), now + 900, now)

    rows = _rows(key)
    assert len(rows) == 1, f"同一 session_key 应恒定 1 行，实际 {len(rows)}"
    assert rows[0][3] == _sha("tk-second"), "应覆盖为最新 token_sha256"
    assert int(rows[0][4]) == now + 900, "expires_at 应更新为最新值"


def test_issue_session_token_twice_is_idempotent(unique_session_key_index):
    key = KEY_PREFIX + uuid.uuid4().hex

    first = issue_session_token(session_key=key, repo_key=REPO_KEY, agent_type="issue", user_id="42")
    # 旧实现（裸 INSERT + 唯一约束）此处抛 1062
    second = issue_session_token(session_key=key, repo_key=REPO_KEY, agent_type="issue", user_id="42")

    assert first and second and first != second
    rows = _rows(key)
    assert len(rows) == 1, f"重复签发应幂等，实际 {len(rows)} 行"
    assert rows[0][3] == _sha(second), "库里应只保留最新 token 的 sha256"


def test_rotate_and_insert_mixed_same_key_no_conflict(unique_session_key_index):
    key = KEY_PREFIX + uuid.uuid4().hex
    now = 1_700_000_500

    session_token_insert(key, REPO_KEY, "issue", "42", _sha("tk-a"), now + 300)
    session_token_rotate(key, REPO_KEY, "issue", "42", _sha("tk-b"), now + 600, now)
    session_token_insert(key, REPO_KEY, "issue", "42", _sha("tk-c"), now + 900)

    rows = _rows(key)
    assert len(rows) == 1
    assert rows[0][3] == _sha("tk-c")


def test_insert_returns_row_id_on_upsert(unique_session_key_index):
    key = KEY_PREFIX + uuid.uuid4().hex
    first_id = session_token_insert(key, REPO_KEY, "issue", "42", _sha("tk-x"), 1_700_001_000)
    second_id = session_token_insert(key, REPO_KEY, "issue", "42", _sha("tk-y"), 1_700_001_100)
    assert first_id > 0 and second_id == first_id, "upsert 后应返回同一行 id，保证调用方契约不变"


def test_same_value_rewrite_twice_keeps_single_row(unique_session_key_index):
    """同值重写两次：MySQL 上 UPDATE 的 rowcount 是「实际改变行数」= 0。

    旧实现拿 rowcount 判存在性 → 判成「未命中」→ 再 INSERT → 1062。
    新实现靠 UNIQUE(session_key) 决定命不命中，值与旧行完全相同也必须收敛为 1 行。
    """
    key = KEY_PREFIX + uuid.uuid4().hex
    now = 1_700_002_000

    for _ in range(2):
        session_token_insert(key, REPO_KEY, "issue", "42", _sha("tk-same"), now + 600)
    for _ in range(2):
        session_token_rotate(key, REPO_KEY, "issue", "42", _sha("tk-same"), now + 600, now)

    rows = _rows(key)
    assert len(rows) == 1, f"同值重写也必须恒定 1 行，实际 {len(rows)}"
    assert rows[0][3] == _sha("tk-same")


def test_primitives_insert_first_and_fall_back_on_conflict():
    """源码级回归锁：两个原语的写入形态。

    1) 不得用 UPDATE 的 rowcount 判存在性（MySQL changed rows=0 / SQLite matched rows=1，
       前者会让线上 1062 复现、后者让单测永远绿）；
    2) 必须是「裸 INSERT 优先」—— 原设计允许同一 session_key 多枚并存（无唯一索引的库，
       如 grape_dev_mgmt），不能再用无条件 upsert 把它抹成单行；
    3) 必须带撞唯一键降级路径（部署侧可能已手工建 uniq_session_key）。
    """
    import inspect

    from src.domains.mgmt.public import session_token_insert, session_token_rotate

    for fn in (session_token_insert, session_token_rotate):
        src = inspect.getsource(fn)
        assert "rowcount" not in src, (
            f"{fn.__name__} 仍在用 rowcount 判存在性：MySQL 同值重写 rowcount=0 会再 INSERT 撞唯一键")
        assert "INSERT INTO session_api_tokens" in src, f"{fn.__name__} 必须裸 INSERT 优先"
        assert "_is_unique_conflict" in src, (
            f"{fn.__name__} 缺少撞唯一键降级路径（部署侧可能存在 uniq_session_key）")


def test_no_unique_index_allows_multiple_rows(no_unique_session_key_index):
    """原设计形态：无唯一索引的库上，同一 session_key 两次签发 → 两行并存，两枚都有效。

    这正是「chat 的 conv 级 key 合法」的前提，也是 2026-11-21 一度被我改掉、现已还原的语义。
    """
    from src.kernel.auth_sdk.auth_token import verify_session_token
    from src.domains.mgmt.public import session_token_insert

    key = "multi:test:conv"
    t1 = issue_session_token(session_key=key, repo_key=REPO_KEY, agent_type="chat", user_id="7")
    t2 = issue_session_token(session_key=key, repo_key=REPO_KEY, agent_type="chat", user_id="7")
    rows = _rows(key)
    assert len(rows) == 2, f"无唯一索引时必须两行并存（原设计），实际 {len(rows)}"
    assert verify_session_token(t1) is not None, "先签发的 token 被后一条顶掉了"
    assert verify_session_token(t2) is not None



def test_upsert_sql_translates_to_mysql_duplicate_key():
    """方言层：SQLite 的 ON CONFLICT ... DO UPDATE 必须翻译成 MySQL 的 ON DUPLICATE KEY UPDATE。"""
    from src.kernel.mysql_compat import translate_sql

    sql = ("INSERT INTO session_api_tokens (session_key, repo_key, token_sha256) "
           "VALUES (?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
           "repo_key=excluded.repo_key, token_sha256=excluded.token_sha256")
    out = translate_sql(sql)

    assert "ON DUPLICATE KEY UPDATE" in out.upper()
    assert "ON CONFLICT" not in out.upper()
    assert "VALUES(repo_key)" in out or "VALUES(`repo_key`)" in out
    assert "excluded." not in out.lower()
