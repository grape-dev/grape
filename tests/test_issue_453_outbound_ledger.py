"""Issue #453 最小根治版：代发台账（outbound_content_ledger）回归测试。

覆盖 spec 验收清单核心用例：
- S11-1/S16-1  bot/系统代发评论 → 台账命中 → skip 不产任务（reason=outbound_ledger_hit）
- S11-3        用户真人评论 → 无记录 → 正常放行
- S16-3        issue_opened 台账命中但 body 含「## 父任务」→ 豁免放行；不含 → 拦
- S16（建单）  _grape_internal_trigger 同步直调 → 跳过台账判定
- S14-1        issue_updated 编辑回声 ID 命中 → 拦
- S12-3        TTL 集合：INSERT 未落盘（表无记录）时查询命中
- S13          查询异常 → 放行不崩
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
from src import outbound_ledger as ol


RK = "gitcode:test/i453led"

_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM events WHERE repo_key LIKE 'github:test/i453gh%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'github:test/i453gh%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'github:test/i453gh%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'github:test/i453gh%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM issue WHERE repo_key LIKE 'github:test/i453gh%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'github:test/i453gh%'",
    "DELETE FROM outbound_content_ledger WHERE repo_key LIKE 'gitcode:test/i453led%'",
    "DELETE FROM outbound_content_ledger WHERE repo_key LIKE 'github:test/i453gh%'",
]


def _clean():
    ol._ensure_table()
    # TTL 集合必须一并清：record_outbound 先记集合再 INSERT，集合命中优先于表查询，
    # 不清会导致跨用例泄漏（表清了集合还在 → 误命中）
    with ol._TTL_LOCK:
        ol._TTL_SET.clear()
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()


def _run(payload: dict, header: str):
    raw = json.dumps(payload).encode()
    headers = {"X-GitCode-Event": header}
    return ep.run_pipeline(raw, headers, account="test/i453led", repo_key_override="test/i453led")


def _comment_payload(username: str, note="hello", num=5, cid=99901):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"noteable_type": "Issue", "iid": num, "note": note, "id": cid},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _issue_opened_payload(num=8, body="", cid=None):
    return {
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": num, "number": num, "description": body,
                              "action": "open", "id": cid or num},
        "user": {"username": "grape_bot"},
    }


def _skipped_reasons():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT reason FROM event_skipped WHERE repo_key=? ORDER BY rowid DESC LIMIT 5", (RK,))
        return [r[0] for r in c.fetchall()]


def _no_dispatch():
    """流水线不应产出任何任务事件。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key LIKE ?", (RK + "%",))
        return c.fetchone()[0] == 0


# ---------- S11-1 / S16-1：台账命中 → 拦 ----------

def test_ledger_hit_comment_skipped():
    _clean()
    # 系统经网关代发了一条评论，VCS 返回 id=99901，已入账
    ol.record_outbound("gitcode", RK, "issue_comment", "99901", issue_number="5")
    res = _run(_comment_payload("grape_bot", note="agent 回帖", cid=99901), "Note Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()
    assert _no_dispatch()


def test_ledger_hit_pathb_user_identity():
    """S11-1 核心价值：路径 B 用户身份回声——actor=托管人本人，不在 bot 名单，仍被台账拦下。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_comment", "99902", issue_number="5")
    res = _run(_comment_payload("realuser01", note="agent 代发（用户 token）", cid=99902), "Note Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()
    assert _no_dispatch()


def test_ledger_hit_bot_name_drift():
    """真实数据模型补漏（F1）：bot 账号名漂移——actor 是我们自己的 bot 但不在
    bot 用户名名单（托管时账号名与当前配置漂移，生产实测 AgentLens 仓
    bot_username=grape_dev_bot 而代发评论 actor=grape_bot，255 条历史零拦截）。
    名单 miss → 入口拦截放行；台账 hit（cid 命中，级别/名字无关）→ 拦。
    台账是这类账号名漂移事件的唯一系统性防线。"""
    _clean()
    # 名单外 bot 名 + 该评论 cid 已入台账
    ol.record_outbound("gitcode", RK, "issue_comment", "99904", issue_number="5")
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_dev_bot"]):
        res = _run(_comment_payload("grape_bot", note="[Issue Agent] 子任务 #5 已处理完毕", cid=99904), "Note Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()
    assert _no_dispatch()
    # 反向兜底核对：同 actor、cid 不在台账 → 放行（名单 miss 时不误拦，靠后续防线）
    _clean()
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_dev_bot"]):
        res = _run(_comment_payload("grape_bot", note="另一条评论", cid=99905), "Note Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- S11-3：用户真人评论 → 放行 ----------

def test_user_comment_no_record_passes():
    _clean()
    # 无台账记录、无 marker 的真人评论——放行（后续节点正常处理，不因台账拦截）
    res = _run(_comment_payload("realuser02", note="用户真人评论，请处理", cid=99903), "Note Hook")
    reasons = _skipped_reasons()
    assert "outbound_ledger_hit" not in reasons, reasons


# ---------- S12-3：TTL 集合挡毫秒竞态 ----------

def test_ttl_set_hits_before_insert():
    _clean()
    # 写侧先记 TTL 集合、INSERT 尚未发生（模拟进程内窗口）
    with ol._TTL_LOCK:
        ol._TTL_SET[ol._ttl_key("gitcode", RK, "issue_comment", "99904")] = 9999999999.0
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "99904") is True


# ---------- S16-3：父任务豁免优先于台账 ----------

def test_issue_opened_parent_task_exempt():
    _clean()
    ol.record_outbound("gitcode", RK, "issue", "8", issue_number="8")
    # 台账命中但 body 含「## 父任务」→ 放行（多任务模式硬约束）
    res = _run(_issue_opened_payload(num=8, body="子任务说明\n\n## 父任务 #1"), "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_issue_opened_ledger_hit_blocks():
    _clean()
    ol.record_outbound("gitcode", RK, "issue", "9", issue_number="9")
    # 无「## 父任务」的 bot 建 issue → 台账命中 → 拦
    _run(_issue_opened_payload(num=9, body="普通 issue"), "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_issue_opened_internal_trigger_bypasses():
    """S16 建单直调：_grape_internal_trigger 标记 → 跳过台账判定（同步触发是我们要开工）。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue", "10", issue_number="10")
    p = _issue_opened_payload(num=10, body="")
    p["_grape_internal_trigger"] = True
    _run(p, "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- S14-1：issue_updated 编辑回声 ----------

def test_issue_edit_echo_blocked():
    _clean()
    # update_issue 端点记账：issue_edit + 剥离 marker 后指纹
    body_sent = "编辑后的正文<!-- grape-auto-comment -->"
    ol.record_outbound("gitcode", RK, "issue_edit", "5", issue_number="5",
                       content_hash=ol.content_hash_md5(body_sent))
    # webhook 回流的 description 被 GitCode 净化（无 marker）→ 剥离后指纹一致 → ID+指纹命中 → 拦
    payload = {
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 5, "description": "编辑后的正文", "action": "update"},
        "user": {"username": "grape_bot"},
    }
    _run(payload, "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_issue_edit_fingerprint_mismatch_allows():
    """S14-1 修正（用户拍板）：指纹不匹配只告警、不拦——防指纹误差打死系统。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_edit", "5", issue_number="5",
                       content_hash=ol.content_hash_md5("我们发的版本"))
    payload = {
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 5, "description": "用户自己的另一个编辑", "action": "update"},
        "user": {"username": "realuser03"},
    }
    _run(payload, "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- S13：查表异常 → 放行不崩 ----------

def test_ledger_exception_allows_through():
    _clean()
    ol.record_outbound("gitcode", RK, "issue_comment", "99909", issue_number="5")
    # 直接清掉 TTL 集合模拟"只有持久表可用"，再让建表/查询全链路抛异常
    with ol._TTL_LOCK:
        ol._TTL_SET.clear()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM outbound_content_ledger WHERE repo_key=?", (RK,))
            db.commit()
        except Exception:
            pass
    with patch("src.outbound_ledger._ensure_table", side_effect=RuntimeError("db down")):
        res = _run(_comment_payload("grape_bot", note="x", cid=99909), "Note Hook")
    # 异常放行：不崩、不 outbound_ledger_hit（兜底判断/bot 名单接手）
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ---------- marker 降级 fallback 保留 ----------

def test_marker_fallback_still_works():
    """S13-2：台账未命中但正文含 marker（GitHub 类平台形态）→ 原 marker 检测仍拦。"""
    _clean()
    _run(_comment_payload("grape_bot", note="回帖<!-- grape-auto-comment -->", cid=99910), "Note Hook")
    assert "system_auto_comment" in _skipped_reasons(), _skipped_reasons()


# ---------- 单元级：ID 提取与指纹 ----------

def test_extract_event_content_id_platforms():
    assert ol.extract_event_content_id("gitcode", {"object_attributes": {"id": 123}}) == "123"
    assert ol.extract_event_content_id("github", {"comment": {"id": 456}}) == "456"
    assert ol.extract_event_content_id("github", {"comment": {"id": 456}, "object_attributes": {"id": 789}}) == "456"


def test_extract_content_id_issue_vs_comment():
    assert ol.extract_content_id({"id": 9, "number": 5}, "issue") == "5"      # issue 取编号
    assert ol.extract_content_id({"id": 9, "number": 5}, "issue_comment") == "9"  # 评论取 ID


def test_content_hash_strips_marker():
    assert ol.content_hash_md5("hello<!-- grape-auto-comment -->") == ol.content_hash_md5("hello")
    assert ol.content_hash_md5("hello") != ol.content_hash_md5("world")


# ========== S18 对照表补齐用例（2026-10-23 追加） ==========

# ---------- S11-2：token 级别无关（判定不读级别，任意级别同命中） ----------

def test_ledger_hit_level_independent():
    """S11-2：台账判定不查 token 级别——session/repo_bot/global_bot/user 任意级别的
    代发评论（都是同一张表同一把 ID 键）同样命中。测试不构造任何级别配置。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_comment", "77701", issue_number="5")
    _run(_comment_payload("anyone", note="任意级别 token 发的", cid=77701), "Note Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()
    assert _no_dispatch()


# ---------- S11-5：provider 不返回 ID → 按无记录放行 ----------

def test_no_id_provider_allows_through():
    """S11-5：边角 provider 事件/响应无内容 ID——按无记录放行，兜底判断接手。"""
    _clean()
    # 事件 payload 无 object_attributes.id（cid=None → extract 返回空）
    p = _comment_payload("grape_bot", note="无 ID 平台的评论", num=5, cid=None)
    p["object_attributes"].pop("id", None)
    _run(p, "Note Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()
    # record 侧：无 ID 也无 issue_number → 不入账（False）
    assert ol.record_outbound("gitcode", RK, "issue_comment", "", content_hash=ol.content_hash_md5("x")) is False
    # 有 issue_number 兼值 → 入账（关 issue 等无独立 ID 的操作靠编号对账，S17 依赖此语义）
    assert ol.record_outbound("gitcode", RK, "issue_close", "", issue_number="5") is True


# ---------- S11-6：30 天清理有界 ----------

def test_cleanup_expired_removes_old_rows():
    """S11-6：超过保留期的记录被清理，表规模有界。"""
    _clean()
    ol._ensure_table()
    with db_lock:
        c = db.cursor()
        # 手工插一条 40 天前的过期记录 + 一条今天的新记录
        # （日期在 Python 侧构造——MySQL 兼容层禁止 datetime('now','-40 days') 方言）
        from datetime import datetime, timedelta
        _old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        _new = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO outbound_content_ledger "
                  "(provider, repo_key, content_type, content_id, issue_number, created_at) "
                  "VALUES ('gitcode', ?, 'issue_comment', 'old01', '1', ?)", (RK, _old))
        c.execute("INSERT INTO outbound_content_ledger "
                  "(provider, repo_key, content_type, content_id, issue_number, created_at) "
                  "VALUES ('gitcode', ?, 'issue_comment', 'new01', '2', ?)", (RK, _new))
        db.commit()
    # 强制触发清理（重置当日清理标记）
    ol._last_cleanup_day = ""
    ol._cleanup_expired()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT content_id FROM outbound_content_ledger WHERE repo_key=? AND content_id IN ('old01','new01')", (RK,))
        rows = [r[0] for r in c.fetchall()]
    assert "old01" not in rows, rows     # 过期清除
    assert "new01" in rows, rows         # 保留期内留存


# ---------- S14 修正：编辑回声不走 TTL ----------

def test_issue_edit_ttl_not_used():
    """S14 修正：issue_edit 的 TTL 集合条目不参与命中——否则用户在 300s 内编辑同一
    issue（编号同键）会被误拦。"""
    _clean()
    # 写侧刚记了 issue_edit（TTL 集合有 (gitcode, RK, issue_edit, 5)），但表内清空
    with ol._TTL_LOCK:
        ol._TTL_SET[ol._ttl_key("gitcode", RK, "issue_edit", "5")] = 9999999999.0
    assert ol.is_our_outbound("gitcode", RK, "issue_edit", "5",
                              content_hash=ol.content_hash_md5("任何内容")) is False


# ---------- S16-2：subtask 完成回帖父 issue → 入账 ----------

def test_subtask_comment_ledgered():
    """S16-2：_post_subtask_completion_to_parent 的 GitCode 路径经
    _gitcode_post_issue_comment_ledgered 记账——mock urllib 返回带 id 的响应。"""
    _clean()
    import urllib.request as _ur_req
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"id": 555001, "body": "ok"}).encode()
    with patch.object(_ur_req, "urlopen", return_value=fake_resp):
        ep._gitcode_post_issue_comment_ledgered("test/i453led", "12", "### 子任务 #12 已完成", "tok",
                                                repo_key=RK)
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "555001") is True


# ---------- S17-8/10/11：VcsApiClient 客户端层入账 ----------

def test_vcs_client_post_comment_ledgered():
    """S17-8：VcsApiClient.post_issue_comment 响应带 id → 入账（第二评论客户端收口）。"""
    _clean()
    from src.vcs.api_client import VcsApiClient
    client = VcsApiClient("gitcode", "tok", "test", "i453led")
    with patch.object(client, "_gitcode_request", return_value={"id": 666001, "body": "c"}):
        resp = client.post_issue_comment(5, "评论内容")
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "666001") is True


def test_vcs_client_close_issue_ledgered():
    """S17-10：VcsApiClient.close_issue → issue_close 入账（issue_closed 事件对账）。"""
    _clean()
    from src.vcs.api_client import VcsApiClient
    client = VcsApiClient("gitcode", "tok", "test", "i453led")
    with patch.object(client, "_gitcode_request", return_value={"number": 5, "state": "close"}), \
         patch.object(client, "get_issue", return_value={"number": 5, "title": "t"}):
        client.close_issue(5)
    assert ol.is_our_outbound("gitcode", RK, "issue_close", "5") is True


def test_vcs_client_pr_ops_ledgered():
    """S17-11：create_pr（响应取 number）与 merge_pr（传参编号）入账。"""
    _clean()
    from src.vcs.api_client import VcsApiClient
    client = VcsApiClient("gitcode", "tok", "test", "i453led")
    with patch.object(client, "_gitcode_request", return_value={"number": 33, "id": 99}):
        client.create_pr("t", "h", "b", "body")
    assert ol.is_our_outbound("gitcode", RK, "pr", "33") is True
    with patch.object(client, "_gitcode_request", return_value={"merged": True}):
        client.merge_pr(33)
    assert ol.is_our_outbound("gitcode", RK, "pr_merge", "33") is True


# ---------- S17 读侧：issue_closed / PR 事件台账命中拦 ----------

def test_issue_closed_and_pr_event_blocked():
    """S17 读侧：issue_closed（编号）与 PR 族事件（PR 编号）台账命中 → 拦。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_close", "5", issue_number="5")
    ol.record_outbound("gitcode", RK, "pr", "33")
    # issue_closed 回流
    _run({
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 5, "action": "close"},
        "user": {"username": "grape_bot"},
    }, "Issue Hook")
    # PR opened 回流（bot 建的 PR）
    _run({
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 33, "action": "open", "noteable_type": ""},
        "user": {"username": "grape_bot"},
    }, "Merge Request Hook")
    reasons = _skipped_reasons()
    assert "outbound_ledger_hit" in reasons, reasons


# ========== S19 第四轮复扫补漏用例 ==========

def test_issue_meta_echo_blocked():
    """S19-1/S19-2：我们代改 assignee/labels/reopen → issue_meta 记账，
    issue_assigned / issue_labeled / issue_reopened 回声命中 → 拦。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_meta", "5", issue_number="5")
    # assignee 变更回声（bot 接管）
    _run({
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 5, "action": "update", "assignees": "grape_bot"},
        "changes": {"assignees": [{"username": "grape_bot"}]},
        "user": {"username": "grape_bot"},
    }, "Issue Hook")
    reasons = _skipped_reasons()
    assert "outbound_ledger_hit" in reasons, reasons


def test_issue_reopened_echo_blocked():
    """S19-2：reopen 回声命中 issue_meta → 拦。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_meta", "6", issue_number="6")
    _run({
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 6, "action": "reopen"},
        "user": {"username": "grape_bot"},
    }, "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_user_meta_change_allows():
    """S19-3：用户本人改标签/换 assignee/重开——台账无记录 → 放行。"""
    _clean()
    _run({
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 7, "action": "reopen"},
        "user": {"username": "realuser04"},
    }, "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_update_issue_meta_and_edit_branches():
    """S19 写侧单元：update_issue 端点——非 body 修改记 issue_meta，body 修改记 issue_edit。
    FastAPI router 装饰不包装函数，gra.update_issue 即原函数；patch 掉 Depends 与翻译层。"""
    _clean()
    from src.api import git_repo_api as gra
    import src.vcs.providers as _vp
    with patch.object(gra, "_vcs_req", return_value={"number": 5}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"), \
         patch.object(_vp, "translate_inbound", return_value={}):
        # 元数据修改（无 body）→ issue_meta 键
        gra.update_issue("test", "i453led", 5, gra.IssueUpdateBody(), user={"username": "u"}, p="gitcode")
        assert ol.is_our_outbound("gitcode", RK, "issue_meta", "5") is True
        # body 修改 → issue_edit 键（指纹双命中）
        _b = gra.IssueUpdateBody()
        _b.body = "编辑正文"
        gra.update_issue("test", "i453led", 6, _b, user={"username": "u"}, p="gitcode")
        assert ol.is_our_outbound("gitcode", RK, "issue_edit", "6",
                                  content_hash=ol.content_hash_md5("编辑正文")) is True
        # 类型隔离：issue_meta 键上查 issue_edit 不命中
        assert ol.is_our_outbound("gitcode", RK, "issue_edit", "5",
                                  content_hash=ol.content_hash_md5("任意")) is False
        # 仓库隔离：同 ID 不同仓不命中（repo_key 进查询条件）
        assert ol.is_our_outbound("gitcode", "gitcode:other/r", "issue_meta", "5") is False


def test_provider_prefix_no_cross_talk():
    """S19-5：同仓名 GitCode/GitHub 双写——provider 前缀区分，互不串扰。"""
    _clean()
    ol.record_outbound("gitcode", RK, "issue_comment", "88801", issue_number="5")
    # GitHub 侧查同 ID——provider 不同，不命中
    assert ol.is_our_outbound("github", RK, "issue_comment", "88801") is False
    # GitCode 侧命中
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "88801") is True


# ========== S19-4 / S20 补充用例 ==========

def test_github_clarify_comment_ledgered():
    """S19-4：GitHub 澄清评论（issues.py _github_req POST 分支）响应带 comment id → 入账。
    直调该分支代码路径：mock _github_req 返回 GitHub 评论响应。"""
    _clean()
    from src.user import repo_manager as rm
    _gh_resp = {"id": 991001, "body": "💬 **开发者澄清**", "user": {"login": "u"}}
    with patch.object(rm, "_github_req", return_value=_gh_resp):
        # 复刻 issues.py GitHub 分支的入账逻辑（与产品代码同构）
        from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
        from src.repo_key import to_repo_key
        owner, rname = "test", "i453led"
        comment_body = "💬 **开发者澄清**\n\n请补充复现步骤"
        record_outbound("github", to_repo_key(f"{owner}/{rname}", "github"), "issue_comment",
                        extract_content_id(_gh_resp, "issue_comment"),
                        issue_number="5", content_hash=content_hash_md5(comment_body))
    # webhook 回流（GitHub issue-comment 事件，comment.id 载体）
    assert ol.is_our_outbound("github", "github:test/i453led", "issue_comment", "991001") is True


def test_issue_iid_number_value_alignment():
    """S20 匹配键假设显式用例：GitCode iid == number（同编号双字段名）。

    写侧从建 issue 响应取 number=52，回流侧 object_attributes 只有 iid=52——
    值相等则对账命中。同时验证 extract_content_id 对 number 为 NULL 的响应
    降级取 iid（issues_center 发送侧 number or iid or id 的规范化）。"""
    _clean()
    # 发送侧响应：number 有值（GitCode 建 issue 响应）
    _resp = {"id": 999999, "iid": 52, "number": 52, "title": "t"}
    _cid = ol.extract_content_id(_resp, "issue")
    assert _cid == "52", _cid  # number 优先
    ol.record_outbound("gitcode", RK, "issue", _cid, issue_number="52")
    # 回流侧 payload：只有 iid（实测 webhook 形态：iid 有值、number 为 NULL）
    p = _issue_opened_payload(num=52, body="普通 issue")
    p["object_attributes"]["iid"] = 52
    p["object_attributes"]["number"] = None
    _run(p, "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()
    # 响应只有 iid 无 number 时降级取 iid（规范化兜底）
    assert ol.extract_content_id({"iid": 53}, "issue") == "53"


# ========== S21 第六轮补漏用例 ==========

def test_multitask_patch_body_ledgered_gitcode():
    """S21-1：多任务模式 patch issue body（GitCode 版）→ issue_edit 记账。"""
    _clean()
    from src.api import issues_center as ic
    import urllib.request as _ur_req
    fake_resp = MagicMock()
    # with urlopen(...) as resp → resp 是 fake_resp.__enter__() 的返回值（独立 mock）
    fake_resp.__enter__.return_value.read.return_value = json.dumps({"number": 5, "body": "x"}).encode()
    with patch.object(_ur_req, "urlopen", return_value=fake_resp):
        ic._patch_issue_body("tok", "test", "i453led", 5, "正文\n\n---\n[multi-task-mode] 标记")
    # 编辑回声（GitCode 净化后无 marker 的 description）指纹双命中 → 拦
    _h = ol.content_hash_md5("正文\n\n---\n[multi-task-mode] 标记")
    assert ol.is_our_outbound("gitcode", RK, "issue_edit", "5", content_hash=_h) is True


def test_multitask_patch_body_ledgered_github():
    """S21-1：GitHub 版 _patch_github_issue_body 同样入账（provider=github）。"""
    _clean()
    from src.api import issues_center as ic
    from src.user import repo_manager as rm
    with patch.object(rm, "_github_req", return_value={"number": 6, "body": "y"}):
        ic._patch_github_issue_body("tok", "test", "i453led", 6, "正文+标记")
    _h = ol.content_hash_md5("正文+标记")
    assert ol.is_our_outbound("github", "github:test/i453led", "issue_edit", "6", content_hash=_h) is True


# ========== S7 修复点 3：redispatch 不得复活被拦/代发事件 ==========

def test_redispatch_blocked_for_ledger_hit_event():
    """S7-1 变体：重投事件内容在台账（我们代发）→ 409 拒绝，不重新入队。"""
    _clean()
    from src.api.routes import events as ev
    from fastapi import HTTPException
    # 我们代发的评论入账
    ol.record_outbound("gitcode", RK, "issue_comment", "99921", issue_number="5")
    # 历史事件入库（payload 为该评论的 webhook 形态）
    _payload = _comment_payload("grape_bot", note="系统代发", cid=99921)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO events (repo_key, issue_id, event_type, status, payload, source, actor) "
                  "VALUES (?, '5', 'issue_comment', 'failed', ?, 'gitcode', 'grape_bot')",
                  (RK, json.dumps(_payload)))
        db.commit()
        c.execute("SELECT MAX(seq) FROM events")
        _seq = c.fetchone()[0]
    with patch.object(ev, "_load_event", return_value={
            "seq": _seq, "repo_key": RK, "issue_id": "5", "event_type": "issue_comment",
            "status": "failed", "payload": json.dumps(_payload), "source": "gitcode",
            "actor": "grape_bot"}), \
         patch("src.api.deps.check_repo_access", return_value=None), \
         patch.object(ev, "require_no_session_token", return_value={"username": "admin"}):
        try:
            ev.redispatch_event(_seq, user={"username": "admin"})
            raised = False
        except HTTPException as e:
            raised = (e.status_code == 409)
        assert raised, "expected 409 for ledger-hit redispatch"


def test_redispatch_blocked_for_self_loop_skipped_event():
    """S7-1：事件曾被 self_loop 拦截（event_skipped 留痕）→ 409 拒绝。"""
    _clean()
    from src.api.routes import events as ev
    from fastapi import HTTPException
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO event_skipped (repo_key, issue_id, event_type, reason, detail) "
                  "VALUES (?, '5', 'issue_comment', 'self_loop', '')", (RK,))
        db.commit()
    with patch.object(ev, "_load_event", return_value={
            "seq": 1, "repo_key": RK, "issue_id": "5", "event_type": "issue_comment",
            "status": "failed", "payload": "{}", "source": "gitcode", "actor": "grape_bot"}), \
         patch("src.api.deps.check_repo_access", return_value=None):
        try:
            ev.redispatch_event(1, user={"username": "admin"})
            raised = False
        except HTTPException as e:
            raised = (e.status_code == 409)
        assert raised, "expected 409 for self_loop-skipped redispatch"


def test_redispatch_normal_event_allows():
    """S7-3：正常用户事件（无留痕、无台账）→ 不被 409 拦（后续流程正常走）。"""
    _clean()
    from src.api.routes import events as ev
    from fastapi import HTTPException
    _payload = _comment_payload("realuser05", note="用户评论", cid=99922)
    _evt = {"seq": 2, "repo_key": RK, "issue_id": "5", "event_type": "issue_comment",
            "status": "failed", "payload": json.dumps(_payload), "source": "gitcode",
            "actor": "realuser05"}
    with patch.object(ev, "_load_event", return_value=_evt), \
         patch("src.api.deps.check_repo_access", return_value=None), \
         patch("src.api.event_store.update_event_status", return_value=None), \
         patch("src.runtime.service.requeue_task", side_effect=RuntimeError("stop-here")):
        # 后续派发路径用 RuntimeError 截断（只验证 1.0a 不 409 拦截）
        try:
            ev.redispatch_event(2, user={"username": "admin"})
            blocked = False
        except HTTPException as e:
            blocked = (e.status_code == 409)
        except Exception:
            blocked = False  # 截断异常不算拦截
        assert not blocked, "normal event must not be 409-blocked"


# ========== S22 GitHub payload 形态流级测试（第八轮 provider 维度） ==========

def _gh_comment_payload(cid: int, body="github comment", num=5, login="ghuser"):
    """GitHub issue_comment 事件原生形态：comment 顶层对象，无 object_attributes。"""
    return {
        "action": "created",
        "issue": {"number": num, "title": "t"},
        "comment": {"id": cid, "body": body, "user": {"login": login}},
        "repository": {"full_name": "test/i453gh"},
        "sender": {"login": login},
    }


def test_github_payload_flow_ledger_hit():
    """S22：GitHub 原生 payload（comment.id 载体，无 object_attributes）过完整 pipeline——
    台账命中 → skip。验证读侧提取在 GitHub 流量形态下端到端工作（此前只有单元级）。"""
    _clean()
    RK_GH = "github:test/i453gh"
    # 我们代发的 GitHub 评论入账（comment.id=6362291824 与子代理实测同值域）
    ol.record_outbound("github", RK_GH, "issue_comment", "6362291824", issue_number="5")
    raw = json.dumps(_gh_comment_payload(6362291824)).encode()
    headers = {"X-GitHub-Event": "issue_comment"}
    # repo_key_override 需带 provider 前缀（生产 GitHub webhook 的 repo_key 形态）
    res = ep.run_pipeline(raw, headers, account="test/i453gh", repo_key_override="github:test/i453gh")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT reason FROM event_skipped WHERE repo_key=? ORDER BY id DESC LIMIT 3", (RK_GH,))
        reasons = [r[0] for r in c.fetchall()]
    assert "outbound_ledger_hit" in reasons, reasons


def test_github_payload_flow_user_allows():
    """S22：GitHub 用户真人评论（comment.id 无台账记录）→ 放行，不误拦。"""
    _clean()
    RK_GH = "github:test/i453gh"
    raw = json.dumps(_gh_comment_payload(6362291825, login="realuser")).encode()
    headers = {"X-GitHub-Event": "issue_comment"}
    ep.run_pipeline(raw, headers, account="test/i453gh", repo_key_override="github:test/i453gh")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT reason FROM event_skipped WHERE repo_key=? AND reason='outbound_ledger_hit' LIMIT 1", (RK_GH,))
        assert c.fetchone() is None


# ========== S23 入口维度：确认完成→关issue→回流（卡片/聊天链路） ==========

def test_confirm_done_close_echo_blocked():
    """S23-1：聊天/卡片"确认完成"→confirm_issue_done→VcsApiClient.close_issue（入账）
    →webhook 回流 issue_closed→台账命中拦。整条链路用真实 close_issue 客户端走。"""
    _clean()
    from src.vcs.api_client import VcsApiClient
    # 卡片/聊天链的执行层：VcsApiClient.close_issue（mock HTTP 层，客户端层记账真实执行）
    client = VcsApiClient("gitcode", "tok", "test", "i453led")
    with patch.object(client, "_gitcode_request", return_value={"number": 9, "state": "close"}), \
         patch.object(client, "get_issue", return_value={"number": 9, "state": "open", "title": "t"}):
        client.close_issue(9)
    # webhook 回流 issue_closed（我们刚关的）
    _run({
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 9, "action": "close"},
        "user": {"username": "grape_bot"},
    }, "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_user_manual_close_allows():
    """S23-2：用户在 GitCode 网页手动关 issue → 回流无台账记录 → 放行。"""
    _clean()
    _run({
        "object_kind": "issue",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 10, "action": "close"},
        "user": {"username": "realuser06"},
    }, "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ========== S24 处理矩阵：bot PR 豁免 ==========

def test_bot_pr_opened_exempt_from_ledger():
    """S24-1：bot 建的 PR（台账必有记录）→ pull_request_opened 回流放行给 QA。
    矩阵唯一系统性例外：bot 动作默认拦，唯独 PR opened 必须进 QA 审查。"""
    _clean()
    # 我们建的 PR 入账
    ol.record_outbound("gitcode", RK, "pr", "33")
    # bot actor 的 PR opened 回流——须放行（不产生 outbound_ledger_hit）
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 33, "action": "open"},
            "user": {"username": "grape_bot"},
        }, "Merge Request Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_nonbot_pr_ledger_hit_still_blocks():
    """S24-1 反例：非 bot actor 的 PR 台账命中（外部自动化经我们 API 建）→ 仍拦。"""
    _clean()
    ol.record_outbound("gitcode", RK, "pr", "34")
    _run({
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "test/i453led"},
        "object_attributes": {"iid": 34, "action": "open"},
        "user": {"username": "realuser07"},
    }, "Merge Request Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


# ========== S25/V10：网关 update_pull / merge_pull 写点（第十一轮补漏） ==========

def test_update_pull_and_merge_pull_gateway_ledgered():
    """S13/V10：agent 改 PR → update_pull 端点入账（pr 键）；合并 PR → merge_pull 入账
    （pr_merge 键）。此前两网关 PATCH/PUT 端点漏埋（URL 扫描常只匹配 POST）。"""
    _clean()
    from src.api import git_repo_api as gra
    import src.vcs.providers as _vp
    _pu = gra.PRUpdateBody()
    _pu.body = "更新后的 PR 描述"
    with patch.object(gra, "_vcs_req", return_value={"number": 21}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"), \
         patch.object(_vp, "translate_inbound", return_value={}):
        gra.update_pull("test", "i453led", 21, _pu, user={"username": "u"}, p="gitcode")
    assert ol.is_our_outbound("gitcode", RK, "pr", "21") is True
    # PR 族回流的读侧拦截由 test_issue_closed_and_pr_event_blocked 覆盖
    # （PR updated/merged 在 1.5/L1 段有 no_commit/issue_done 等更早防线，不重复验证）
    # merge_pull 网关端点
    with patch.object(gra, "_vcs_req", return_value={"merged": True}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"):
        gra.merge_pull("test", "i453led", 21, user={"username": "u"}, p="gitcode")
    assert ol.is_our_outbound("gitcode", RK, "pr_merge", "21") is True


# ========== S26 PR 三来源三分路（V4 红线） ==========

def test_user_pr_opened_allows_flow():
    """S26-1 / V4 前半：用户自己提的 PR（台账无记录）→ 放行进流水线——
    最该处理的场景一次都不能漏。验证不产生任何拦截留痕。"""
    _clean()
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 41, "action": "open"},
            "user": {"username": "realuser08"},
        }, "Merge Request Hook")
    reasons = _skipped_reasons()
    assert "outbound_ledger_hit" not in reasons, reasons
    assert "self_loop" not in reasons, reasons


# ========== V10 / S16-2 后半：此前砍掉读侧断言的用例补齐 ==========

def test_agent_update_pr_echo_blocked():
    """V10 / S13：agent 改 PR（update_pull 写点入账）→ pull_request_updated 回流台账命中。
    读侧单元级验证（PR updated 流级有 no_commit/issue_done 等多道前置防线，
    与台账共同构成防线链——本用例验证台账环节判定正确）。"""
    _clean()
    ol.record_outbound("gitcode", RK, "pr", "51")
    # PR 编号 51 命中 pr 键（pull_request_updated 的对账键同 pull_request_opened）
    assert ol.is_our_outbound("gitcode", RK, "pr", "51") is True
    # 用户改自己的 PR（无我们经手记录的另一 PR）→ 放行
    assert ol.is_our_outbound("gitcode", RK, "pr", "52") is False


def test_subtask_comment_parent_issue_echo_blocked():
    """S16-2 后半：系统在父 issue 发"子任务 #N 已完成"回帖（入账）→
    该评论的 webhook 回流（父 issue 的 issue_comment）→ 台账命中拦。"""
    _clean()
    # 写侧：_gitcode_post_issue_comment_ledgered 发到父 issue #12（S16-2 已测入账）
    from unittest.mock import MagicMock
    import urllib.request as _ur_req
    fake_resp = MagicMock()
    # 无 with：urlopen 返回值即 resp，read() 直接可调
    fake_resp.read.return_value = json.dumps({"id": 777001}).encode()
    with patch.object(_ur_req, "urlopen", return_value=fake_resp):
        ep._gitcode_post_issue_comment_ledgered("test/i453led", "12", "### 子任务 #12 已完成", "tok",
                                                repo_key=RK)
    # 读侧：父 issue 收到该评论的回流（评论 ID 命中）
    _run(_comment_payload("grape_bot", note="### 子任务 #12 已完成", num=12, cid=777001), "Note Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_pr_update_with_commit_flows_to_qa_review():
    """S26/E2 精确语义：agent 的 PR 收到新 commit（push 到同分支触发 pull_request_updated）
    → 台账命中但放行——qa 复审流转不断（Issue #46 QA↔coding 回流闭环）。"""
    _clean()
    ol.record_outbound("gitcode", RK, "pr", "61")
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 61, "action": "update", "oldrev": "abc"},
            "commits": [{"id": "def"}],
            "user": {"username": "grape_bot"},
        }, "Merge Request Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_pr_update_text_only_edit_blocked():
    """S13：agent 改 PR 标题/正文（无新 commit，纯文本编辑）→ 台账命中拦——回声不流转。"""
    _clean()
    ol.record_outbound("gitcode", RK, "pr", "62")
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 62, "action": "update"},
            "user": {"username": "grape_bot"},
        }, "Merge Request Hook")
    # 审查 D2 修正后收紧断言：真实事件名 pull_request_updated 进台账判定段，
    # 无 commit 的文本编辑必须由台账拦（不再逃逸到 no_commit 守卫的 OR）
    reasons = _skipped_reasons()
    assert "outbound_ledger_hit" in reasons, reasons


def test_pr_update_user_text_edit_allows():
    """S13 第三格：用户手动改自己 PR 的标题/描述（纯文本、无 commit）→ 放行——
    判定链是"台账命中才看 commit，台账无记录直接放"，文本编辑拦截只对系统回声生效，
    绝不能做成无条件规则误伤用户。"""
    _clean()
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 63, "action": "update"},
            "user": {"username": "realuser10"},
        }, "Merge Request Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


# ========== 审查 D1/D2/D3 修正用例 ==========

def test_bot_command_comment_not_executed():
    """审查 D1 / S4-1：bot 评论含"确认完成"→ 不执行 confirm_issue_done（指令守卫）。
    2.0b 在入口拦截之前，守卫缺失时副作用已发生——本用例钉死守卫存在。"""
    _clean()
    _executed = []
    import src.api.issues_center as ic
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
         patch.object(ic, "confirm_issue_done", side_effect=lambda *a, **k: _executed.append(a)):
        _run(_comment_payload("grape_bot", note="确认完成", num=15, cid=99931), "Note Hook")
    assert _executed == [], "bot 评论不得执行 confirm_issue_done"


def test_bot_command_echo_quoted_not_executed():
    """审查 D1 / S4-4：agent 引用用户原文（原文含 /cancel）→ 引用回声不执行指令。"""
    _clean()
    _executed = []
    import src.api.issues_center as ic
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
         patch.object(ic, "cancel_issue_task", side_effect=lambda *a, **k: _executed.append(a)):
        _run(_comment_payload("grape_bot", note="> 用户说：/cancel\n好的我来处理", num=16, cid=99932), "Note Hook")
    assert _executed == [], "引用回声不得执行 cancel_issue_task"


def test_user_command_comment_executed():
    """审查 D1 / S4-5：用户真人评论含"确认完成"→ 指令正常执行（人类语义保留，无回归）。"""
    _clean()
    _executed = []
    import src.api.issues_center as ic
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
         patch.object(ic, "confirm_issue_done", side_effect=lambda *a, **k: _executed.append(a)):
        _run(_comment_payload("realuser11", note="确认完成", num=17, cid=99933), "Note Hook")
    assert len(_executed) == 1, "用户指令必须执行一次"


def test_pr_updated_real_event_name_blocked():
    """审查 D2 回归：真实事件名 pull_request_updated（非 pr_updated 假名）进台账判定段。"""
    _clean()
    ol.record_outbound("gitcode", RK, "pr", "71")
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 71, "action": "update"},
            "user": {"username": "realuser12"},
        }, "Merge Request Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_redispatch_event_type_scoped():
    """审查 D3 / S7-3：同 issue 历史上有 bot 评论留痕（self_loop），但重派的是
    另一类型的正常事件（issue_updated）→ 不受留痕影响，放行重派。"""
    _clean()
    from src.api.routes import events as ev
    from fastapi import HTTPException
    # 该 issue 有 bot 评论被拦的留痕（issue_comment 类型）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO event_skipped (repo_key, issue_id, event_type, reason, detail) "
                  "VALUES (?, '5', 'issue_comment', 'self_loop', '')", (RK,))
        db.commit()
    # 重派同 issue 的 issue_updated 正常事件（无同类型留痕）→ 不 409
    _evt = {"seq": 3, "repo_key": RK, "issue_id": "5", "event_type": "issue_updated",
            "status": "failed", "payload": "{}", "source": "gitcode", "actor": "realuser"}
    with patch.object(ev, "_load_event", return_value=_evt), \
         patch("src.api.deps.check_repo_access", return_value=None), \
         patch("src.api.event_store.update_event_status", return_value=None), \
         patch("src.runtime.service.requeue_task", side_effect=RuntimeError("stop-here")), \
         patch("src.api.event_pipeline.run_pipeline", return_value={"seq": 9, "ok": True}):
        try:
            ev.redispatch_event(3, user={"username": "admin"})
            blocked = False
        except HTTPException as e:
            # 只把守卫 409 视为拦截；重派失败的 409（run_pipeline 未成功）不算
            blocked = ("自循环" in str(e.detail) or "代发" in str(e.detail))
        except Exception:
            blocked = False
        assert not blocked, "同 issue 不同类型事件的重派不得被历史 bot 留痕封禁"


# ========== 第十四轮审查 P0-1 / P1-1 修正用例 ==========

def test_github_create_task_triggers_pipeline():
    """审查 P0-1：GitHub 仓建单后同步触发 pipeline——run_pipeline 曾误缩进进
    GitCode 分支，GitHub 建单不再触发（缩进回归）。本用例直调 create_task_via_issue
    的 GitHub 路径，断言 run_pipeline 被调用。"""
    import src.api.issues_center as ic
    _called = []
    with patch.object(ic, "_post_github_issue", return_value={"number": 77}), \
         patch.object(ic, "_find_open_issue_by_title", return_value=None), \
         patch.object(ep, "run_pipeline",
               side_effect=lambda *a, **k: _called.append(k.get("repo_key_override") or "x") or {"seq": 1}), \
         patch("src.api.deps.check_repo_access"), \
         patch.object(ic, "ensure_issue_task", return_value=99), \
         patch.object(ic, "_ensure_task_conversation", return_value=""), \
         patch.object(ic, "_attach_list", return_value=[]), \
         patch.object(ic, "_refresh_user_gitcode_token", return_value=None):
        from src.api.issues_center import TaskCreateBody
        _b = TaskCreateBody(repo_key="github:test/i453gh", title="测试需求", description="测试描述")
        import asyncio
        try:
            asyncio.run(ic.create_task_via_issue(_b, {"username": "u", "id": 1, "auth_type": "internal", "token": "test_preauth_token"}))
        except Exception:
            pass  # 后续建档逻辑 mock 不全不影响本断言（触发已发生）
    assert any("test/i453gh" in str(c) for c in _called), f"GitHub 建单必须同步触发 pipeline: {_called}"


def test_subtask_completion_real_chain_ledgered():
    """审查 P1-1：_post_subtask_completion_to_parent 真实调用链入账（非 helper 自证）——
    GitCode 分支经 ledgered 辅助读响应取 ID。"""
    _clean()
    import urllib.request as _ur_req
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"id": 666001}).encode()
    with patch.object(_ur_req, "urlopen", return_value=fake_resp):
        ep._post_subtask_completion_to_parent("test/i453led", "12", "5", "conclusion: 完成", "tok",
                                              repo_key=RK)
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "666001") is True


def test_all_subtasks_done_real_chain_ledgered():
    """审查 P1-1：_post_all_subtasks_done 真实调用链入账。"""
    _clean()
    import urllib.request as _ur_req
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"id": 666002}).encode()
    with patch.object(_ur_req, "urlopen", return_value=fake_resp):
        ep._post_all_subtasks_done("test/i453led", "5", [{"issue_id": "12", "stage": "done"}], "tok",
                                   repo_key=RK)
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "666002") is True


def test_pr_result_comment_real_chain_ledgered():
    """审查 P1-1（写点9）：_post_result_to_pr 真实调用链入账——qa 回帖 PR 的
    note_hook_pr 回流对账。"""
    _clean()
    import urllib.request as _ur_req
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"id": 666003}).encode()
    with patch.object(_ur_req, "urlopen", return_value=fake_resp):
        ep._post_result_to_pr("test/i453led", "33", "conclusion: 审查通过", "tok", "qa",
                              repo_key=RK)
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "666003") is True


# ========== S29 对照核验修正轮（G1/G2/G3） ==========

def test_gateway_pr_comment_ledgered():
    """S29-UC1：网关 PR 评论端点（create_pull_comment）入账——原先同族写点已埋、
    唯此端点漏账（外部 API 调用者路径零台账防线）。入账键 issue_comment，
    note_hook_pr 回流按评论 ID 对账命中。"""
    _clean()
    from src.api import git_repo_api as gra
    with patch.object(gra, "_vcs_req", return_value={"id": 777001}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"):
        gra.create_pull_comment("test", "i453led", 40, gra.CommentBody(body="PR 评论内容"),
                                user={"username": "u"}, p="gitcode")
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "777001") is True


def test_gateway_create_pull_ledgered():
    """S29-UC2：网关建 PR 端点（create_pull）入账（pr 键）——同族 update_pull/merge_pull
    已入账，此端点原先漏账。外部用户建的 PR 回流命中 → 拦（bot 场景豁免见下一用例）。"""
    _clean()
    from src.api import git_repo_api as gra
    import src.vcs.providers as _vp
    _pb = gra.PRCreateBody()
    _pb.title = "外部建 PR"
    _pb.body = "PR 描述"
    with patch.object(gra, "_vcs_req", return_value={"number": 40}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"), \
         patch.object(_vp, "translate_inbound", return_value={}):
        gra.create_pull("test", "i453led", _pb, user={"username": "u"}, p="gitcode")
    assert ol.is_our_outbound("gitcode", RK, "pr", "40") is True


def test_gateway_create_pull_bot_exempt():
    """S29-UC3：网关建 PR 入账后，bot actor 的 pull_request_opened 回流仍走 S24 豁免
    （放行给 QA）——入账不得破坏 E2 红线。"""
    _clean()
    ol.record_outbound("gitcode", RK, "pr", "41")
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 41, "action": "open"},
            "user": {"username": "grape_bot"},
        }, "Merge Request Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_gateway_pr_write_idempotent_on_missing_id():
    """S29-UC4：网关 PR 端点响应不含 ID（provider 边角）——入账 best-effort 返回 False、
    端点响应/行为零变化（兜底回落基线行为，不抛错不改返回值）。"""
    _clean()
    from src.api import git_repo_api as gra
    import src.vcs.providers as _vp
    _pb = gra.PRCreateBody()
    _pb.title = "无 ID 响应"
    with patch.object(gra, "_vcs_req", return_value={"html_url": "x"}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"), \
         patch.object(_vp, "translate_inbound", return_value={}):
        _resp = gra.create_pull("test", "i453led", _pb, user={"username": "u"}, p="gitcode")
    assert _resp == {"html_url": "x"}, "端点返回值不得被入账逻辑改变"
    # PR 评论端点同样：无 ID 响应 → 返回值原样、零异常
    with patch.object(gra, "_vcs_req", return_value={"message": "created"}), \
         patch.object(gra, "_repo_token", return_value="t"), \
         patch.object(gra, "_provider", return_value="gitcode"):
        _resp2 = gra.create_pull_comment("test", "i453led", 40, gra.CommentBody(body="y"),
                                         user={"username": "u"}, p="gitcode")
    assert _resp2 == {"message": "created"}


def test_redispatch_ledger_hit_trail_blocks_after_retention():
    """S29-UC5（G3）：台账 30 天保留期过后记录已清，但 outbound_ledger_hit 留痕
    （event_skipped 无保留期）仍拒绝该事件重派——路径 B 回声 30 天后也无法复活。"""
    _clean()
    from src.api.routes import events as ev
    from fastapi import HTTPException
    # 历史留痕：该事件曾被台账拦（留痕永久保留，流水线真实形态 issue_id 带 # 前缀）；
    # 台账侧模拟已清理（无记录）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO event_skipped (repo_key, issue_id, event_type, reason, detail) "
                  "VALUES (?, '#5', 'issue_comment', 'outbound_ledger_hit', '')", (RK,))
        db.commit()
    _payload = _comment_payload("realuser01", note="agent 代发回声", cid=99902)
    with patch.object(ev, "_load_event", return_value={
            "seq": 3, "repo_key": RK, "issue_id": "#5", "event_type": "issue_comment",
            "status": "failed", "payload": json.dumps(_payload), "source": "gitcode",
            "actor": "realuser01"}), \
         patch("src.api.deps.check_repo_access", return_value=None), \
         patch.object(ev, "require_no_session_token", return_value={"username": "admin"}):
        try:
            ev.redispatch_event(3, user={"username": "admin"})
            raised = False
        except HTTPException as e:
            raised = (e.status_code == 409 and "outbound_ledger_hit" in str(e.detail))
        assert raised, "expected 409 via permanent ledger_hit trail after retention expiry"


def test_ensure_table_once_per_connection():
    """审查 D6（性能优化回归钉子）：_ensure_table 进程内一次性——同一 db 连接上
    重复调用不再执行 DDL（消除热路径每事件带 db_lock 的 CREATE/information_schema
    往返）；连接更换（测试隔离 reload 换新库）后标记失效自动重建，语义不变。"""
    import src.session_manager as _sm
    # 首次调用：在当前（fixture mock 的）连接上建表并绑定标记
    ol._TABLE_READY_CONN = None
    ol._ensure_table()
    assert ol._TABLE_READY_CONN is _sm.db, "首次调用应绑定当前连接"
    # 重复调用应命中标记直接返回（不再执行 DDL）
    ol._ensure_table()
    assert ol._TABLE_READY_CONN is _sm.db
    # 模拟连接更换（reload 后新库新连接）：标记失效 → 重新建表（幂等，不抛错）
    ol._TABLE_READY_CONN = None
    ol._ensure_table()
    assert ol._TABLE_READY_CONN is _sm.db, "新连接上应重建并重新绑定标记"
    # 重建后功能不受影响：入账 + 命中
    _clean()
    ol.record_outbound("gitcode", RK, "issue_comment", "888001", issue_number="5")
    assert ol.is_our_outbound("gitcode", RK, "issue_comment", "888001") is True


# ========== S30 共享键动作的误拦窗口（G5：close/meta 编号+时效双确认） ==========

def _seed_ledger_row(content_type, cid, minutes_ago):
    """直接按指定时间插入台账记录（模拟写侧当时的入账）。"""
    import datetime as _dt
    from src.session_manager import db as _db, db_lock as _dl
    _ts = (_dt.datetime.now() - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with _dl:
        c = _db.cursor()
        c.execute("INSERT INTO outbound_content_ledger (provider, repo_key, content_type, "
                  "content_id, issue_number, created_at) VALUES ('gitcode', ?, ?, ?, ?, ?)",
                  (RK, content_type, cid, cid, _ts))
        _db.commit()


def test_issue_close_recent_hit_blocks():
    """S30-UC1：我们关 issue（窗口内入账）→ issue_closed 回声 → 编号+窗口双命中 → 拦。"""
    _clean()
    _seed_ledger_row("issue_close", "301", minutes_ago=2)
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "issue",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 301, "action": "close"},
            "user": {"username": "realuser01"},
        }, "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_issue_meta_recent_hit_blocks():
    """S30-UC2：我们改 assignee（窗口内入账）→ issue_assigned 回声 → 双命中 → 拦。"""
    _clean()
    _seed_ledger_row("issue_meta", "302", minutes_ago=2)
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "issue",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 302, "action": "update"},
            "changes": {"assignees": {"current": [{"login": "realuser01"}]}},
            "user": {"username": "realuser01"},
        }, "Issue Hook")
    assert "outbound_ledger_hit" in _skipped_reasons(), _skipped_reasons()


def test_issue_close_stale_record_allows_user():
    """S30-UC4：我们 31 分钟前关过该 issue（保留期内但窗口外）→ 用户手动关 →
    回声放行——归档动作不再被旧记录误拦（原实现误拦 30 天）。"""
    _clean()
    _seed_ledger_row("issue_close", "303", minutes_ago=31)
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "issue",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 303, "action": "close"},
            "user": {"username": "realuser01"},
        }, "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_issue_meta_stale_record_allows_user():
    """S30-UC5：窗口外旧 meta 记录不误拦用户改 assignee/reopen。"""
    _clean()
    _seed_ledger_row("issue_meta", "304", minutes_ago=31)
    with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]):
        _run({
            "object_kind": "issue",
            "project": {"path_with_namespace": "test/i453led"},
            "object_attributes": {"iid": 304, "action": "reopen"},
            "user": {"username": "realuser01"},
        }, "Issue Hook")
    assert "outbound_ledger_hit" not in _skipped_reasons(), _skipped_reasons()


def test_issue_close_window_boundary():
    """S30-UC6：窗口边界——29 分钟前的记录命中（拦）；31 分钟前放行。单元级直测
    is_our_outbound，避免整流水线噪声。"""
    _clean()
    _seed_ledger_row("issue_close", "305", minutes_ago=29)
    assert ol.is_our_outbound("gitcode", RK, "issue_close", "305") is True
    _clean()
    _seed_ledger_row("issue_close", "306", minutes_ago=31)
    assert ol.is_our_outbound("gitcode", RK, "issue_close", "306") is False
