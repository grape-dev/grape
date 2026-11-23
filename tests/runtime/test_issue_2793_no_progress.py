"""Issue #2793：running 无新日志/无进展超时自愈（配置链 + 活跃度判据 + 调度巡检）。

验收对齐 Spec（评论 201191006）：
- 阈值默认 600s、重派上限默认 1、开关默认开启；per_skill/per_repo 覆盖生效；
- 非法值静默回退默认，坏配置不拖垮主链路；
- 命中后写 NO_PROGRESS_REQUEUE 诊断（task_id/静默时长/阈值/第 N 次）、先标旧执行
  superseded（started_at 置空 → 旧 run_token 失效）再重派一次；
- 达到上限不再重派并留 NO_PROGRESS_EXHAUSTED 诊断；
- no_progress_enabled=false 整体回滚（误杀可回滚）。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import task_timeout as tt
from src.domains.runtime.engine import task_activity as ta
from src.domains.runtime.engine import scheduler

RK = "gitcode:issue2793/no-progress"
SEQ_BASE = 92793


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("RUNTIME_NO_PROGRESS_TIMEOUT", "RUNTIME_NO_PROGRESS_RETRIES",
              "RUNTIME_NO_PROGRESS_ENABLED", "GRAPE_TASK_TIMEOUT_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    _clean()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_i2793_np%'")
            c.execute("DELETE FROM task_messages WHERE task_id LIKE 't_i2793_np%'")
            c.execute("DELETE FROM events WHERE seq >= ? AND seq < ?", (SEQ_BASE, SEQ_BASE + 100))
        except Exception:
            pass
        db.commit()


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _seed_task(tid, seq, started_secs_ago=900, skill="coding", errors=None,
               env_extra="{}", status="running", runtime_id=0, pool_tag="",
               pool_agent_type="", attempts=0, stream_ended=0, self_reported=0,
               exit_code=None, result_text=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "status, prompt, cwd, skill_files, env_extra, skill, event_seq, started_at, errors, "
            "pool_tag, pool_agent_type, attempts, stream_ended, self_reported, exit_code, result_text) "
            "VALUES (?,?,?,?, 'coding', ?, 'x', '', '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, "sess-" + tid, runtime_id, RK, status, env_extra, skill, seq,
             _iso_ago(started_secs_ago),
             json.dumps(errors) if errors is not None else None,
             pool_tag, pool_agent_type, attempts, stream_ended, self_reported,
             exit_code, result_text))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, status, summary, result) "
            "VALUES (?,?,?,'coding','running','A2A: → coding','')",
            (seq, RK, "a2a_event"))
        db.commit()


def _seed_message(tid, seq, secs_ago):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id=?", (tid,))
        c.execute(
            "INSERT INTO task_messages (task_id, seq, type, content, created_at) "
            "VALUES (?,?, 'text', 'progress', ?)",
            (tid, seq, _iso_ago(secs_ago)))
        db.commit()


def _task_row(tid):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT status, started_at, errors, runtime_id, attempts, stream_ended, "
            "self_reported, exit_code, result_text, pool_tag, pool_agent_type "
            "FROM agent_tasks WHERE id=?", (tid,)).fetchone()


def _event_row(seq):
    with db_lock:
        c = db.cursor()
        return c.execute("SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()


def _error_codes(raw):
    try:
        return [e.get("error_code") for e in json.loads(raw or "[]") if isinstance(e, dict)]
    except Exception:
        return []


# ── 配置链 ─────────────────────────────────────────────────────

class TestNoProgressConfig:
    def test_defaults(self):
        t = {"skill": "s", "repo_key": "r"}
        assert tt.no_progress_timeout_sec(t, cfg={}) == (600.0, "default")
        assert tt.no_progress_retries(t, cfg={}) == 1
        assert tt.no_progress_enabled(t, cfg={}) is True

    def test_top_level_config(self):
        cfg = {"no_progress_timeout_sec": 900, "no_progress_retries": 3,
               "no_progress_enabled": False}
        t = {"skill": "s", "repo_key": "r"}
        assert tt.no_progress_timeout_sec(t, cfg=cfg)[0] == 900.0
        assert tt.no_progress_retries(t, cfg=cfg) == 3
        assert tt.no_progress_enabled(t, cfg=cfg) is False

    def test_per_skill_dict_override(self):
        cfg = {"per_skill": {"vod-cli": {"timeout": 7200,
                                         "no_progress_timeout_sec": 240,
                                         "no_progress_retries": 2}}}
        t = {"skill": "VOD-CLI", "repo_key": "r"}  # 大小写不敏感
        v, src = tt.no_progress_timeout_sec(t, cfg=cfg)
        assert (v, src) == (240.0, "per-skill:VOD-CLI")
        assert tt.no_progress_retries(t, cfg=cfg) == 2

    def test_per_repo_dict_override(self):
        cfg = {"per_repo": {"gitcode:o/r": {"no_progress_timeout_sec": 300}}}
        t = {"skill": "s", "repo_key": "gitcode:o/r"}
        assert tt.no_progress_timeout_sec(t, cfg=cfg)[0] == 300.0

    def test_skill_over_repo(self):
        cfg = {"per_skill": {"s": {"no_progress_timeout_sec": 120}},
               "per_repo": {"r": {"no_progress_timeout_sec": 300}}}
        assert tt.no_progress_timeout_sec({"skill": "s", "repo_key": "r"}, cfg=cfg)[0] == 120.0

    def test_task_env_wins(self):
        t = {"skill": "s", "repo_key": "r",
             "env_extra": json.dumps({"RUNTIME_NO_PROGRESS_TIMEOUT": "90",
                                      "RUNTIME_NO_PROGRESS_RETRIES": "0",
                                      "RUNTIME_NO_PROGRESS_ENABLED": "false"})}
        cfg = {"no_progress_timeout_sec": 900}
        assert tt.no_progress_timeout_sec(t, cfg=cfg) == (90.0, "task-env")
        assert tt.no_progress_retries(t, cfg=cfg) == 0
        assert tt.no_progress_enabled(t, cfg=cfg) is False

    def test_invalid_values_fallback(self):
        cfg = {"no_progress_timeout_sec": -5, "no_progress_retries": "bad",
               "no_progress_enabled": "maybe"}
        t = {"skill": "s", "repo_key": "r"}
        assert tt.no_progress_timeout_sec(t, cfg=cfg) == (600.0, "default")
        assert tt.no_progress_retries(t, cfg=cfg) == 1
        assert tt.no_progress_enabled(t, cfg=cfg) is True

    def test_bad_task_env_falls_back_to_config(self):
        t = {"skill": "s", "repo_key": "r",
             "env_extra": json.dumps({"RUNTIME_NO_PROGRESS_TIMEOUT": "not-a-number"})}
        cfg = {"no_progress_timeout_sec": 450}
        assert tt.no_progress_timeout_sec(t, cfg=cfg)[0] == 450.0


# ── 活跃度判据 ─────────────────────────────────────────────────

class TestNoProgressActivity:
    def test_old_message_is_no_activity(self):
        _seed_task("t_i2793_np_act_old", SEQ_BASE + 1)
        _seed_message("t_i2793_np_act_old", 1, 900)
        with db_lock:
            c = db.cursor()
            assert ta.has_no_recent_activity(c, "t_i2793_np_act_old", 600,
                                             now_epoch=time.time()) is True

    def test_recent_message_is_activity(self):
        _seed_task("t_i2793_np_act_new", SEQ_BASE + 2)
        _seed_message("t_i2793_np_act_new", 1, 10)
        with db_lock:
            c = db.cursor()
            assert ta.has_no_recent_activity(c, "t_i2793_np_act_new", 600,
                                             now_epoch=time.time()) is False

    def test_no_messages_is_no_activity(self):
        _seed_task("t_i2793_np_act_none", SEQ_BASE + 3)
        with db_lock:
            c = db.cursor()
            assert ta.has_no_recent_activity(c, "t_i2793_np_act_none", 600,
                                             now_epoch=time.time()) is True


# ── 调度巡检 / 重派 ────────────────────────────────────────────

class TestNoProgressSweep:
    def test_requeue_once_with_diagnostic(self):
        tid, seq = "t_i2793_np_req", SEQ_BASE + 10
        _seed_task(tid, seq, started_secs_ago=900, runtime_id=7, attempts=2,
                   stream_ended=1, self_reported=1, exit_code=1,
                   result_text="partial output")
        _seed_message(tid, 1, 900)
        n = scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert n >= 1
        row = _task_row(tid)
        status, started_at, errors = row[0], row[1], row[2]
        assert status == "queued"
        assert started_at is None  # 旧执行 superseded（run_token 失效）
        # 对齐 transient_retry._do_requeue 的重派复位惯例：runtime 归零、attempts+1、
        # 信号/产物字段复位、pool_tag/pool_agent_type 回填（否则定向任务永久 queued）
        assert row[3] == 0
        assert row[4] == 3
        assert row[5] == 0 and row[6] == 0
        assert row[7] is None and row[8] == ""
        assert row[9] == "coding" and row[10] == "coding"
        errs = [e for e in json.loads(errors) if isinstance(e, dict)]
        diag = [e for e in errs if e.get("error_code") == "NO_PROGRESS_REQUEUE"]
        assert len(diag) == 1
        assert diag[0]["task_id"] == tid
        assert diag[0]["threshold"] == 600.0
        assert diag[0]["silent_sec"] >= 900
        assert diag[0]["attempt"] == 1
        evt = _event_row(seq)
        assert evt[0] == "pending"
        assert "NO_PROGRESS_REQUEUE" in evt[1]

    def test_retry_limit_blocks_requeue_and_records_exhausted(self):
        tid, seq = "t_i2793_np_limit", SEQ_BASE + 11
        prior = [{"error_code": "NO_PROGRESS_REQUEUE", "task_id": tid,
                  "silent_sec": 900, "threshold": 600.0, "attempt": 1, "limit": 1}]
        _seed_task(tid, seq, started_secs_ago=900, errors=prior)
        _seed_message(tid, 1, 900)
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        row = _task_row(tid)
        status, started_at, errors = row[0], row[1], row[2]
        assert status == "running"
        assert started_at is not None
        codes = _error_codes(errors)
        assert codes.count("NO_PROGRESS_REQUEUE") == 1
        assert codes.count("NO_PROGRESS_EXHAUSTED") == 1
        # 再跑一轮不重复写 exhausted 噪音
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert _error_codes(_task_row(tid)[2]).count("NO_PROGRESS_EXHAUSTED") == 1

    def test_recent_activity_skips_requeue(self):
        tid, seq = "t_i2793_np_active", SEQ_BASE + 12
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_message(tid, 1, 5)
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert _task_row(tid)[0] == "running"

    def test_below_threshold_skips_requeue(self):
        tid, seq = "t_i2793_np_young", SEQ_BASE + 13
        _seed_task(tid, seq, started_secs_ago=60)
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert _task_row(tid)[0] == "running"

    def test_disabled_switch_rolls_back(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_ENABLED", "0")
        tid, seq = "t_i2793_np_off", SEQ_BASE + 14
        _seed_task(tid, seq, started_secs_ago=900)
        _seed_message(tid, 1, 900)
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        row = _task_row(tid)
        status, started_at, errors = row[0], row[1], row[2]
        assert status == "running"
        assert started_at is not None
        codes = _error_codes(errors)
        assert "NO_PROGRESS_REQUEUE" not in codes
        # Spec-1：开关关闭时仍留诊断（只诊断、不重派），且不重复刷屏
        assert codes.count("NO_PROGRESS_DISABLED") == 1
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert _error_codes(_task_row(tid)[2]).count("NO_PROGRESS_DISABLED") == 1

    def test_requeue_limit_clamped_to_error_slots(self, monkeypatch):
        """重派计数存于 errors 槽位，配置上限超过槽位数时按槽位截断防无限重派。"""
        monkeypatch.setenv("RUNTIME_NO_PROGRESS_RETRIES", "99")
        tid, seq = "t_i2793_np_clamp", SEQ_BASE + 17
        prior = [{"error_code": "NO_PROGRESS_REQUEUE", "task_id": tid, "attempt": i + 1}
                 for i in range(5)]
        _seed_task(tid, seq, started_secs_ago=900, errors=prior)
        _seed_message(tid, 1, 900)
        scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert _task_row(tid)[0] == "running"
        assert _error_codes(_task_row(tid)[2]).count("NO_PROGRESS_EXHAUSTED") == 1

    def test_per_skill_override_controls_threshold(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "task-timeout.json"
        cfg_path.write_text(json.dumps({
            "per_skill": {"slow-skill": {"no_progress_timeout_sec": 1200},
                          "fast-skill": {"no_progress_timeout_sec": 120}}}),
            encoding="utf-8")
        monkeypatch.setenv("GRAPE_TASK_TIMEOUT_CONFIG", str(cfg_path))

        slow_id, slow_seq = "t_i2793_np_slow", SEQ_BASE + 15
        _seed_task(slow_id, slow_seq, started_secs_ago=900, skill="slow-skill")
        fast_id, fast_seq = "t_i2793_np_fast", SEQ_BASE + 16
        _seed_task(fast_id, fast_seq, started_secs_ago=900, skill="fast-skill")

        n = scheduler._sweep_no_progress_tasks(now_epoch=time.time())
        assert n >= 1
        assert _task_row(slow_id)[0] == "running"
        assert _task_row(fast_id)[0] == "queued"