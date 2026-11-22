"""Issue #3770：env_extra 存量明文凭据行爆量 / 明文落库收口。

覆盖（seam = 用户可见行为，不测内部细节）：
- 写入收口：`sync_task_session_token` 轮换后 `agent_tasks.env_extra` 原文为 enc:v1 信封（无 tk_ 明文）；
  加密失败 → 任务失败（返回空串）且**不写回明文**，计数 write_refused；
  写入侧清单（QA #3770 审查 R2 收全）：enqueue / sync_task_session_token /
  `_create_direct_task` / `_create_failed_task` 全走 enc:v1（direct/failed 触发后
  验收口径 SQL = 0）；
- 读侧自愈：明文行经 `get_task` 读出后列原文变信封；重复读不重复计数（幂等）；自愈失败才告警；
  自愈写通道在单库拓扑下同持 `session_manager.db_lock`（审查 R1 锁错配）；
  CAS 防旧快照覆盖并发新值；rowcount=0 计 heal_skipped 不计 healed（审查 Optional 口径）；
- 节流：同一 (host, 行类型) 每进程每小时 ≤1 条 WARNING，文案指向 202611220003 且只含键名/行 id；
  report 策略 / 无行 id 读点计 report_only，不与真失败（heal_failed）混计；
- backfill：口径「非空且非 enc:v1:」（含前导空格脏值）；失败行保留原值不置 NULL 且不死循环；
- 指标：`grape_env_extra_plaintext_rows_total{outcome=...}` 已注册并可增长。
"""
import json
import os
import sys
import uuid

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import service
from src.domains.runtime.engine import env_extra_security as sec
from src.domains.runtime.engine.env_extra_security import (
    ENVELOPE_PREFIX, decrypt_env_extra, encrypt_env_extra, heal_env_extra_plaintext_row,
)

REPO = "gitcode:owner/test-3770-envextra"


class _Capture:
    """捕获 WARNING（只关心条数与文案，不落真实日志）。"""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else msg)

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM session_api_tokens WHERE repo_key=?", (REPO,))
        db.commit()


def setup_function():
    _clean()
    sec.reset_plaintext_alert_throttle()


def teardown_function():
    _clean()
    sec.reset_plaintext_alert_throttle()


def _seed_session_and_runtime(repo_key=REPO, agent_type="coding"):
    with db_lock:
        c = db.cursor()
        sid = "i3770sess-%s" % uuid.uuid4().hex[:8]
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status, skill) "
            "VALUES (?,?,?,?,'i3770skill')",
            (sid, repo_key, agent_type, "idle"))
        name = "i3770rt-%s" % uuid.uuid4().hex[:6]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES (?,?,'0',1,1,'online','global','')", (name, name))
        rid = c.lastrowid
        db.commit()
    return sid, rid


def _new_task(env_extra=None):
    sid, rid = _seed_session_and_runtime()
    return service.enqueue_task(
        agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
        event_type="test", account="", user_id=0,
        repo_key=REPO, agent_type="coding", skill="i3770skill",
        env_extra=dict(env_extra or {}),
    )


def _insert_plaintext_row(raw: str) -> str:
    tid = f"t_3770{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "skill, prompt, env_extra, status) VALUES (?,?,?,?,?,?,?,?,'completed')",
            (tid, f"ses-{tid}", 0, REPO, "coding", "i3770skill", "p", raw))
        db.commit()
    return tid


def _raw_env_extra(task_id: str):
    with db_lock:
        row = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    return row[0] if row else None


def _expire_token(token):
    from src.api import auth_token
    with db_lock:
        db.cursor().execute(
            "UPDATE session_api_tokens SET expires_at='0' WHERE token_sha256=?",
            (auth_token._sha256(token),))
        db.commit()


@pytest.fixture(autouse=True)
def _reset_lock_state():
    import threading
    import src.domains.runtime.engine.service as _svc
    _svc._lock_conn_state["conn"] = None
    _svc._lock_conn_state["guard"] = threading.Lock()
    yield
    _svc._lock_conn_state["conn"] = None
    _svc._lock_conn_state["guard"] = threading.Lock()


@pytest.fixture
def outcomes(monkeypatch):
    """替换计数转发，断言 outcome 序列（指标本体另有用例）。"""
    seen = []
    monkeypatch.setattr(sec, "_record_plaintext", lambda outcome: seen.append(outcome))
    return seen


# ---------- 写入收口（sync_task_session_token 回写） ----------

def test_token_rotation_writes_envelope_not_plaintext():
    tid = _new_task({"MY_API_KEY": "sek-3770", "GRAPE_SESSION_TOKEN": "tk_3770"})
    before = decrypt_env_extra(_raw_env_extra(tid))["GRAPE_SESSION_TOKEN"]
    _expire_token(before)

    got = service.sync_task_session_token(tid)

    raw = _raw_env_extra(tid)
    assert got.startswith("tk_")
    assert got != before
    assert raw.startswith(ENVELOPE_PREFIX), "轮换回写必须是 enc:v1 信封"
    assert "tk_" not in raw and "sek-3770" not in raw, "DB 原文不得再出现明文凭据"
    env = decrypt_env_extra(raw)
    assert env["GRAPE_SESSION_TOKEN"] == got
    assert env["MY_API_KEY"] == "sek-3770", "加密回写不得丢变量"


def test_encryption_failure_fails_task_without_plaintext_fallback(monkeypatch, outcomes):
    tid = _new_task({"MY_API_KEY": "sek-3770b"})
    before = _raw_env_extra(tid)
    token = decrypt_env_extra(before)["GRAPE_SESSION_TOKEN"]
    _expire_token(token)

    def _boom(_env):
        raise RuntimeError("kms down")

    monkeypatch.setattr(service, "_encrypt_env_extra_for_write", _boom)

    assert service.sync_task_session_token(tid) == "", "加密失败必须按任务失败处理（返回空串）"
    raw = _raw_env_extra(tid)
    assert raw == before, "加密失败不得改动行（尤其不得写回明文）"
    assert not raw.lstrip().startswith("{"), "不得回落为明文 JSON"
    assert "write_refused" in outcomes


def test_direct_and_failed_task_rows_keep_acceptance_sql_zero():
    """验收证据（QA #3770 审查 R2）：触发 direct/failed 任务后，验收口径 SQL = 0
    ——写入侧清单收全，任何入口不再产生「非空且非 enc:v1:」的行。"""
    from src.api import event_pipeline as ep
    evt = {"type": "issue_opened", "issue_id": "#3770",
           "repository": "gitcode:owner/test-3770-envextra"}

    ep._create_failed_task(REPO, 9102, "coding", "", "i3770skill", skill_path="",
                           skill_id=1, evt=evt, issue_id="#3770", runtime_id=0)
    tid = ep._create_direct_task(REPO, 9103, "coding", "", "i3770skill", skill_path="",
                                 engine_id=0, evt=evt, skip_event_config=True)
    assert tid, "direct 任务行必须创建成功"

    with db_lock:
        c = db.cursor()
        failed_raw = c.execute(
            "SELECT env_extra FROM agent_tasks WHERE repo_key=? AND event_seq=9102",
            (REPO,)).fetchone()[0]
    assert failed_raw == "" or failed_raw.startswith(ENVELOPE_PREFIX), \
        "failed 快照行不得落明文"
    # 验收 SQL（spec Acceptance）：repo 范围内「非空且非 enc:v1:」行数必须为 0
    with db_lock:
        n = db.cursor().execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=? "
            "AND env_extra IS NOT NULL AND env_extra<>'' "
            "AND env_extra NOT LIKE 'enc:v1:%'", (REPO,)).fetchone()[0]
    assert n == 0, "direct/failed 快照行不得再产生明文 env_extra（写入侧不复发）"


def test_direct_task_encrypt_failure_falls_back_to_empty_not_plaintext(
        monkeypatch, outcomes):
    """写入侧 fail-closed：direct 任务 env_extra 加密失败 → 落空串 + 计数
    write_refused，任务行仍创建、绝不回落明文。"""
    from src.api import event_pipeline as ep

    def _boom(_env):
        raise RuntimeError("kms down")

    monkeypatch.setattr(service, "_encrypt_env_extra_for_write", _boom)

    tid = ep._create_direct_task(REPO, 9104, "coding", "", "i3770skill", skill_path="",
                                 engine_id=0, evt={"issue_id": "#3770"},
                                 skip_event_config=True)
    assert tid, "加密失败不应吞掉任务行（direct 行本无 env 数据，回退空串即可）"
    assert _raw_env_extra(tid) == "", "加密失败不得回落明文（空串不在明文口径内）"
    assert "write_refused" in outcomes


# ---------- 读侧自愈 ----------

def test_plaintext_row_healed_in_place_on_read(outcomes):
    plain = json.dumps({"SECRET_AK": "legacy-3770", "GRAPE_SESSION_TOKEN": "tk_legacy"})
    tid = _insert_plaintext_row(plain)

    task = service.get_task(tid)

    assert task["env_extra"]["SECRET_AK"] == "legacy-3770", "自愈不得影响本次读出"
    raw = _raw_env_extra(tid)
    assert raw.startswith(ENVELOPE_PREFIX), "读侧必须就地自愈为信封"
    assert decrypt_env_extra(raw)["SECRET_AK"] == "legacy-3770"
    assert outcomes == ["healed"], "自愈成功只计 healed，不告警"

    service.get_task(tid)  # 第二次读：已加密，不再命中明文
    assert outcomes == ["healed"], "自愈必须幂等（不重复计数）"


def test_heal_update_is_guarded_and_idempotent():
    tid = _insert_plaintext_row(json.dumps({"A": "1"}))
    sealed = encrypt_env_extra({"A": "1"})
    assert heal_env_extra_plaintext_row(tid, sealed) is True
    assert _raw_env_extra(tid) == sealed
    assert heal_env_extra_plaintext_row(tid, sealed) is False, "已加密行不得被二次覆盖"


def test_heal_cas_does_not_overwrite_concurrent_new_plaintext():
    """QA #3770 审查 Optional：读后行被并发换成新明文（如 token 刚轮换）时，
    自愈不得拿旧快照覆盖新值。"""
    tid = _insert_plaintext_row(json.dumps({"A": "old"}))
    raw_read = _raw_env_extra(tid)
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_tasks SET env_extra=? WHERE id=?",
            (json.dumps({"A": "new", "GRAPE_SESSION_TOKEN": "tk_new3770"}), tid))
        db.commit()

    changed = heal_env_extra_plaintext_row(
        tid, encrypt_env_extra({"A": "old"}), expected_raw=raw_read)

    assert changed is False, "CAS 未命中必须放弃覆盖（返回 False）"
    assert "tk_new3770" in (_raw_env_extra(tid) or ""), "并发新值不得被旧快照覆盖"


def test_heal_holds_session_db_lock_in_single_db_topology(monkeypatch):
    """QA #3770 审查 R1（锁错配）：单库/同域拓扑下 heal 的写通道连接就是
    session_manager.db（autocommit=False 共享连接），必须同持 session_manager.db_lock
    ——否则 heal 的 commit 可能提交其它线程未完成的事务。"""
    from src.kernel import db_registry
    assert not db_registry.uses_own_dsn("runtime"), "本用例前提：单库拓扑（无 runtime 自有 DSN）"

    import src.kernel.session_manager as sm
    inner = sm.db_lock

    class _RecordingLock:
        def __init__(self):
            self.enters = 0

        def __enter__(self):
            self.enters += 1
            return inner.__enter__()

        def __exit__(self, *a):
            return inner.__exit__(*a)

    rec = _RecordingLock()
    monkeypatch.setattr(sm, "db_lock", rec)

    tid = _insert_plaintext_row(json.dumps({"A": "1"}))
    changed = heal_env_extra_plaintext_row(tid, encrypt_env_extra({"A": "1"}))

    assert changed is True
    assert rec.enters == 1, "heal 写通道必须进入 session_manager.db_lock（锁与连接同源）"
    raw = _raw_env_extra(tid)
    assert raw and raw.startswith(ENVELOPE_PREFIX)


def test_heal_skipped_when_row_already_converged(monkeypatch, outcomes):
    """QA #3770 审查 Optional：rowcount=0（行已被并发加密/删除/覆盖）计 heal_skipped，
    不计 healed——healed 只计真正改写的行。"""
    tid = _insert_plaintext_row(json.dumps({"A": "1"}))
    monkeypatch.setattr(sec, "heal_env_extra_plaintext_row", lambda *a, **k: False)

    env = decrypt_env_extra(json.dumps({"A": "1"}), task_id=tid)

    assert env == {"A": "1"}, "未改行不影响本次读出"
    assert outcomes == ["heal_skipped"], "rowcount=0 不计 healed 也不告警"


def test_empty_object_plaintext_row_is_healed(outcomes):
    """验收 SQL 口径是「非空且非 enc:v1:」——`{}` 也算明文行，必须收敛为信封。"""
    tid = _insert_plaintext_row("{}")
    task = service.get_task(tid)
    assert task["env_extra"] == {}
    assert _raw_env_extra(tid).startswith(ENVELOPE_PREFIX)
    assert outcomes == ["healed"]


def test_heal_failure_reports_and_keeps_row(monkeypatch, outcomes):
    plain = json.dumps({"SECRET_AK": "keep-3770"})
    tid = _insert_plaintext_row(plain)
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)

    def _no_heal(*a, **k):
        raise RuntimeError("no runtime db")

    monkeypatch.setattr(sec, "heal_env_extra_plaintext_row", _no_heal)
    env = decrypt_env_extra(plain, task_id=tid)

    assert env == {"SECRET_AK": "keep-3770"}, "自愈失败仍要能读出（任务不因观测失败而失败）"
    assert outcomes == ["heal_failed"]
    assert _raw_env_extra(tid) == plain, "自愈失败不得改动行（凭据不丢）"
    assert len(logs.warnings) == 1


# ---------- 告警节流 / 文案脱敏 ----------

def test_alert_throttled_per_host_and_row_kind(monkeypatch, outcomes):
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    monkeypatch.setenv("GRAPE_HOSTNAME", "ecs-grape-master-001")

    for i in range(5):
        decrypt_env_extra(json.dumps({"K%d" % i: "v"}), plaintext_policy="report")

    assert len(logs.warnings) == 1, "同一 (host, 行类型) 每小时只允许 1 条 WARNING"
    # QA #3770 审查 Optional：report 策略（按设计不自愈）计 report_only，
    # 不与自愈真失败（heal_failed）混计——监控可区分「不自愈」与「失败」。
    assert outcomes.count("report_only") == 5, "超限只计数，不丢数"
    assert outcomes.count("heal_failed") == 0, "report 策略不得计入 heal_failed"

    # 换一台主机 → 各自可观测
    monkeypatch.setenv("GRAPE_HOSTNAME", "ecs-grape-master-002")
    decrypt_env_extra(json.dumps({"K": "v"}), plaintext_policy="report")
    assert len(logs.warnings) == 2


def test_alert_text_points_to_real_migration_and_has_no_values(monkeypatch):
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    monkeypatch.setenv("GRAPE_HOSTNAME", "ecs-grape-master-003")
    tid = _insert_plaintext_row(json.dumps({"MY_API_KEY": "sek-3770-leak", "B": "2"}))

    decrypt_env_extra(json.dumps({"MY_API_KEY": "sek-3770-leak"}), task_id="t_missing3770",
                      plaintext_policy="report")

    text = logs.warnings[0]
    assert "存量明文落库行" in text
    assert "202611220003" in text, "文案必须指向真实 backfill（不是 202611130001）"
    assert "202611130001" not in text
    assert "sek-3770-leak" not in text, "日志不得输出凭据值"
    assert "MY_API_KEY" in text, "允许输出键名"
    assert tid  # 行 id 仅在已知时输出；这里确认粒度是行/键名而非值


def test_unparseable_plaintext_counted_and_alerted(monkeypatch, outcomes):
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    monkeypatch.setenv("GRAPE_HOSTNAME", "ecs-grape-master-004")

    assert decrypt_env_extra("not-json{") == {}
    assert outcomes == ["unparseable"]
    assert "unparseable" in logs.warnings[0]


def test_sealed_row_never_counts_as_plaintext(outcomes):
    sealed = encrypt_env_extra({"A": "1"})
    assert decrypt_env_extra(sealed) == {"A": "1"}
    assert outcomes == [], "信封行不得触发明文计数/告警"


# ---------- backfill ----------

def test_backfill_covers_non_empty_non_envelope_rows():
    from src.api import db_setup
    dirty_id = _insert_plaintext_row('  {"A": "1"}')          # 前导空格：old 口径 LIKE '{%' 会漏
    plain_id = _insert_plaintext_row(json.dumps({"B": "2"}))
    sealed = encrypt_env_extra({"C": "3"})
    sealed_id = _insert_plaintext_row(sealed)
    empty_id = _insert_plaintext_row("")

    db_setup._fn_migrate_env_extra_encryption_2423()

    assert _raw_env_extra(dirty_id).startswith(ENVELOPE_PREFIX)
    assert decrypt_env_extra(_raw_env_extra(dirty_id)) == {"A": "1"}
    assert _raw_env_extra(plain_id).startswith(ENVELOPE_PREFIX)
    assert _raw_env_extra(sealed_id) == sealed, "已加密行不得重复处理"
    assert _raw_env_extra(empty_id) == "", "空行不动（不是明文凭据行）"


def test_backfill_keeps_original_value_on_failure(monkeypatch, outcomes):
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    monkeypatch.setattr(service, "log", logs)   # 迁移告警从 service 侧发出
    bad = json.dumps({"SECRET_AK": "boom-3770"})
    bad_id = _insert_plaintext_row(bad)
    good_id = _insert_plaintext_row(json.dumps({"SECRET_AK": "ok-3770"}))
    orig = sec.encrypt_env_extra

    def _flaky(env):
        if env.get("SECRET_AK") == "boom-3770":
            raise RuntimeError("kms down")
        return orig(env)

    monkeypatch.setattr(sec, "encrypt_env_extra", _flaky)

    from src.api import db_setup
    db_setup._fn_migrate_env_extra_encryption_2423()   # 必须终止（失败行留在原地，不死循环）

    kept = _raw_env_extra(bad_id)
    assert kept == bad, "失败行必须保留原值，不能置 NULL（凭据不丢）"
    assert kept is not None
    assert _raw_env_extra(good_id).startswith(ENVELOPE_PREFIX)
    assert "heal_failed" in outcomes
    assert any("3770" in w for w in logs.warnings), "失败需告警（含迁移口径）"
    assert any(bad_id in w or "加密失败" in w for w in logs.warnings)


def test_backfill_registered_and_wraps_impl():
    from src.api import db_setup
    migs = {m.version: m for m in db_setup._MIGRATIONS}
    assert "202611220003" in migs
    assert migs["202611220003"].kind == "backfill"
    assert migs["202611220003"].name == "issue2423-env-extra-encrypt-backfill"
    # 旧告警文案指向的 202611130001 是无关迁移（autopilot 修复）——按它执行「跑了也没用」
    assert migs["202611130001"].name != "issue2423-env-extra-encrypt-backfill"
    assert "autopilot" in migs["202611130001"].name


# ---------- 指标 ----------

def test_plaintext_rows_counter_registered_and_increments():
    from src.domains.insight import observability as obs

    assert obs.ENV_EXTRA_PLAINTEXT_ROWS_METRIC == "grape_env_extra_plaintext_rows_total"
    assert set(obs.ENV_EXTRA_PLAINTEXT_OUTCOMES) == {
        "healed", "heal_skipped", "report_only",
        "heal_failed", "unparseable", "write_refused"}

    before = obs.registry.get_sample_value(
        "grape_env_extra_plaintext_rows_total", {"outcome": "unparseable"}) or 0
    obs.record_env_extra_plaintext_row("unparseable")
    after = obs.registry.get_sample_value(
        "grape_env_extra_plaintext_rows_total", {"outcome": "unparseable"})
    assert after == before + 1

    # 未知 outcome 归 unknown（不丢数、不新增高基数标签）
    obs.record_env_extra_plaintext_row("whatever")
    assert obs.registry.get_sample_value(
        "grape_env_extra_plaintext_rows_total", {"outcome": "unknown"}) is not None


def test_runtime_records_into_counter_end_to_end(monkeypatch):
    """真实链：读侧命中明文 → 计数落到 registry 同一样本（不再依赖 patch）。"""
    from src.domains.insight import observability as obs

    monkeypatch.setenv("GRAPE_HOSTNAME", "ecs-grape-master-001")
    sec.reset_plaintext_alert_throttle()
    before = obs.registry.get_sample_value(
        "grape_env_extra_plaintext_rows_total", {"outcome": "unparseable"}) or 0

    assert decrypt_env_extra("still-not-json{") == {}

    after = obs.registry.get_sample_value(
        "grape_env_extra_plaintext_rows_total", {"outcome": "unparseable"})
    assert after == before + 1