"""告警中心测试：规则求值 / 事件生命周期 / 用户面边界（§9 + §5.3）。

钉死四件容易回归的事：
1. **平台级规则与事件绝不进用户面**（DB 池 / 全部离线 / HTTP 5xx 属 L2）
2. **他人仓库的事件不可见**；越权仓库查询 404；静默他人事件失败
3. **`for_sec` 语义**：条件需持续满足才触发（抖动不触发），条件恢复后转 resolved
4. **开放事件唯一**：同一 (规则, 维度) 反复求值不会刷出重复行，而是更新 last_seen_at
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import alerts as al  # noqa: E402

UID = 7788
REPO = "gitcode:org/alert-mine"
OTHER = "gitcode:org/alert-theirs"


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM obs_alert_events", "DELETE FROM agent_tasks WHERE id LIKE 't_alrt_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        try:
            c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        except Exception:
            pass
        db.commit()


def _grant(repo_key):
    from src.session_manager import db, db_lock
    owner, _, name = repo_key.split(":", 1)[1].partition("/")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join)"
                  " VALUES (?,?,?,?,1,1)", (UID, repo_key, owner, name))
        db.commit()


def _task(tid, repo_key, status, started_min_ago=None, completed_min_ago=None):
    from src.session_manager import db, db_lock
    now = datetime.now(timezone.utc)
    st = (now - timedelta(minutes=started_min_ago)).isoformat() if started_min_ago is not None else None
    ct = (now - timedelta(minutes=completed_min_ago)).isoformat() if completed_min_ago is not None else None
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, agent_type,"
                  " user_id, status, created_at, started_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, "s-" + tid, 1, "p", repo_key, "coding", UID, status,
                   (now - timedelta(hours=1)).isoformat(), st, ct))
        db.commit()


def _rules_by_name():
    return {r["name"]: r for r in al.list_rules()}


def _events(**kw):
    from src.session_manager import db, db_lock
    sql = "SELECT * FROM obs_alert_events"
    conds, params = [], []
    for k, v in kw.items():
        conds.append(f"{k}=?")
        params.append(v)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    with db_lock:
        c = db.cursor()
        c.execute(sql, tuple(params))
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


def _set_rule(name, **fields):
    from src.session_manager import db, db_lock
    sets = ", ".join(f"{k}=?" for k in fields)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE obs_alert_rules SET {sets} WHERE name=?", (*fields.values(), name))
        db.commit()


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    al.seed_rules()
    _grant(REPO)
    # 触发条件不受 for_sec 拖累的默认设置：单测里显式把 for_sec 调小
    _set_rule("task_stuck", for_sec=0)
    _set_rule("task_failure_rate", for_sec=0)
    _set_rule("task_queue_backlog", for_sec=0, threshold=2)
    # 通知渠道默认留空，避免用例互相污染（各用例自己设置）
    for _n in ("task_stuck", "task_failure_rate", "task_queue_backlog"):
        _set_rule(_n, notify_channels="")
    # 策略层默认值复位（P1-1）：节流/聚合/时段/升级 —— 否则用例之间互相污染
    for _n in ("task_stuck", "task_failure_rate", "task_queue_backlog",
               "all_runtimes_offline", "db_pool_usage"):
        _set_rule(_n, enabled=1, severity="warning", repeat_sec=0, aggregate_mode="none",
                  aggregate_window_sec=0, notify_window_json="", escalate_json="")
    # P1-3 新增规则：指标类用例里一律关闭（数据缺失/突变有自己的探测与测试文件）
    for _n in ("runtime_silent", "repo_silent", "task_success_rate_drop"):
        _set_rule(_n, enabled=0, repeat_sec=0, aggregate_mode="none", aggregate_window_sec=0,
                  notify_window_json="", escalate_json="")
    from src.observability.alert_policy import AGGREGATOR
    AGGREGATOR.clear()
    al._pending.clear()
    yield
    _clean()


# ---------------------------------------------------------------- 种子

def test_seed_rules_idempotent_and_levels():
    n1 = al.seed_rules()
    assert n1 == 0, "二次种子不应重复插入"
    rules = _rules_by_name()
    assert set(rules) >= {"task_stuck", "task_failure_rate", "task_queue_backlog",
                          "all_runtimes_offline", "db_pool_usage", "db_aborted_connects",
                          "http_error_rate_5xx",
                          # P1-3：数据缺失 + 突变
                          "runtime_silent", "repo_silent", "task_success_rate_drop"}
    assert len(al.list_rules()) == 10, "种子规则应为 10 条（7 指标 + 2 缺失 + 1 突变）"
    user_rules = {r["name"] for r in al.list_rules(level="user")}
    assert user_rules == {"task_stuck", "task_failure_rate", "task_queue_backlog",
                          "runtime_silent", "repo_silent", "task_success_rate_drop"}
    for name in ("db_pool_usage", "db_aborted_connects", "http_error_rate_5xx",
                 "all_runtimes_offline"):
        assert rules[name]["level"] == "platform"
    # 来源可追溯到 deploy/alerts.yml
    assert rules["task_stuck"]["seed_source"] == "GrapeTaskStuck"
    # P1-3：kind 与默认开关
    assert rules["runtime_silent"]["kind"] == "gap"
    assert rules["repo_silent"]["kind"] == "gap" and int(rules["repo_silent"]["enabled"]) == 0, \
        "仓库静默默认关闭（低活跃仓库会误报）"
    assert rules["task_success_rate_drop"]["kind"] == "metric"
    assert al._event_source(rules["runtime_silent"]) == "gap"
    assert al._event_source(rules["task_success_rate_drop"]) == "mutation"
    assert al._event_source(rules["task_stuck"]) == "monitor"


# ---------------------------------------------------------------- 求值

def test_stuck_task_fires_and_resolves():
    _task("t_alrt_stuck", REPO, "running", started_min_ago=40)
    r = al.evaluate_rules()
    assert r["fired"] >= 1, r
    evs = _events(rule_name="task_stuck", status="firing", repo_key=REPO)
    assert len(evs) == 1, evs
    assert evs[0]["repo_key"] == REPO
    assert evs[0]["dim_key"] == f"repo:{REPO}"
    assert evs[0]["level"] == "user"

    # 反复求值：不重复开事件，只更新 last_seen_at
    first_seen = evs[0]["last_seen_at"]
    al.evaluate_rules()
    evs2 = _events(rule_name="task_stuck", status="firing", repo_key=REPO)
    assert len(evs2) == 1, f"开放事件应唯一，实际 {len(evs2)}"
    assert evs2[0]["id"] == evs[0]["id"]
    assert evs2[0]["last_seen_at"] >= first_seen

    # 任务结束 → 条件不满足 → 转 resolved（不再通知）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='completed', completed_at=? WHERE id='t_alrt_stuck'",
                  (datetime.now(timezone.utc).isoformat(),))
        db.commit()
    r2 = al.evaluate_rules()
    assert r2["resolved"] >= 1, r2
    assert _events(rule_name="task_stuck", status="firing", repo_key=REPO) == []
    assert len(_events(rule_name="task_stuck", status="resolved", repo_key=REPO)) == 1


def test_for_sec_requires_sustained_condition():
    """条件刚满足时不能立刻触发（抑制抖动）。"""
    _task("t_alrt_jit", REPO, "running", started_min_ago=40)
    _set_rule("task_stuck", for_sec=300)
    al._pending.clear()
    r = al.evaluate_rules()
    assert r["fired"] == 0, "for_sec 未满不应触发"
    assert _events(rule_name="task_stuck", repo_key=REPO) == []
    # 模拟"条件已持续满足 5 分钟"
    for k in list(al._pending.keys()):
        if k.startswith("task_stuck"):
            al._pending[k] = datetime.now(timezone.utc).timestamp() - 301
    r2 = al.evaluate_rules()
    assert r2["fired"] >= 1, r2


def test_failure_rate_and_backlog_metrics():
    # 5 分钟内 2 失败 1 完成 → 失败率 0.667 > 0.5
    _task("t_alrt_f1", REPO, "failed", completed_min_ago=1)
    _task("t_alrt_f2", REPO, "failed", completed_min_ago=2)
    _task("t_alrt_c1", REPO, "completed", completed_min_ago=3)
    # 窗口外的失败不计入（31 分钟前完成）
    _task("t_alrt_old", REPO, "failed", completed_min_ago=31)
    # 3 个排队 > 阈值 2
    for i in range(3):
        _task(f"t_alrt_q{i}", REPO, "queued")
    al.evaluate_rules()
    evs = {e["rule_name"]: e for e in _events(status="firing", repo_key=REPO)}
    assert "task_failure_rate" in evs, evs.keys()
    assert abs(float(evs["task_failure_rate"]["value"]) - 0.6667) < 0.01, evs["task_failure_rate"]
    assert "task_queue_backlog" in evs, evs.keys()
    assert float(evs["task_queue_backlog"]["value"]) == 3


# ---------------------------------------------------------------- 用户面边界

def test_other_users_events_are_not_visible():
    # 自己仓库 + 他人仓库各造一个卡住任务
    _task("t_alrt_mine", REPO, "running", started_min_ago=40)
    _task("t_alrt_theirs", OTHER, "running", started_min_ago=40)
    al.evaluate_rules()
    assert len(_events(status="firing", repo_key=REPO)) == 1, "自己仓库的事件应落库"
    assert len(_events(status="firing", repo_key=OTHER)) == 1, "他人仓库的事件也应落库（全局求值）"

    user = {"id": UID, "username": "u", "org_id": 0}
    mine = al.list_events(user, status="firing")
    assert len(mine) == 1, f"只应看到自己仓库的事件: {mine}"
    assert mine[0]["repo_key"] == REPO
    assert al.firing_summary(user)["firing"] == 1


def test_platform_events_never_reach_user_face():
    """平台级事件（即使被求值出来）绝不出现在用户面。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name FROM obs_alert_rules WHERE level='platform' LIMIT 1")
        row = c.fetchone()
        assert row, "应有平台级种子规则"
        c.execute(
            "INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status, metric,"
            " value, threshold, dim_key, repo_key, first_fired_at, last_seen_at)"
            " VALUES (?,?, 'platform','critical','firing','db_pool_usage',0.95,0.8,'global','',?,?)",
            (int(row[0]), row[1], datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        db.commit()
    user = {"id": UID, "username": "u", "org_id": 0}
    assert al.list_events(user, status="firing") == [], "平台级事件不该出现在用户面"
    assert al.firing_summary(user)["firing"] == 0
    # 静默平台级事件也不行（等价 404）
    ev = [e for e in _events(level="platform") if e["rule_name"] == row[1]][0]
    assert al.silence_event(int(ev["id"]), 30, user) is False


def test_user_events_survive_visible_device_dims():
    """回归（真机踩到、单测曾漏过）：带**可见设备维度**时，SQL 里不能出现字面 `%`。

    原实现是 `dim_key LIKE 'runtime:%'`；SQL 里出现字面 `%` 且同时带参数时，pymysql 会做一次
    Python `%` 格式化 → `unsupported format character` / `not enough arguments for format string`，
    而 `list_events` 的 try 把它吞掉 → **用户面统一事件视图静默丢掉全部告警类事件**
    （monitor/gap/mutation 全没了，只剩 task/change 来源），前端没有任何报错。

    为什么以前测不出来：只有 `dim_keys` 非空（调用者有可见设备）才会拼出那个 LIKE 分支，
    测试环境里 REPO 没有对应设备 → 分支被跳过。本用例显式造一台可见设备把这个分支打出来。
    """
    from src.session_manager import db, db_lock
    _clean()
    _grant(REPO)
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # 该用户可见的设备（repo_key 落在可见仓库内 → _cred_visibility 命中）
        c.execute("INSERT OR REPLACE INTO runtime_credentials"
                  " (id, token_hash, name, device_name, scope, owner_name, status, repo_key, last_seen_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (9001, "h_alrt_rt9001", "t_alrt_rt9001", "dev-alrt", "user", "u", "online", REPO, now))
        # 仓库维度事件（monitor）+ 设备维度事件（gap，dim_key=runtime:9001）
        c.execute("INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status, source,"
                  " metric, value, threshold, dim_key, repo_key, first_fired_at, last_seen_at)"
                  " VALUES (1,'task_stuck','user','warning','firing','monitor','task_stuck',3,0,?,?,?,?)",
                  (f"repo:{REPO}", REPO, now, now))
        c.execute("INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status, source,"
                  " metric, value, threshold, dim_key, repo_key, first_fired_at, last_seen_at)"
                  " VALUES (8,'runtime_silent','user','important','firing','gap','runtime_silent',900,900,"
                  " 'runtime:9001','',?,?)", (now, now))
        db.commit()
    try:
        user = {"id": UID, "username": "u", "org_id": 0}
        # 前提：这台设备对本人可见（**用包含**判断：批量跑时 app 启动可能顺带种入别的 runtime）
        assert "runtime:9001" in al.visible_runtime_dim_keys(user, [REPO]), "前提：设备应可见"

        # ① 拼出来的 SQL 文本里不能有字面 %（除占位符）——测试库是 SQLite（conftest 强制），
        #    pymysql 的 % 格式化问题在 SQLite 上**根本不会复现**，只能直接断言 SQL。
        import src.session_manager as _sm
        captured: list = []

        class _SpyCur:
            """包一层 cursor 抓 SQL（sqlite3.Cursor 的属性是只读的，不能直接改写 execute）"""

            def __init__(self, cur, cap):
                self._c = cur
                self._cap = cap

            def execute(self, sql, *a, **k):
                self._cap.append(str(sql))
                return self._c.execute(sql, *a, **k)

            def fetchall(self):
                return self._c.fetchall()

            def fetchone(self):
                return self._c.fetchone()

            @property
            def description(self):
                return self._c.description

            @property
            def rowcount(self):
                return self._c.rowcount

        class _SpyDb:
            def __init__(self, real, cap):
                self._real = real
                self._cap = cap

            def cursor(self, *a, **k):
                return _SpyCur(self._real.cursor(*a, **k), self._cap)

            def __getattr__(self, name):
                return getattr(self._real, name)

        _orig_db = _sm.db
        _sm.db = _SpyDb(_orig_db, captured)
        try:
            evs = al.list_events(user, limit=50)
        finally:
            _sm.db = _orig_db
        _hit = [s for s in captured if 'obs_alert_events' in s]
        assert _hit, f"未捕获事件查询 SQL: {captured}"
        _sql = _hit[0]
        assert 'runtime:' in _sql, f"设备维度分支没被打出来，用例失去意义: {_sql}"
        assert '%' not in _sql.replace('%s', ''), (
            f"SQL 里出现字面 %（MySQL 下 pymysql 参数绑定会报错且被吞 → 事件全丢）: {_sql}")

        # ② 行为断言（SQLite 逻辑层）
        evs = al.list_events(user, limit=50)
        assert len(evs) == 2, (f"修复前恒为 0（异常被静默吞掉）；实际 {len(evs)} 条；"
                               f"命中设备事件: {[e['dim_key'] for e in evs]}")
        assert {e["dim_key"] for e in evs} == {f"repo:{REPO}", "runtime:9001"}
        # 状态过滤也走同一条 SQL，一并钉住
        assert len(al.list_events(user, status="firing")) == 2
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM runtime_credentials WHERE name='t_alrt_rt9001'")
            db.commit()


def test_events_for_invisible_repo_are_404_and_not_silenceable():
    from src.observability.overview import RepoNotVisible
    _task("t_alrt_theirs2", OTHER, "running", started_min_ago=40)
    al.evaluate_rules()
    user = {"id": UID, "username": "u", "org_id": 0}
    with pytest.raises(RepoNotVisible):
        al.list_events(user, repo="org/alert-theirs")
    ev = _events(repo_key=OTHER, status="firing")
    assert ev, "他人仓库事件应落库"
    assert al.silence_event(int(ev[0]["id"]), 30, user) is False, "他人仓库事件不能静默"


def test_no_visible_repo_returns_empty():
    user = {"id": 999999, "username": "nobody", "org_id": 0}
    assert al.list_events(user) == []
    assert al.firing_summary(user)["firing"] == 0


def test_silence_marks_event_but_keeps_recording():
    _task("t_alrt_sil", REPO, "running", started_min_ago=40)
    al.evaluate_rules()
    ev = _events(rule_name="task_stuck", status="firing", repo_key=REPO)[0]
    user = {"id": UID, "username": "u", "org_id": 0}
    assert al.silence_event(int(ev["id"]), 30, user) is True
    row = _events(rule_name="task_stuck", repo_key=REPO)[0]
    assert row["silenced_until"], "静默时间应写入"
    assert row["status"] == "firing", "静默不影响记录（事件仍在 firing）"


# ---------------------------------------------------------------- 通知（飞书）

class _FakeApi:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_text_to_user(self, ctx, open_id, text):
        self.sent.append((open_id, text))
        return "om_test_msg_id" if self.ok else ""


class _FakeCtx:
    app_id = "cli_test_bot_123456"


class _FakeBM:
    def __init__(self, api, ctx=None):
        self.api = api
        self._ctx = ctx if ctx is not None else _FakeCtx()


def _bind_open_id(uid, open_id, username="u-notify"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id,"
                  " feishu_name, updated_at) VALUES (?,?,?,?,datetime('now'))",
                  (int(uid), username, open_id, "测试"))
        db.commit()


def _unbind_open_id(uid):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (int(uid),))
        db.commit()


def test_notify_skips_when_no_channel_configured():
    """notify_channels 为空 → 只记录不推送（种子默认如此，避免误打扰）。"""
    from src.observability import alerts as al2
    import asyncio
    _set_rule("task_stuck", notify_channels="")
    stats = asyncio.get_event_loop().run_until_complete(
        al2.notify_fired_events([{"rule_name": "task_stuck", "repo_key": REPO, "value": 1,
                                  "threshold": 0, "metric": "task_stuck", "severity": "warning"}]))
    assert stats["sent"] == 0 and stats["skipped"] == 1, stats


def test_notify_feishu_uses_recipients_own_bot(monkeypatch=None):
    """配了 feishu → 用收件人本人的 chat bot 发私聊；收件人取该仓库成员。"""
    import asyncio
    from src.observability import alerts as al2
    import src.api.main as api_main

    _bind_open_id(UID, "ou_test_open_id")
    _set_rule("task_stuck", notify_channels="feishu")
    fake_api = _FakeApi(ok=True)
    orig_bm = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = _FakeBM(fake_api)
    # 打桩“取本人 bot”与“按 user_id 取 open_id”，避免依赖真实 IM 层
    import src.api.issues_center as ic
    orig_resolve = ic._resolve_user_chat_bot
    orig_oid = ic._get_feishu_open_id_by_user_id
    ic._resolve_user_chat_bot = lambda bm, uid: bm._ctx if bm else None
    ic._get_feishu_open_id_by_user_id = lambda uid: "ou_test_open_id" if int(uid) == UID else ""
    try:
        stats = asyncio.get_event_loop().run_until_complete(
            al2.notify_fired_events([{"rule_name": "task_stuck", "repo_key": REPO, "value": 3,
                                      "threshold": 0, "metric": "task_stuck", "severity": "warning",
                                      "silenced_until": ""}]))
    finally:
        ic._resolve_user_chat_bot = orig_resolve
        ic._get_feishu_open_id_by_user_id = orig_oid
        if orig_bm is None:
            try:
                del api_main.bot_manager
            except Exception:
                pass
        else:
            api_main.bot_manager = orig_bm
        _unbind_open_id(UID)
    assert stats["sent"] == 1, stats
    assert fake_api.sent and fake_api.sent[0][0] == "ou_test_open_id"
    assert "task_stuck" in fake_api.sent[0][1] and REPO.split(":")[1] in fake_api.sent[0][1]


def test_notify_send_failure_counted_not_raised():
    """飞书返回空 message_id（token/权限问题）→ 计 failed，不影响求值。"""
    import asyncio
    from src.observability import alerts as al2
    import src.api.main as api_main
    import src.api.issues_center as ic

    _set_rule("task_stuck", notify_channels="feishu")
    orig_bm = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = _FakeBM(_FakeApi(ok=False))
    orig_resolve = ic._resolve_user_chat_bot
    orig_oid = ic._get_feishu_open_id_by_user_id
    ic._resolve_user_chat_bot = lambda bm, uid: bm._ctx if bm else None
    ic._get_feishu_open_id_by_user_id = lambda uid: "ou_x"
    try:
        stats = asyncio.get_event_loop().run_until_complete(
            al2.notify_fired_events([{"rule_name": "task_stuck", "repo_key": REPO, "value": 1,
                                      "threshold": 0, "metric": "task_stuck", "severity": "warning",
                                      "silenced_until": ""}]))
    finally:
        ic._resolve_user_chat_bot = orig_resolve
        ic._get_feishu_open_id_by_user_id = orig_oid
        if orig_bm is None:
            try:
                del api_main.bot_manager
            except Exception:
                pass
        else:
            api_main.bot_manager = orig_bm
    assert stats["failed"] >= 1, stats


def test_notify_skips_silenced_event():
    import asyncio
    from datetime import datetime, timedelta, timezone
    from src.observability import alerts as al2
    _set_rule("task_stuck", notify_channels="feishu")
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    stats = asyncio.get_event_loop().run_until_complete(
        al2.notify_fired_events([{"rule_name": "task_stuck", "repo_key": REPO, "value": 1,
                                  "threshold": 0, "metric": "task_stuck", "severity": "warning",
                                  "silenced_until": future}]))
    assert stats["skipped"] == 1 and stats["sent"] == 0, stats


def test_test_notification_reports_reason():
    """测试通道：未绑定 open_id / 未配 bot 要给出**可操作的原因**，不能只说失败。"""
    import asyncio
    from src.observability import alerts as al2
    import src.api.issues_center as ic
    import src.api.main as api_main

    # 1) 未绑定飞书身份
    _unbind_open_id(UID)
    r = asyncio.get_event_loop().run_until_complete(
        al2.send_test_notification({"id": UID, "username": "u"}))
    assert r["ok"] is False and r["reason"] == "no_open_id", r

    # 2) 绑定了但没有 bot 连接
    _bind_open_id(UID, "ou_test_open_id")
    orig_oid = ic._get_feishu_open_id_by_user_id
    orig_resolve = ic._resolve_user_chat_bot
    ic._get_feishu_open_id_by_user_id = lambda uid: "ou_test_open_id"
    ic._resolve_user_chat_bot = lambda bm, uid: None
    orig_bm = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = _FakeBM(_FakeApi())
    try:
        r = asyncio.get_event_loop().run_until_complete(
            al2.send_test_notification({"id": UID, "username": "u"}))
        assert r["ok"] is False and r["reason"] == "no_bot", r
        # 3) 正常路径
        ic._resolve_user_chat_bot = lambda bm, uid: bm._ctx if bm else None
        r = asyncio.get_event_loop().run_until_complete(
            al2.send_test_notification({"id": UID, "username": "u"}))
        assert r["ok"] is True and r["message_id"], r
    finally:
        ic._get_feishu_open_id_by_user_id = orig_oid
        ic._resolve_user_chat_bot = orig_resolve
        if orig_bm is None:
            try:
                del api_main.bot_manager
            except Exception:
                pass
        else:
            api_main.bot_manager = orig_bm
        _unbind_open_id(UID)


def test_notify_route_sets_channels_and_rejects_unknown():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    user = {"id": UID, "username": "u", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    try:
        with TestClient(app) as cli:
            rid = [r for r in al.list_rules(level="user") if r["name"] == "task_stuck"][0]["id"]
            r = cli.post(f"/api/obs/alerts/rules/{rid}/notify", params={"channels": "feishu"})
            assert r.status_code == 200 and r.json()["channels"] == "feishu", r.text
            assert [x for x in al.list_rules(level="user") if x["id"] == rid][0]["notify_channels"] == "feishu"
            r = cli.post(f"/api/obs/alerts/rules/{rid}/notify", params={"channels": "sms"})
            assert r.status_code == 400, r.text
            r = cli.post(f"/api/obs/alerts/rules/{rid}/notify", params={"channels": ""})
            assert r.status_code == 200 and r.json()["channels"] == "", r.text
            # 平台级规则不可配 → 404
            plat = [x for x in al.list_rules() if x["level"] == "platform"][0]
            r = cli.post(f"/api/obs/alerts/rules/{int(plat['id'])}/notify", params={"channels": "feishu"})
            assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.pop(require_org_member, None)


# ---------------------------------------------------------------- DB 连接池口径（误报修复）

def test_pool_usage_uses_configured_limit_not_high_water_mark():
    """分母必须是 max_connections（配置上限），不能用 Max_used_connections（历史峰值）。

    实测踩坑：74/78=93.6% 触发告警，而真实上限 8000 → 占用 0.93%，永久误报。
    """
    from src.observability import alerts as al2
    # 真实场景：74 连接 / 上限 8000
    assert abs(al2._pool_usage(74, 8000) - 0.00925) < 1e-6
    assert al2._pool_usage(74, 8000) < 0.8, "配置上限口径下不该触发 80% 阈值"
    # 旧口径（峰值 78）会算出 94.9% —— 这就是误报来源，钉死不允许
    assert round(74 / 78, 4) > 0.8, "旧口径确实会误报（回归说明）"
    # 接近上限才该报
    assert al2._pool_usage(6600, 8000) > 0.8
    # 取不到上限 → 未知就不告警
    assert al2._pool_usage(74, 0) == 0.0
    assert al2._pool_usage(74, None) == 0.0
    assert al2._pool_usage(None, 8000) == 0.0


def test_platform_metrics_pool_usage_is_sane_on_real_db():
    """真库上取数：使用率应远低于阈值（本机 max_connections=8000）。"""
    from src.observability import alerts as al2
    m = al2._platform_metrics()
    if "db_pool_usage" not in m:
        return  # SQLite 后端（无 SHOW STATUS）跳过
    v = m["db_pool_usage"]
    assert 0.0 <= v < 0.5, f"连接池使用率异常偏高，检查口径：{v}"


def test_gauge_for_configured_limit_is_exposed():
    """新增 grape_db_max_connections 指标（外部 Prometheus 也要能用正确分母）。"""
    from src.observability import DB_MAX_CONNECTIONS, DB_MAX_USED_CONNECTIONS
    assert DB_MAX_CONNECTIONS._name == "grape_db_max_connections"
    assert DB_MAX_USED_CONNECTIONS._name == "grape_db_max_used_connections"
    assert "NOT the configured limit" in DB_MAX_USED_CONNECTIONS._documentation


def test_metrics_endpoint_triggers_scrape_hook():
    """DB/业务度量是"拉取时采样"：/metrics 导出前必须触发采样，否则这些 gauge 恒为 0。

    实测踩坑（2026-10-17）：on_metrics_scrape 定义了但**零调用点** → 9090 上的
    grape_db_threads_connected / max_connections 全是 0，L3 外部面形同虚设。
    """
    import src.observability as o
    called = {"n": 0}
    orig = o.on_metrics_scrape
    o.on_metrics_scrape = lambda: called.__setitem__("n", called["n"] + 1)
    try:
        resp = o.metrics_endpoint()
        assert called["n"] == 1, "metrics_endpoint 未触发拉取时采样"
        assert b"grape_" in resp.body
    finally:
        o.on_metrics_scrape = orig


def test_mysql_metrics_collector_sets_gauge_when_available():
    """MySQL 后端下采集器应把配置上限写进 gauge（SQLite 后端跳过）。"""
    from prometheus_client import generate_latest
    import src.observability as o
    o.collect_mysql_metrics()
    text = generate_latest(o.registry).decode()
    assert "grape_db_max_connections" in text, "缺少配置上限指标"
    # 能拿到值时应为正整数（本机 8000）；SQLite 后端保持 0 也不报错
    for line in text.split("\n"):
        if line.startswith("grape_db_max_connections "):
            v = float(line.split()[1])
            assert v >= 0
            break


# ---------------------------------------------------------------- 路由层

def test_routes_banner_panel_and_rule_ops():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    _task("t_alrt_route", REPO, "running", started_min_ago=40)
    al.evaluate_rules()
    user = {"id": UID, "username": "u", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/obs/alerts")
            assert r.status_code == 200, r.text
            b = r.json()
            assert b["summary"]["firing"] >= 1, b["summary"]
            assert any(e["rule_name"] == "task_stuck" for e in b["events"]), b["events"]
            assert b["rules"] and all(x["level"] == "user" for x in b["rules"]), b["rules"]
            # 平台级规则不得出现在用户面规则列表里
            assert not any(x["level"] == "platform" for x in b["rules"])

            rid = b["rules"][0]["id"]
            r = cli.post(f"/api/obs/alerts/rules/{rid}/toggle", params={"enabled": "false"})
            assert r.status_code == 200 and r.json()["enabled"] is False, r.text
            r = cli.post(f"/api/obs/alerts/rules/{rid}/threshold", params={"threshold": "7"})
            assert r.status_code == 200 and r.json()["threshold"] == 7.0, r.text
            # 平台级规则不可被用户面切换 → 404
            plat = [x for x in al.list_rules() if x["level"] == "platform"][0]
            r = cli.post(f"/api/obs/alerts/rules/{int(plat['id'])}/toggle", params={"enabled": "true"})
            assert r.status_code == 404, r.text

            ev_id = b["events"][0]["id"]
            r = cli.post(f"/api/obs/alerts/{ev_id}/silence", params={"minutes": "15"})
            assert r.status_code == 200, r.text
            r = cli.post("/api/obs/alerts/99999999/silence", params={"minutes": "15"})
            assert r.status_code == 404, r.text
            # 越权仓库 → 404
            r = cli.get("/api/obs/alerts", params={"repo": "org/alert-theirs"})
            assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.pop(require_org_member, None)


# ---------------------------------------------------------------- 策略层（P1-1 / D1）

def _insert_event(rule_id, rule_name, repo_key, first_iso=None, deferred=0, last_notified=""):
    """造一条 firing 事件（绕过求值，专注策略链路）。返回事件 id。"""
    from src.session_manager import db, db_lock
    now = first_iso or datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_alert_events (rule_id, rule_name, level, severity, status,"
                  " metric, value, threshold, dim_key, repo_key, first_fired_at, last_seen_at,"
                  " notify_deferred, last_notified_at)"
                  " VALUES (?,?,?,'warning','firing',?,?,?,?,?,?,?,?,?)",
                  (int(rule_id), rule_name, "user", "task_stuck", 1.0, 0.0,
                   f"repo:{repo_key}", repo_key, now, now, int(deferred), last_notified))
        db.commit()
        return int(c.lastrowid)


def _reload_ev(ev, rule_id):
    """把库里的通知状态合并回事件 dict（真实循环每轮都是重新读状态的）。"""
    rows = _events(rule_id=int(rule_id))
    if not rows:
        return ev
    r = rows[0]
    return {**ev, "id": int(r["id"]), "last_notified_at": r.get("last_notified_at") or "",
            "notify_deferred": int(r.get("notify_deferred") or 0),
            "escalated_at": r.get("escalated_at") or "",
            "silenced_until": r.get("silenced_until") or ""}


def test_rule_policy_roundtrip_and_validation():
    """策略字段可写可读；**非法配置不写库**（返回 errors，脏值不覆盖好值）。"""
    rid = _rules_by_name()["task_stuck"]["id"]
    res = al.set_rule_policy(rid, {
        "severity": "severe", "repeat_sec": 900, "aggregate_mode": "period",
        "aggregate_window_sec": 600,
        "notify_window_json": '{"days":[1,2,3,4,5],"start":"09:00","end":"19:00"}',
        "escalate_json": '{"after_sec":1800,"severity":"critical"}'})
    assert res["ok"] and res["errors"] == [], res
    r = al.get_rule(rid)
    assert r["severity"] == "severe" and int(r["repeat_sec"]) == 900
    assert r["aggregate_mode"] == "period" and int(r["aggregate_window_sec"]) == 600
    assert '"start": 540' in r["notify_window_json"] and '1800' in r["escalate_json"]

    bad = al.set_rule_policy(rid, {"severity": "urgent", "repeat_sec": -1,
                                   "aggregate_mode": "hourly", "notify_window_json": "{oops"})
    assert bad["ok"] is False and len(bad["errors"]) == 4, bad
    r2 = al.get_rule(rid)
    assert r2["severity"] == "severe" and r2["aggregate_mode"] == "period"   # 未被脏写覆盖

    assert al.set_rule_policy(999999, {"severity": "info"})["errors"] == ["rule not found"]


def test_list_rules_exposes_policy_fields():
    """规则列表必须带策略字段（管理台 T-A1 靠它回显）。"""
    r = _rules_by_name()["task_queue_backlog"]
    for k in ("repeat_sec", "aggregate_mode", "aggregate_window_sec",
              "notify_window_json", "escalate_json"):
        assert k in r, f"缺字段 {k}"


def test_admin_api_lists_global_rules_and_writes_policy():
    """管理台 L2 接口：全局规则列表（含 platform）+ 策略写入；非法 400 / 不存在 404。"""
    from fastapi.testclient import TestClient
    from src.api.deps import require_admin
    from src.api.main import app

    rid = _rules_by_name()["task_stuck"]["id"]
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "username": "admin"}
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/admin/obs/alerts/rules")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["global"] is True and body["count"] == len(al.list_rules())
            assert any(x["level"] == "platform" for x in body["rules"]), "管理台是全局视图"
            assert "severe" in body["severities"] and "rule" in body["aggregate_modes"]

            r = cli.put(f"/api/admin/obs/alerts/rules/{rid}/policy",
                        json={"severity": "important", "repeat_sec": 60})
            assert r.status_code == 200, r.text
            assert r.json()["applied"]["severity"] == "important"
            assert al.get_rule(rid)["severity"] == "important"

            r = cli.put(f"/api/admin/obs/alerts/rules/{rid}/policy",
                        json={"aggregate_mode": "weekly"})
            assert r.status_code == 400 and r.json()["detail"]["error_code"] == "INVALID_POLICY"
            r = cli.put("/api/admin/obs/alerts/rules/999999/policy", json={"severity": "info"})
            assert r.status_code == 404 and r.json()["detail"]["error_code"] == "RULE_NOT_FOUND"
            r = cli.put(f"/api/admin/obs/alerts/rules/{rid}/policy", json={})
            assert r.status_code == 400
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_evaluate_escalates_long_running_event_once():
    """开放事件持续超过 escalate_json.after_sec → 产生升级候选并把级别抬高（只升一次）。"""
    rid = _rules_by_name()["task_stuck"]["id"]
    al.set_rule_policy(rid, {"escalate_json": '{"after_sec":60,"severity":"critical"}'})
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _insert_event(rid, "task_stuck", REPO, first_iso=old)
    r = al.evaluate_rules()
    esc = [e for e in r["new_events"] if e.get("escalated")]
    assert len(esc) == 1, r["new_events"]
    assert esc[0]["severity"] == "critical" and esc[0]["repo_key"] == REPO
    assert _events(rule_id=rid)[0]["escalated_at"], "escalated_at 未落库"
    # 已升级过 → 不再重复产生升级候选
    r2 = al.evaluate_rules()
    assert [e for e in r2["new_events"] if e.get("escalated")] == []


def test_escalation_not_due_before_threshold():
    rid = _rules_by_name()["task_stuck"]["id"]
    al.set_rule_policy(rid, {"escalate_json": '{"after_sec":7200,"severity":"critical"}'})
    _insert_event(rid, "task_stuck", REPO,
                  first_iso=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    r = al.evaluate_rules()
    assert [e for e in r["new_events"] if e.get("escalated")] == []


def test_evaluate_renominates_open_event_when_repeat_due():
    """repeat_sec>0：开放事件到期后**重新进通知候选**（真机演练发现的缺口：只抑制不复推）。"""
    from src.session_manager import db, db_lock
    rid = _rules_by_name()["task_stuck"]["id"]
    al.set_rule_policy(rid, {"repeat_sec": 60})
    ev_id = _insert_event(rid, "task_stuck", REPO)          # last_notified_at='' → 从未通知过
    r = al.evaluate_rules()
    assert any(int(e.get("id") or 0) == ev_id for e in r["new_events"]), r["new_events"]
    # 刚通知过（未到间隔）→ 不再提名
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE obs_alert_events SET last_notified_at=? WHERE id=?",
                  (datetime.now(timezone.utc).isoformat(), ev_id))
        db.commit()
    r2 = al.evaluate_rules()
    assert not any(int(e.get("id") or 0) == ev_id for e in r2["new_events"]), r2["new_events"]
    # repeat_sec=0 → 永不复推
    al.set_rule_policy(rid, {"repeat_sec": 0})
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE obs_alert_events SET last_notified_at='' WHERE id=?", (ev_id,))
        db.commit()
    r3 = al.evaluate_rules()
    assert not any(int(e.get("id") or 0) == ev_id for e in r3["new_events"]), r3["new_events"]


def test_deferred_event_is_resent_only_inside_window():
    """通知时段外只记录（notify_deferred=1）；窗口内才补推（且不每天反复提醒）。"""
    now_cst = datetime.now(timezone(timedelta(hours=8)))
    h = now_cst.hour
    rid = _rules_by_name()["task_stuck"]["id"]
    # 窗口 = 当前小时起（含当前时刻）
    al.set_rule_policy(rid, {"notify_window_json": '{"start":"%02d:00","end":"23:59"}' % h})
    _insert_event(rid, "task_stuck", REPO, deferred=1)
    r = al.evaluate_rules()
    assert any(e["repo_key"] == REPO for e in r["new_events"]), r["new_events"]

    # 窗口在 9 小时后才开 → 不产生候选（只记录）
    nh = (h + 9) % 24
    al.set_rule_policy(rid, {"notify_window_json":
                             '{"start":"%02d:00","end":"%02d:59"}' % (nh, nh)})
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE obs_alert_events SET notify_deferred=1, last_notified_at='' WHERE rule_id=?",
                  (rid,))
        db.commit()
    r2 = al.evaluate_rules()
    assert not any(e["repo_key"] == REPO for e in r2["new_events"]), r2["new_events"]


def test_notify_repeat_suppression_and_state_writeback():
    """repeat_sec=0：只通知一次（第二次跳过）；成功后写 notify_count/last_notified_at。"""
    import asyncio
    from src.observability.alert_policy import AGGREGATOR
    import src.api.main as api_main
    import src.api.issues_center as ic

    _bind_open_id(UID, "ou_test_open_id")
    fake_api = _FakeApi(ok=True)
    orig_bm = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = _FakeBM(fake_api)
    orig_resolve, orig_oid = ic._resolve_user_chat_bot, ic._get_feishu_open_id_by_user_id
    ic._resolve_user_chat_bot = lambda bm, uid: bm._ctx if bm else None
    ic._get_feishu_open_id_by_user_id = lambda uid: "ou_test_open_id"
    rid = _rules_by_name()["task_stuck"]["id"]
    ev = {"rule_name": "task_stuck", "repo_key": REPO, "value": 1, "threshold": 0,
          "metric": "task_stuck", "severity": "warning", "silenced_until": ""}
    loop = asyncio.new_event_loop()
    try:
        al.set_rule_policy(rid, {"notify_channels": "feishu", "repeat_sec": 0})
        ev["id"] = _insert_event(rid, "task_stuck", REPO)
        s1 = loop.run_until_complete(al.notify_fired_events([dict(ev)]))
        assert s1["sent"] == 1, s1
        row = _events(rule_id=rid)[0]
        assert row["last_notified_at"] and int(row["notify_count"]) == 1, row

        # 第二轮：真实循环会带库里的 last_notified_at 进来 → 重复抑制
        s2 = loop.run_until_complete(al.notify_fired_events([_reload_ev(ev, rid)]))
        assert s2["sent"] == 0 and s2["skipped"] == 1, s2

        # repeat_sec>0 且未到间隔 → 仍抑制
        al.set_rule_policy(rid, {"repeat_sec": 3600})
        s3 = loop.run_until_complete(al.notify_fired_events([_reload_ev(ev, rid)]))
        assert s3["sent"] == 0 and s3["skipped"] == 1, s3
    finally:
        loop.close()
        ic._resolve_user_chat_bot, ic._get_feishu_open_id_by_user_id = orig_resolve, orig_oid
        if orig_bm is None:
            try:
                del api_main.bot_manager
            except Exception:
                pass
        else:
            api_main.bot_manager = orig_bm
        _unbind_open_id(UID)
        AGGREGATOR.clear()


def test_notify_aggregate_buffers_then_flushes_summary():
    """聚合规则：不发单条 → 进缓冲；窗口到期 → 一条汇总（含维度与最大当前值）。"""
    import asyncio
    from src.observability.alert_policy import AGGREGATOR
    import src.api.main as api_main
    import src.api.issues_center as ic
    from datetime import timedelta as _td

    _bind_open_id(UID, "ou_test_open_id")
    fake_api = _FakeApi(ok=True)
    orig_bm = getattr(api_main, "bot_manager", None)
    api_main.bot_manager = _FakeBM(fake_api)
    orig_resolve, orig_oid = ic._resolve_user_chat_bot, ic._get_feishu_open_id_by_user_id
    ic._resolve_user_chat_bot = lambda bm, uid: bm._ctx if bm else None
    ic._get_feishu_open_id_by_user_id = lambda uid: "ou_test_open_id"
    rid = _rules_by_name()["task_stuck"]["id"]
    loop = asyncio.new_event_loop()
    try:
        al.set_rule_policy(rid, {"notify_channels": "feishu", "aggregate_mode": "rule",
                                 "aggregate_window_sec": 300, "repeat_sec": 0})
        ev = {"id": _insert_event(rid, "task_stuck", REPO), "rule_name": "task_stuck",
              "repo_key": REPO, "value": 4, "threshold": 0, "metric": "task_stuck",
              "severity": "warning", "silenced_until": ""}
        s = loop.run_until_complete(al.notify_fired_events([ev]))
        assert s["aggregated"] == 1 and s["sent"] == 0, s
        assert AGGREGATOR.pending() == 1
        g = list(AGGREGATOR._groups.values())[0]
        text = al._aggregate_text(g)
        assert "汇总" in text and "task_stuck" in text and "受影响" in text, text

        # 强制窗口到期 → flush 发出汇总
        g["window_end"] = datetime.now(timezone.utc) - _td(seconds=1)
        f = loop.run_until_complete(al.flush_aggregates())
        assert f["summaries"] == 1 and f["sent"] == 1, f
        assert "汇总" in fake_api.sent[-1][1], fake_api.sent
        assert AGGREGATOR.pending() == 0
    finally:
        loop.close()
        ic._resolve_user_chat_bot, ic._get_feishu_open_id_by_user_id = orig_resolve, orig_oid
        if orig_bm is None:
            try:
                del api_main.bot_manager
            except Exception:
                pass
        else:
            api_main.bot_manager = orig_bm
        _unbind_open_id(UID)
        AGGREGATOR.clear()