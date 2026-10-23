"""Issue #453: session 级 token 仓库 bot 评论自循环 — 入口统一拦截回归测试。

事故（2026-10-23）：issue-372 的 read_stored_token() 把托管人 PAT 抄写进
devops_agent_session.repo_gitcode_token → 新托管仓库 resolve_config_level 返回
level=session → SessionTokenHandler.is_self_loop_actor 写死 return False →
grape_bot 自己发的 issue 评论被当作新用户评论受理 → "bot 评论 → 任务 → 再评论"
闭环，任务堆积、worker 健康分拉黑。

修复：event_pipeline 2.0 节在 resolve_config_level 之前对评论族
（issue_comment / note_hook_pr）按 actor 命中 bot 用户名表无条件跳过，
不依赖 token 级别分派。对应 docs/issue-453/spec.md 场景 S1–S5。
"""
import json
from unittest.mock import patch, MagicMock

# 测试隔离（审查 F1 修正，mock 方案）：混跑时其他模块（如 test_issue_337_list_handoff）
# 会在收集期/每个测试前 reload(session_manager) 重绑全局 db 单例且不恢复——任何在
# 模块级/收集期捕获的连接引用都会与写侧分裂（写 A 库读 B 库）。
# 修法：不再对齐任何磁盘库（时序不可控），改为每个测试独立内存库 mock——
# autouse fixture monkeypatch session_manager.db/db_lock 及所有已捕获该引用的
# 模块属性，写读两侧（含 run_pipeline 的函数级 import）恒指向同一 mock 库。
import sys
import threading

import pytest


class _DbProxy:
    """动态转发到当前 session_manager.db（被 fixture patch 后即 mock 库）。"""

    def __getattr__(self, attr):
        import src.session_manager as _sm
        return getattr(_sm.db, attr)


class _DbLockProxy:
    def __enter__(self):
        import src.session_manager as _sm
        self._real = _sm.db_lock
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)


db = _DbProxy()
db_lock = _DbLockProxy()

# 模块级捕获 session_manager.db/db_lock 的已知模块（run_pipeline 写侧链路）：
# 函数级 import 的模块自动经 session_manager 取到 mock，无需列举。
_PATCH_DB_MODULES = (
    "src.session_manager", "src.api.event_store", "src.api.event_pipeline",
    "src.user.db", "src.api.session_store", "src.api.issues_center",
    "src.api.db_setup", "src.api.routes.events", "src.api.token_resolver",
)


@pytest.fixture(autouse=True)
def _mock_db(monkeypatch):
    """每测试独立内存库：建表走真实 init_database（内部函数级 import 取到 mock）。"""
    import sqlite3
    import src.session_manager as _sm
    import src.api.db_setup as _dbs
    _conn = sqlite3.connect(":memory:", check_same_thread=False)
    _lock = threading.RLock()
    for _name in _PATCH_DB_MODULES:
        _mod = sys.modules.get(_name)
        if _mod is None:
            continue
        monkeypatch.setattr(_mod, "db", _conn, raising=False)
        monkeypatch.setattr(_mod, "db_lock", _lock, raising=False)
    _dbs.init_database()
    yield
    _conn.close()

from src.api import event_pipeline as ep


RK = "gitcode:test/i453"

_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/i453%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/i453%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/i453%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/i453%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/i453%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/i453%'",
]


def _clean():
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql)
        db.commit()


def _run(payload: dict, header: str):
    raw = json.dumps(payload).encode()
    headers = {"X-GitCode-Event": header}
    return ep.run_pipeline(raw, headers, account="test/i453", repo_key_override="test/i453")


def _issue_comment_payload(username: str, note="代码已合入，回归通过", num=5):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i453"},
        "object_attributes": {"noteable_type": "Issue", "iid": num, "note": note},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _pr_comment_payload(username: str, note="PR 评论", num=7):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i453"},
        "object_attributes": {"noteable_type": "MergeRequest", "iid": num, "note": note},
        "user": {"username": username},
    }


def _issue_opened_payload(username: str, description="普通需求"):
    return {
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453"},
        "object_attributes": {"id": 9, "iid": 9, "title": "T", "action": "open",
                              "description": description},
        "user": {"username": username},
    }


def _skipped_rows(reason=None):
    with db_lock:
        c = db.cursor()
        if reason:
            c.execute("SELECT COUNT(*) FROM event_skipped WHERE repo_key=? AND reason=?",
                      (RK, reason))
        else:
            c.execute("SELECT COUNT(*) FROM event_skipped WHERE repo_key=?", (RK,))
        return c.fetchone()[0]


def _session_level_result():
    """模拟 issue-453 现场：session 级 token（PAT 被抄写进 devops_agent_session）。"""
    from src.token_resolver import TokenResult
    return TokenResult(token="SESSION_PAT", level="session",
                       provider=MagicMock(), repo_key=RK)


class TestS1SessionLevelBotComment:
    """S1: session 级仓库 bot 评论（本 bug 核心场景）——入口拦截必须生效。"""

    def test_s1_1_bot_issue_comment_skipped(self):
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()), \
             patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor",
                   return_value=False):
            result = _run(_issue_comment_payload("grape_bot"), "Note Hook")
        assert _skipped_rows("self_loop") == 1, "bot issue 评论必须被入口拦截并留痕"
        assert _skipped_rows() == 1, "不应产生其他 skip 记录"
        with db_lock:
            row = db.cursor().execute(
                "SELECT detail, event_type FROM event_skipped WHERE repo_key=? AND reason='self_loop'",
                (RK,)).fetchone()
        assert row[0] == "grape_bot" and row[1] == "issue_comment"

    def test_s1_2_bot_pr_comment_skipped(self):
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()):
            _run(_pr_comment_payload("grape_bot"), "Note Hook")
        assert _skipped_rows("self_loop") == 1, "bot PR 评论（note_hook_pr）同样拦截"
        with db_lock:
            row = db.cursor().execute(
                "SELECT event_type FROM event_skipped WHERE repo_key=? AND reason='self_loop'",
                (RK,)).fetchone()
        assert row[0] == "note_hook_pr"

    def test_s1_3_user_comment_not_intercepted(self):
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()):
            _run(_issue_comment_payload("alice"), "Note Hook")
        assert _skipped_rows("self_loop") == 0, "真实用户评论不得被入口拦截"


class TestS2RepoBotLevelRegression:
    """S2: 存量 repo_bot 级仓库 bot 评论——原拦截行为保留（无回归）。"""

    def test_s2_1_repo_bot_level_bot_comment_still_skipped(self):
        _clean()
        from src.token_resolver import TokenResult
        _bot_level = TokenResult(token="REPO_BOT", level="repo_bot",
                                 provider=MagicMock(), repo_key=RK)
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _bot_level):
            _run(_issue_comment_payload("grape_bot"), "Note Hook")
        assert _skipped_rows("self_loop") == 1, "repo_bot 级仓库 bot 评论仍被拦截"

    def test_s2_2_repo_bot_level_user_comment_not_skipped(self):
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()):
            _run(_issue_comment_payload("bob"), "Note Hook")
        assert _skipped_rows("self_loop") == 0


class TestS3NonCommentEvents:
    """S3: 非评论族事件不受入口拦截影响。"""

    def test_s3_1_bot_subtask_issue_opened_allowed(self):
        """bot 创建的「## 父任务」issue_opened 必须放行（豁免保留）。"""
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()), \
             patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor",
                   return_value=False):
            _run(_issue_opened_payload("grape_bot", description="## 父任务\n#1"), "Issue Hook")
        assert _skipped_rows("self_loop") == 0, "子任务豁免被入口拦截破坏（回归！）"

    def test_s3_3_pr_family_not_touched_by_entry_intercept(self):
        """PR 族事件（pull_request_opened）不走评论族入口拦截。"""
        _clean()
        payload = {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453"},
            "object_attributes": {"iid": 3, "action": "open", "title": "PR"},
            "user": {"username": "grape_bot"},
        }
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()), \
             patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor",
                   return_value=False):
            _run(payload, "Merge Request Hook")
        assert _skipped_rows("self_loop") == 0, "PR 族不应被评论族入口拦截"


class TestS5GuardEdges:
    """S5: 守卫边界——actor 为空 / bot 表为空时不拦截、不抛错。"""

    def test_s5_1_empty_actor_no_intercept_no_crash(self):
        _clean()
        payload = _issue_comment_payload("")
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
            _run(payload, "Note Hook")
        assert _skipped_rows("self_loop") == 0

    def test_s5_3_empty_bot_usernames_degrades_to_no_intercept(self):
        """bot 用户名查询返回空（DB 异常降级）→ 不拦截，走原 handler 链。"""
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=[]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()), \
             patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor",
                   return_value=False):
            _run(_issue_comment_payload("grape_bot"), "Note Hook")
        assert _skipped_rows("self_loop") == 0, "降级为不拦截（不误杀），由原 handler 链兜底"
