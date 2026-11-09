"""Issue #565: bot 创建的 Issue 触发智能推荐流水线 — 回归测试。

背景：bot 建单（Agent feedback 等批量入口）的 issue_opened 此前被 2.0 自循环检测
拦截 → scene_id 永远为空，后续评论路由 get_issue_scene 落到 dev_scene/default 兜底链。

修复：
1. 自循环检测两处路径（主路径 handler / fallback）对 bot actor 的 issue_opened 一律放行，
   放行时打 evt["_bot_opened"]=True；评论族（issue_comment/note_hook_pr）拦截不动。
2. 放行后、路由判定前加仓级滑动窗口限流：同 repo_key 60s 内最多
   _BOT_OPEN_RECOMMEND_LIMIT 条 bot 建单推荐（当前 30），超限记
   event_skipped(bot_open_recommend_rate_limited)（进程内 dict，不进 DB）。
3. 推荐完成回调确认会派发执行棒（_redispatch_vod_after_recommend 重派 VOD 执行流，
   direct 路径 event_pipeline + worker 路径 service._handle_vod_recommend_complete 双入口），
   bot 建单以 evt["_bot_opened"] 短路——只做场景判定，不进执行棒。

用例（对齐验收标准 5 条）：
T1 bot issue_opened → 放行 + 派发 vod 推荐任务 + worker 侧回调写 scene_id + 不重派执行棒
T2 issue 已有场景的 bot 建单 → 跳过推荐（is_scene_recommend=False）
T3 60s 内第 6 条 bot 建单 → event_skipped 记 bot_open_recommend_rate_limited
T4 bot issue_comment → 仍被 self_loop 拦截（回归保护）
T5 人建 issue_opened → 行为与现状一致（放行 + 推荐，不限流）
"""
import json
import sys
import threading
from unittest.mock import patch, MagicMock

import pytest


# 测试隔离（同 test_issue_453_self_loop.py 的 mock 方案）：每测试独立内存库，
# 防止混跑时其他模块 reload(session_manager) 导致写读两侧库分裂。
class _DbProxy:
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

_PATCH_DB_MODULES = (
    "src.session_manager", "src.api.event_store", "src.api.event_pipeline",
    "src.user.db", "src.api.session_store", "src.api.issues_center",
    "src.api.db_setup", "src.api.routes.events", "src.api.token_resolver",
    "src.runtime.service",
)


@pytest.fixture(autouse=True)
def _mock_db(monkeypatch):
    import sqlite3
    import src.session_manager as _sm
    import src.api.db_setup as _dbs
    _conn = sqlite3.connect(":memory:", check_same_thread=False)
    _lock = threading.RLock()
    _real_db, _real_lock = _sm.db, _sm.db_lock
    for _name in _PATCH_DB_MODULES:
        _mod = sys.modules.get(_name)
        if _mod is None:
            continue
        monkeypatch.setattr(_mod, "db", _conn, raising=False)
        monkeypatch.setattr(_mod, "db_lock", _lock, raising=False)
    _dbs.init_database()
    # 每测试重置 Issue #565 限流窗口（进程内模块级状态）
    from src.api import event_pipeline as _ep
    _ep._BOT_OPEN_RECOMMEND_WINDOWS.clear()
    yield
    for _m in list(sys.modules.values()):
        try:
            if getattr(_m, "db", None) is _conn:
                _m.db = _real_db
            if getattr(_m, "db_lock", None) is _lock:
                _m.db_lock = _real_lock
        except Exception:
            pass
    _conn.close()


from src.api import event_pipeline as ep

RK = "gitcode:test/i565"

_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/i565%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/i565%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/i565%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/i565%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/i565%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/i565%'",
    "DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:test/i565%'",
]


def _clean():
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql)
        db.commit()


def _issue_opened_payload(username, num, description="bot 批量建单需求"):
    return {
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i565"},
        "object_attributes": {"id": num, "iid": num, "number": num, "title": f"T{num}",
                              "action": "open", "description": description},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _issue_comment_payload(username, note="bot 自评回帖", num=9):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i565"},
        "object_attributes": {"noteable_type": "Issue", "iid": num, "note": note},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _session_level_result():
    from src.token_resolver import TokenResult
    return TokenResult(token="SESSION_PAT", level="session",
                       provider=MagicMock(), repo_key=RK)


def _skipped_count(reason=None):
    with db_lock:
        c = db.cursor()
        if reason:
            c.execute("SELECT COUNT(*) FROM event_skipped WHERE repo_key=? AND reason=?",
                      (RK, reason))
        else:
            c.execute("SELECT COUNT(*) FROM event_skipped WHERE repo_key=?", (RK,))
        return c.fetchone()[0]


def _events_count():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=?", (RK,))
        return c.fetchone()[0]


def _dispatch_patches(handler_self_loop=True):
    """公共 patch 集：session 级 token + 引擎广播派发 mock（捕获 _enqueue_pipeline_task）。"""
    captured = []

    def _fake_enqueue(*args, **kwargs):
        captured.append({
            "agent_type": args[2] if len(args) > 2 else kwargs.get("agent_type"),
            "is_scene_recommend": kwargs.get("is_scene_recommend", False),
        })
        return None

    return captured, [
        patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]),
        patch("src.token_resolver.TokenResolver.resolve_config_level",
              side_effect=lambda rk: _session_level_result()),
        patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor",
              return_value=handler_self_loop),
        patch("src.runtime.engine_select.pick_engine", return_value=0),
        patch("src.runtime.engine_select.engine_name", return_value="opencode"),
        patch("src.runtime.engine_select.get_scene_pool_engine_ids", return_value=""),
        patch("src.runtime.engine_select._repo_user_id", return_value=1),
        patch("src.api.event_pipeline.is_plugin_mode", return_value=True),
        patch("src.runtime.service.get_execution_mode", return_value="remote"),
        patch("src.api.event_pipeline._enqueue_pipeline_task",
              side_effect=_fake_enqueue),
        patch("src.user.db.get_agent_bot_token", return_value=""),
        patch("src.project_info.builder.maybe_refresh_project_info", lambda *a, **k: None),
    ]


def _run(payload: dict, header="Issue Hook"):
    raw = json.dumps(payload).encode()
    return ep.run_pipeline(raw, {"X-GitCode-Event": header},
                           account="test/i565", repo_key_override="test/i565")


class TestT1BotIssueOpenedRecommends:
    def test_t1_bot_issue_opened_allowed_recommend_scene_only(self):
        """T1: bot issue_opened → 不进 self_loop、派发 vod 推荐任务、
        worker 侧回调写 scene_id 且 _bot_opened 短路执行棒（不重派 VOD）。"""
        _clean()
        captured, patches = _dispatch_patches(handler_self_loop=True)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _run(_issue_opened_payload("grape_bot", 201))
        # 1) 不被自循环拦截，无任何 skip 记录
        assert _skipped_count() == 0, "bot 建单 issue_opened 必须放行（不得 self_loop）"
        # 2) 派发 vod 智能推荐任务
        assert captured, "必须派发 vod 推荐任务"
        assert captured[0]["agent_type"] == "vod"
        assert captured[0]["is_scene_recommend"] is True, "bot 建单走智能推荐（场景判定）"
        _events_before = _events_count()
        assert _events_before == 1, "应产生 1 条 issue_opened 事件"
        # 3) 2026-11-04 反转：默认放开 → bot 建单推荐完成后重派 VOD，继续跑完整流水线
        with patch("src.api.event_store.append_event", return_value=1) as _ae, \
             patch("src.runtime.engine_select.pick_engine", return_value=None), \
             patch.object(ep, "_fail_event"):
            ep._redispatch_vod_after_recommend(RK, "#201", "vod",
                                               {"_bot_opened": True, "payload": {}}, 0)
        assert _ae.called, ("bot 建单必须重派 VOD 执行棒"
                            "（GRAPE_BOT_OPENED_FULL_PIPELINE 默认 1）")
        # 3b) 开关显式置 0 → 回退旧行为：scene-only，不新增事件
        with patch("src.config.GRAPE_BOT_OPENED_FULL_PIPELINE", False), \
             patch("src.api.event_store.append_event", return_value=1) as _ae2:
            ep._redispatch_vod_after_recommend(RK, "#201", "vod",
                                               {"_bot_opened": True, "payload": {}}, 0)
        assert not _ae2.called, "开关置 0 时必须回退 scene-only（Issue #565 旧行为）"
        # 4) worker 路径：推荐完成回调解析 scene_id 写库 + 按 issue 首事件 actor 还原
        #    _bot_opened 短路标记（核实结论：回调确会派执行棒，已按标记短路）
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
                "prompt, status) VALUES ('t565-1', 'sess-i565', 0, ?, 'vod', "
                "'你是智能推荐器，请判定 scene_id', 'completed')",
                (RK,))
            db.commit()
        redispatch_calls = []
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.runtime.engine_select._repo_user_id", return_value=1), \
             patch("src.api.pipeline_config.get_first_node",
                   return_value={"skill": "vod", "tag": "vod"}), \
             patch("src.api.event_pipeline._redispatch_vod_after_recommend",
                   side_effect=lambda rk, iid, skill, evt, rid:
                       redispatch_calls.append(dict(evt=evt))):
            from src.runtime.service import _handle_vod_recommend_complete
            _handle_vod_recommend_complete(RK, "#201", '{"scene_id": "scene-i565"}',
                                           "t565-1", {"runtime_id": 0, "account": "test/i565"})
        from src.api.session_store import get_issue_scene
        _scene, _ = get_issue_scene(RK, "#201")
        assert _scene == "scene-i565", "推荐完成回调必须写 issue scene_id"
        assert redispatch_calls, "worker 侧回调应尝试重派（执行棒派发点）"
        assert redispatch_calls[0]["evt"].get("_bot_opened") is True, \
            "worker 侧必须按建单人还原 _bot_opened 短路标记"


class TestT2ExistingSceneSkipsRecommend:
    def test_t2_bot_open_with_existing_scene_skips_recommend(self):
        """T2: issue 卡已有场景的 bot 建单 → 跳过推荐（is_scene_recommend=False，不重复判定）。"""
        _clean()
        from src.api.session_store import set_issue_scene
        set_issue_scene(RK, "#202", "scene-i565")
        captured, patches = _dispatch_patches(handler_self_loop=True)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _run(_issue_opened_payload("grape_bot", 202))
        assert _skipped_count("self_loop") == 0, "bot 建单仍须放行"
        assert captured, "已有场景走正常 vod 派发"
        assert captured[0]["is_scene_recommend"] is False, \
            "issue 已有场景 → 跳过推荐（不重复判定）"


class TestT3RateLimit:
    def test_t3_over_limit_bot_open_within_60s_rate_limited(self):
        """T3: 同 repo 60s 内第 (limit+1) 条 bot 建单 → event_skipped 记 bot_open_recommend_rate_limited。"""
        _clean()
        _lim = ep._BOT_OPEN_RECOMMEND_LIMIT
        captured, patches = _dispatch_patches(handler_self_loop=True)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            for num in range(301, 301 + _lim + 1):  # limit 条放行 + 第 limit+1 条超限
                _run(_issue_opened_payload("grape_bot", num))
        assert _skipped_count("bot_open_recommend_rate_limited") == 1, \
            f"第 {_lim + 1} 条 bot 建单必须被限流并留痕"
        assert _events_count() == _lim, f"前 {_lim} 条正常入流水线（各 1 条事件）"
        assert _skipped_count("self_loop") == 0, "限流≠自循环，不得误记 self_loop"


class TestT4BotCommentStillIntercepted:
    def test_t4_bot_issue_comment_still_self_loop(self):
        """T4: bot 的 issue_comment → 仍被 self_loop 拦截（评论族闸门不动，回归保护）。"""
        _clean()
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _session_level_result()), \
             patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor",
                   return_value=False):
            _run(_issue_comment_payload("grape_bot"), header="Note Hook")
        assert _skipped_count("self_loop") == 1, "bot 评论必须继续被入口拦截"
        assert _skipped_count() == 1, "不应产生其他 skip 记录"


class TestT5HumanIssueOpenedUnchanged:
    def test_t5_human_issue_opened_behavior_unchanged(self):
        """T5: 人建 issue_opened → 行为与现状一致（放行 + vod 推荐，不受限流影响）。"""
        _clean()
        # 预先打满 bot 限流窗口（人建单不应受影响）
        for _ in range(ep._BOT_OPEN_RECOMMEND_LIMIT):
            ep._bot_open_recommend_rate_limited(RK)
        assert ep._bot_open_recommend_rate_limited(RK) is True, \
            f"窗口应已打满（bot 第 {ep._BOT_OPEN_RECOMMEND_LIMIT + 1} 条会被限）"
        captured, patches = _dispatch_patches(handler_self_loop=False)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _run(_issue_opened_payload("alice", 401))
        assert _skipped_count() == 0, "人建单不得被拦截/限流"
        assert captured, "人建单照常派发 vod 推荐任务"
        assert captured[0]["agent_type"] == "vod"
        assert captured[0]["is_scene_recommend"] is True, "人建单走智能推荐（现状不变）"
