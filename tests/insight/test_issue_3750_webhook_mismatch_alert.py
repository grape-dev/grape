"""Issue #3750：同仓库 webhook token mismatch 速率告警（指标 + 种子规则 + 生命周期）。

钉死四件事：
1. 指标 `webhook_token_mismatch` 只数近 60s、按仓库维度（跨仓库不聚合）；
2. 种子规则 `webhook_token_mismatch_rate` 字段与 deploy/alerts.yml 对齐（gt 10 / 60s / for 5min / warning）；
3. `for_sec` 语义不变：刚超阈值不触发，持续满足才 firing；
4. **能回落**：拒绝停掉后指标归 0，已 firing 的事件转 resolved（存在期 15 分钟内仍参与求值）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.insight.observability import alerts as al  # noqa: E402
from src.domains.insight.observability import webhook_rejects as wr  # noqa: E402

REPO = "gitcode:grape-dev/wm-a"
OTHER = "gitcode:grape-dev/wm-b"
RULE = "webhook_token_mismatch_rate"


def _clean():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM obs_webhook_rejects")
        c.execute("DELETE FROM obs_alert_events WHERE rule_name=?", (RULE,))
        db.commit()


def _set_rule(name, **fields):
    from src.kernel.session_manager import db, db_lock
    sets = ", ".join(f"{k}=?" for k in fields)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE obs_alert_rules SET {sets} WHERE name=?", (*fields.values(), name))
        db.commit()


def _insert_rejects(repo_key, n, seconds_ago=0):
    from src.kernel.session_manager import db, db_lock
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    with db_lock:
        c = db.cursor()
        for _ in range(n):
            c.execute("INSERT INTO obs_webhook_rejects (repo_key, provider, event, ip,"
                      " token_fp, expected_fp, reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (repo_key, "gitcode", "Push Hook", "203.0.113.9",
                       "sha256:aaaaaaaaaaaa", "sha256:bbbbbbbbbbbb", "mismatch", ts))
        db.commit()


def _events(**kw):
    from src.kernel.session_manager import db, db_lock
    sql = "SELECT * FROM obs_alert_events WHERE rule_name=?"
    params = [RULE]
    for k, v in kw.items():
        sql += f" AND {k}=?"
        params.append(v)
    with db_lock:
        c = db.cursor()
        c.execute(sql, tuple(params))
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


def _pending_key(repo_key=REPO):
    return f"{RULE}|repo:{repo_key}"


def _setup():
    _clean()
    al.seed_rules()
    # 默认：阈值/窗口/开关取种子值，for_sec 归 0 便于单轮触发；用例自己改回 300 验持续语义
    _set_rule(RULE, for_sec=0, threshold=10, window_sec=60, enabled=1)
    al._pending.clear()


def _teardown():
    al._pending.clear()
    _clean()


class _LedgerCase:
    """xunit 钩子：类内用例每条前后都清拒绝表/开放事件（模块级 setup_function 不作用于方法）。"""

    def setup_method(self, method=None):
        _setup()

    def teardown_method(self, method=None):
        _teardown()


class TestSeedRule(_LedgerCase):
    def test_rule_definition_matches_spec(self):
        rule = next(r for r in al.SEED_RULES if r["name"] == RULE)
        assert rule["metric"] == "webhook_token_mismatch"
        assert rule["comparator"] == "gt"
        assert rule["threshold"] == 10
        assert rule["window_sec"] == 60
        assert rule["for_sec"] == 300
        assert rule["severity"] == "warning"
        assert rule["level"] == "user", "用户面可见（按仓库维度），不是平台级"
        assert rule["seed_source"] == "GrapeWebhookTokenMismatch"

    def test_seed_idempotent(self):
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            db.cursor().execute("DELETE FROM obs_alert_rules WHERE name=?", (RULE,))
            db.commit()
        n1 = al.seed_rules()
        assert n1 >= 1
        n2 = al.seed_rules()
        assert n2 == 0
        row = next(r for r in al.list_rules() if r["name"] == RULE)
        assert row["metric"] == "webhook_token_mismatch" and int(row["enabled"]) == 1


class TestMetric(_LedgerCase):
    def test_counts_recent_per_repo(self):
        _insert_rejects(REPO, 11)
        _insert_rejects(REPO, 5, seconds_ago=20)
        # 窗口外（2 分钟前）不计入指标，但仍在存在期内 → 维度保留、值为 0
        _insert_rejects(OTHER, 100, seconds_ago=120)
        m = al._repo_metrics()
        assert m[REPO]["webhook_token_mismatch"] == 16.0, m.get(REPO)
        assert m[OTHER]["webhook_token_mismatch"] == 0.0, "窗口外的拒绝不得计入 60s 计数"

    def test_no_rejects_no_metric_dim(self):
        m = al._repo_metrics()
        assert "webhook_token_mismatch" not in m.get(REPO, {})


class TestLifecycle(_LedgerCase):
    def test_for_sec_requires_sustained_mismatch(self):
        _set_rule(RULE, for_sec=300)
        _insert_rejects(REPO, 11)
        al._pending.clear()
        r = al.evaluate_rules()
        assert _events(status="firing") == [], "for_sec 未满不得触发"
        # 模拟条件已持续 5 分钟
        al._pending[_pending_key()] = datetime.now(timezone.utc).timestamp() - 301
        r2 = al.evaluate_rules()
        evs = _events(status="firing", repo_key=REPO)
        assert len(evs) == 1, (r2, evs)
        assert evs[0]["dim_key"] == f"repo:{REPO}"
        assert evs[0]["severity"] == "warning"
        assert evs[0]["level"] == "user"
        assert float(evs[0]["value"]) >= 11

    def test_threshold_not_crossed_no_fire(self):
        _insert_rejects(REPO, 10)   # 阈值 gt 10：恰好 10 次不触发
        al.evaluate_rules()
        assert _events(status="firing") == []

    def test_resolves_when_mismatch_stops(self):
        _set_rule(RULE, for_sec=0)
        _insert_rejects(REPO, 11)
        al.evaluate_rules()
        assert len(_events(status="firing", repo_key=REPO)) == 1
        # 拒绝停掉：行仍在 15 分钟存在期内，但已滑出 60s 计数窗口 → 指标归 0
        from src.kernel.session_manager import db, db_lock
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        with db_lock:
            db.cursor().execute("UPDATE obs_webhook_rejects SET created_at=?", (old,))
            db.commit()
        r = al.evaluate_rules()
        assert r["resolved"] >= 1, r
        assert _events(status="firing") == []
        assert len(_events(status="resolved", repo_key=REPO)) == 1

    def test_cross_repo_not_aggregated(self):
        _set_rule(RULE, for_sec=0)
        _insert_rejects(REPO, 6)
        _insert_rejects(OTHER, 6)   # 合计 12，但分仓库各 6 → 都不触发
        al.evaluate_rules()
        assert _events(status="firing") == [], "跨仓库不得聚合（会掩盖单仓库错配）"
        m = al._repo_metrics()
        assert m[REPO]["webhook_token_mismatch"] == 6.0
        assert m[OTHER]["webhook_token_mismatch"] == 6.0

    def test_pending_pruned_when_dimension_disappears(self):
        """拒绝中断超过存在期（15 分钟）后仓库从维度集合消失，`_pending` 必须一起裁掉。

        否则恢复后的首轮沿用陈旧「首个命中时刻」，直接满足 `for_sec`，绕过 300s 持续判定。
        """
        _set_rule(RULE, for_sec=300)
        _insert_rejects(REPO, 11)
        al.evaluate_rules()
        assert _pending_key() in al._pending

        from src.kernel.session_manager import db, db_lock
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        with db_lock:
            db.cursor().execute("UPDATE obs_webhook_rejects SET created_at=?", (old,))
            db.commit()
        assert al._repo_metrics().get(REPO, {}).get("webhook_token_mismatch") is None, \
            "滑出存在期后该维度本轮不应再出现"
        al.evaluate_rules()
        assert _pending_key() not in al._pending, "维度消失后残留的 pending 会绕过 for_sec"

        _insert_rejects(REPO, 11)
        al.evaluate_rules()
        assert _events(status="firing") == [], "恢复首轮必须重新等满 for_sec"

    def test_pending_dropped_when_metric_missing(self, monkeypatch):
        """维度仍被遍历但本轮没有该指标（val=None）同样算未命中，pending 要清。"""
        _set_rule(RULE, for_sec=300)
        _insert_rejects(REPO, 11)
        al.evaluate_rules()
        assert _pending_key() in al._pending
        monkeypatch.setattr(al, "_repo_metrics",
                            lambda: {REPO: {"task_stuck": 0.0, "task_queued": 0.0}})
        al.evaluate_rules()
        assert _pending_key() not in al._pending

    def test_user_face_event_visible_for_repo(self):
        _set_rule(RULE, for_sec=0)
        _insert_rejects(REPO, 11)
        al.evaluate_rules()
        user = {"id": 7788, "username": "u", "org_id": 0}
        from src.kernel.session_manager import db, db_lock
        owner, _, name = REPO.split(":", 1)[1].partition("/")
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name,"
                      " is_admin, is_join) VALUES (?,?,?,?,1,1)", (7788, REPO, owner, name))
            db.commit()
        mine = al.list_events(user, status="firing")
        assert any(e["rule_name"] == RULE for e in mine), mine
        with db_lock:
            db.cursor().execute("DELETE FROM user_repos WHERE user_id=?", (7788,))
            db.commit()
