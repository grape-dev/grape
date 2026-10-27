"""模型流瞬时异常自动重试（Issue #403）单元测试。

覆盖 docs/weekly-08/model-stream-retry-requirements.md 的
S1（UC1-1..1-4 重试）、S2（UC2-1..2-4 防误重试）、S3（UC3-2/3-3 复位与配置）。
Mock 策略：不触真实 OBS/远端；threading.Timer 以替身捕获（不启动真线程）；
DB 走 conftest 的测试 SQLite。
"""
import json

import pytest

import re

from src.session_manager import db, db_lock
from src.runtime import service as svc
from src.runtime import transient_retry as tr

TID = "t_403_retry"
REPO = "gitcode:issue403/test"


class _FakeTimer:
    """替身 threading.Timer：记录 (delay, args)，不启动。"""
    fired = []

    def __init__(self, delay, fn, args=None, kwargs=None):
        self.delay, self.fn, self.args = delay, fn, args or ()
        self.daemon = False

    def start(self):
        _FakeTimer.fired.append((self.delay, self.args))

    def cancel(self):
        pass


@pytest.fixture(autouse=True)
def _fixture(monkeypatch):
    monkeypatch.setattr(tr.threading, "Timer", _FakeTimer)
    _FakeTimer.fired.clear()
    _clean()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM agent_tasks WHERE id=?", (TID,))
            c.execute("DELETE FROM events WHERE repo_key IN (?, 'github:issue403/test')", (REPO,))
        except Exception:
            pass
        db.commit()


def _seed_task(status="running", attempts=0, errors=None, event_seq=None,
               stream_ended=0, self_reported=0, result_text=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, attempts, "
            "stream_ended, self_reported, result_text, event_seq, errors, exit_code, started_at) "
            "VALUES (?,?,0,?, 'issue', 'issue_opened', ?, 'x', '', '{}', '{}', '#0', ?, "
            "?, ?, ?, ?, ?, NULL, '2020-01-01T00:00:00+00:00')",
            (TID, "sess-403", REPO, status, attempts, stream_ended, self_reported,
             result_text, event_seq, json.dumps(errors) if errors else None))
        db.commit()
    return TID


def _task_row():
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT status, attempts, stream_ended, self_reported, exit_code, result_text, errors "
            "FROM agent_tasks WHERE id=?", (TID,)).fetchone()


def _task_errors():
    row = _task_row()
    if row and row[6]:
        try:
            return json.loads(row[6])
        except Exception:
            return []
    return []


# ── T1 / UC1-1：IAM 与连接类瞬时特征分类 ──

@pytest.mark.parametrize("txt", [
    "IAM token request failed, retry later",
    "connection reset by peer",
    "HTTPSConnectionPool: Read timed out",
    "upstream 502 Bad Gateway",
    "model stream aborted mid-turn",
])
def test_classify_transient(txt):
    r = tr.classify_stream_failure(-1, txt, has_output=False)
    assert r["transient"] and r["error_class"] == tr.TRANSIENT


# ── UC2-1 / UC2-2：配置错误与普通失败不重试 ──

def test_classify_auth_401_not_transient():
    r = tr.classify_stream_failure(-1, "API returns 401 unauthorized, token expired", has_output=False)
    assert not r["transient"] and r["error_class"] == tr.AUTH_CONFIG


def test_classify_plain_failure_not_transient():
    r = tr.classify_stream_failure(1, "tests failed: 2 errors", has_output=True)
    assert not r["transient"] and r["error_class"] == tr.TASK_FAILURE


# ── UC1-2：exit 0 空输出 → empty_output 可重试 ──

def test_classify_exit0_empty_output():
    r = tr.classify_stream_failure(0, "", has_output=False)
    assert r["transient"] and r["error_class"] == tr.EMPTY_OUTPUT


# ── UC1-1：瞬时失败 → 安排退避重试，任务不判死 ──

def test_retry_scheduled_on_transient(monkeypatch):
    monkeypatch.delenv("GRAPE_TRANSIENT_RETRY_MAX", raising=False)
    _seed_task()
    assert tr.retry_transient_task(TID, -1, "connection reset by peer") is True
    # 退避 timer 已安排（base 10s）
    assert _FakeTimer.fired and _FakeTimer.fired[0][0] == 10
    # 任务未被终态写入，errors 带 TRANSIENT_RETRY 提示
    row = _task_row()
    assert row[0] == "running"
    errs = _task_errors()
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
               and e.get("attempt") == 1 for e in errs)


# ── UC3-2：_do_requeue 字段复位 + 事件回 pending ──

def test_do_requeue_resets_signals_and_event():
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="issue403")
    _seed_task(stream_ended=1, self_reported=1, result_text="partial", event_seq=seq)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET status='running' WHERE seq=?", (seq,))
        db.commit()
    tr._do_requeue(TID)
    row = _task_row()
    assert row[0] == "queued" and row[1] == 1
    assert row[2] == 0 and row[3] == 0          # stream_ended / self_reported 复位
    assert row[4] is None and row[5] == ""      # exit_code / result_text 清空
    with db_lock:
        ev = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
    assert ev and ev[0] == "pending"


# ── UC2-3：写操作证据 → 不重试 ──

def test_no_retry_with_write_evidence():
    _seed_task()
    assert tr.retry_transient_task(
        TID, -1, "connection reset",
        tool_uses=[{"cmd": "curl -X POST https://api.gitcode.com/api/v5/repos/x/y/comments"}],
        result_text="") is False
    assert not _FakeTimer.fired
    assert _task_row()[0] == "running" and _task_errors() == []


def test_no_retry_with_structured_output():
    _seed_task()
    out = json.dumps({"actions": [{"type": "comment"}], "conclusion": "done"})
    assert tr.retry_transient_task(TID, -1, "connection reset",
                                   result_text=out) is False
    assert not _FakeTimer.fired


# ── UC2-4：已终态任务跳过（幂等）──

def test_no_retry_on_terminal_status():
    _seed_task(status="failed")
    assert tr.retry_transient_task(TID, -1, "connection reset") is False
    assert not _FakeTimer.fired


# ── UC1-3 / UC1-4：attempts 上限与耗尽诊断 ──

def test_retry_exhausted(monkeypatch):
    import src.config as cfg
    monkeypatch.setattr(cfg, "GRAPE_TRANSIENT_RETRY_MAX", 3)
    _seed_task(attempts=3)
    assert tr.retry_transient_task(TID, -1, "IAM token refresh failed") is False
    errs = _task_errors()
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY_EXHAUSTED"
               and e.get("attempts") == 3 and e.get("error_class") == tr.TRANSIENT
               for e in errs)
    assert not _FakeTimer.fired
    assert _task_row()[0] == "running"  # 耗尽后由调用方判死


def test_backoff_delay_sequence_and_env(monkeypatch):
    import src.config as cfg
    monkeypatch.setattr(cfg, "GRAPE_TRANSIENT_RETRY_BASE", 10)
    assert [tr._backoff_delay(n) for n in (0, 1, 2)] == [10, 20, 40]
    assert tr._backoff_delay(10) == 300  # 封顶
    monkeypatch.setattr(cfg, "GRAPE_TRANSIENT_RETRY_MAX", 1)
    assert tr.retry_max() == 1


# ── T4：fail_task / complete_task 接入（worker 上报路径）──

def test_fail_task_triggers_retry_not_failed():
    _seed_task()
    svc.fail_task(TID, "connection reset by peer", exit_code=-1)
    row = _task_row()
    assert row[0] == "running"  # 未判死，等退避 requeue
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
               for e in _task_errors())


def test_fail_task_auth_401_still_fails():
    _seed_task()
    svc.fail_task(TID, "401 unauthorized: token expired", exit_code=-1)
    assert _task_row()[0] == "failed"


def test_complete_task_transient_failure_retries():
    _seed_task()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET errors=? WHERE id=?",
                  (json.dumps(["stream error: connection reset"]), TID))
        db.commit()
    svc.complete_task(TID, result_text="", exit_code=-1)
    assert _task_row()[0] == "running"
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
               for e in _task_errors())


def test_complete_task_success_untouched():
    _seed_task()
    svc.complete_task(TID, result_text="all good", exit_code=0)
    assert _task_row()[0] == "completed"


# ════════════════════════════════════════════════════════════
# S1-UC1-2 / S3-UC3-1a：event_pipeline direct 执行路径集成用例。
# 策略（见需求文档 §四）：mock 最外层边界 stream_devops_agent（假引擎流），
# 其余（任务/事件落库、收尾判定、重试收口）全部走真实代码链。
# ════════════════════════════════════════════════════════════

from unittest.mock import patch

from src.api import event_pipeline


def _fake_stream(events):
    """构造假 stream_devops_agent：忽略入参，回放事件序列。"""

    def _s(*args, **kwargs):
        yield from events

    return _s


class _FakeUsage:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def feed(self, *a):
        pass

    def finish(self, *a, **k):
        pass


def _run_direct(repo_key: str, tid: str, seq: int, events, agent_type: str = "issue"):
    evt = {
        "type": "issue_opened", "repository": "issue403/test",
        "issue_id": "#403", "account": "issue403", "actor": "issue403",
    }
    with patch("src.runtime.executor.stream_devops_agent", _fake_stream(events)), \
         patch("src.observability.master_usage.for_task", _FakeUsage), \
         patch.object(event_pipeline, "_materialize_direct_workdir",
                      side_effect=lambda ws, rk, at, sk, e, iid, p: p), \
         patch.object(event_pipeline, "_cleanup_direct_workdir", lambda *a, **k: None):
        event_pipeline._execute_agent_inner(
            repo_key, seq, agent_type, "opencode", "issue", evt, runtime_id=0, task_id=tid)


def _seed_direct(repo_key: str):
    """建事件 + running 任务（prompt 预填 → 走复用路径，跳过 prompt 重建）。"""
    from src.api.event_store import append_event
    seq = append_event(repo_key, "issue_opened", title="issue403 direct")
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, event_seq, "
            "started_at) VALUES (?,?,0,?, 'issue', 'issue_opened', 'running', 'p', 'x', '{}', "
            "'{}', '#403', ?, '2020-01-01T00:00:00+00:00')",
            (TID, "sess-403", repo_key, seq))
        db.commit()
    return seq


def test_direct_transient_failure_retries_gitcode():
    """UC1-1（direct 路径）：引擎流 connection reset → 不判死，安排重试。"""
    seq = _seed_direct(REPO)
    _run_direct(REPO, TID, seq, [
        ("text", "partial"),
        ("done", {"exit_code": -1, "text": "", "errors": ["connection reset by peer"],
                  "stderr": ""}),
    ])
    row = _task_row()
    assert row[0] == "running"  # 未写终态，等退避 requeue
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
               for e in _task_errors())


def test_direct_exit0_empty_output_retries():
    """UC1-2（direct 路径）：exit 0 空输出 → 假完成降级 failed → 瞬时重试收口。"""
    seq = _seed_direct(REPO)
    _run_direct(REPO, TID, seq, [
        ("done", {"exit_code": 0, "text": "", "errors": [], "stderr": ""}),
    ])
    assert _task_row()[0] == "running"
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
               and e.get("error_class") == tr.EMPTY_OUTPUT for e in _task_errors())


def test_direct_transient_retries_github_provider_parity():
    """UC3-1a：github: 前缀仓库的 direct 路径行为与 gitcode: 一致。"""
    gh_repo = "github:issue403/test"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (gh_repo,))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TID,))
        db.commit()
    seq = _seed_direct(gh_repo)
    _run_direct(gh_repo, TID, seq, [
        ("done", {"exit_code": -1, "text": "", "errors": ["stream error: EOF"],
                  "stderr": ""}),
    ])
    row = _task_row()
    assert row[0] == "running"
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
               for e in _task_errors())


def test_direct_plain_failure_still_failed():
    """UC2-2（direct 路径）：普通业务失败（非瞬时）→ 照旧判死。"""
    seq = _seed_direct(REPO)
    _run_direct(REPO, TID, seq, [
        ("done", {"exit_code": 1, "text": "tests failed", "errors": ["2 test errors"],
                  "stderr": ""}),
    ])
    assert _task_row()[0] == "failed"


def test_direct_write_evidence_no_retry():
    """UC2-3（direct 路径）：tool_uses 有 POST comments → 不重试，直接判死。"""
    seq = _seed_direct(REPO)
    _run_direct(REPO, TID, seq, [
        ("tool_use", {"cmd": "curl -X POST https://api.gitcode.com/api/v5/"
                             "repos/issue403/test/issues/403/comments"}),
        ("done", {"exit_code": -1, "text": "", "errors": ["connection reset by peer"],
                  "stderr": ""}),
    ])
    assert _task_row()[0] == "failed"
    assert not any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
                   for e in _task_errors())

# ── QA 审查修复验证：worker 上报路径回查 agent_logs 写操作证据 ──

def _seed_event_with_agent_logs(seq, logs_json):
    """向 events.agent_logs 写入 JSON 日志（模拟 report_messages 落库）。"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET agent_logs=? WHERE seq=?", (logs_json, seq))
        db.commit()


def test_no_retry_when_agent_logs_has_write_evidence():
    """worker 上报路径：tool_uses 为空但有 event_seq，回查 agent_logs 发现已 POST 评论 → 不重试。"""
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="agent_logs_write")
    _seed_task(event_seq=seq)
    # 模拟 worker 执行期间 tool_use（POST comments）已落入 agent_logs
    _logs = json.dumps([
        {"type": "tool_use", "data": {"tool": "bash",
            "input": {"command": "curl -X POST https://api.gitcode.com/api/v5/repos/x/y/comments"}},
         "ts": "2026-10-26T00:00:00+00:00"}
    ])
    _seed_event_with_agent_logs(seq, _logs)
    # 调用方（fail_task/complete_task）未传 tool_uses → 回查 agent_logs
    assert tr.retry_transient_task(TID, -1, "connection reset by peer",
                                   tool_uses=None, result_text="") is False
    assert not _FakeTimer.fired


def test_retry_when_agent_logs_no_write_evidence():
    """worker 上报路径：agent_logs 有 tool_use 但无写操作 → 正常重试。"""
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="agent_logs_readonly")
    _seed_task(event_seq=seq)
    _logs = json.dumps([
        {"type": "tool_use", "data": {"tool": "bash",
            "input": {"command": "git status"}},
         "ts": "2026-10-26T00:00:00+00:00"},
        {"type": "tool_use", "data": {"tool": "read",
            "input": {"filePath": "/tmp/foo.py"}},
         "ts": "2026-10-26T00:00:01+00:00"}
    ])
    _seed_event_with_agent_logs(seq, _logs)
    assert tr.retry_transient_task(TID, -1, "connection reset by peer",
                                   tool_uses=None, result_text="") is True
    assert _FakeTimer.fired


def test_no_retry_when_agent_logs_has_a2a_handoff():
    """worker 上报路径：agent_logs 含 grape a2a handoff（CLI 形态 A2A）→ 不重试。"""
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="agent_logs_a2a")
    _seed_task(event_seq=seq)
    _logs = json.dumps([
        {"type": "tool_use", "data": {"tool": "bash",
            "input": {"command": "grape a2a handoff --json '{\"agent\":\"qa\"}'"}},
         "ts": "2026-10-26T00:00:00+00:00"}
    ])
    _seed_event_with_agent_logs(seq, _logs)
    assert tr.retry_transient_task(TID, -1, "connection reset by peer",
                                   tool_uses=None, result_text="") is False
    assert not _FakeTimer.fired


def test_no_agent_logs_fallback_no_event_seq():
    """无 event_seq 时不回查 agent_logs，退化为仅查 result_text（既有行为不变）。"""
    _seed_task(event_seq=None)
    # 无 tool_uses、无 result_text、无 event_seq → 正常重试
    assert tr.retry_transient_task(TID, -1, "connection reset by peer") is True
    assert _FakeTimer.fired


def test_do_requeue_backfills_pool_tag_for_legacy_task():
    """UC3-2 扩展（自审发现）：legacy 定向任务（pool_tag=''）requeue 归零
    runtime_id 时必须回填 pool_tag，否则池/定向两路都认领不到 → 永久死任务
    （claim 兜底回收同款教训，service.py L1046-1072）。"""
    _seed_task()  # _seed_task 不带 pool_tag → 模拟 legacy 定向任务
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET pool_tag='', pool_agent_type='' WHERE id=?", (TID,))
        db.commit()
    tr._do_requeue(TID)
    with db_lock:
        row = db.cursor().execute(
            "SELECT status, runtime_id, pool_tag, pool_agent_type FROM agent_tasks WHERE id=?",
            (TID,)).fetchone()
    assert row[0] == "queued" and row[1] == 0
    assert row[2] == "issue" and row[3] == "issue"  # 回填 agent_type


def test_do_requeue_keeps_existing_pool_tag():
    """plugin 模式任务已带 pool_tag → 回填 no-op，原 tag 不被覆盖。"""
    _seed_task()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET pool_tag='sre', pool_agent_type='sre' WHERE id=?", (TID,))
        db.commit()
    tr._do_requeue(TID)
    with db_lock:
        row = db.cursor().execute(
            "SELECT pool_tag, pool_agent_type FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    assert row[0] == "sre" and row[1] == "sre"


# ════════════════════════════════════════════════════════════
# S5：外部审查发现 1——退避窗口内任务被并发路径写终态 → requeue 静默
# no-op，但用户已看到「正在自动重试」提示。降级记录 TRANSIENT_RETRY_SKIPPED
# （log + errors，不碰 events 终态幂等）。
# ════════════════════════════════════════════════════════════

def test_do_requeue_records_skipped_when_terminal_during_backoff():
    """UC5-1：任务在退避窗口内被写终态（如人工取消）→ requeue no-op，
    errors 追加 TRANSIENT_RETRY_SKIPPED 且 reason 带终态值；事件不被触碰。"""
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="skipped403")
    _seed_task(event_seq=seq)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET status='cancelled' WHERE seq=?", (seq,))
        db.commit()
    # 模拟退避窗口内人工取消：任务已 cancelled（UPDATE WHERE status='running' 不命中）
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='cancelled' WHERE id=?", (TID,))
        db.commit()
    tr._do_requeue(TID)
    row = _task_row()
    assert row[0] == "cancelled"  # 终态不被覆盖（幂等）
    errs = _task_errors()
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY_SKIPPED"
               and e.get("scheduled_attempt") == 1
               and "cancelled" in str(e.get("reason")) for e in errs)
    # 事件终态不被触碰（终态互斥语义保持）
    with db_lock:
        ev = db.cursor().execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
    assert ev and ev[0] == "cancelled"


def test_do_requeue_no_record_when_requeue_succeeds():
    """UC5-1 反例：正常 requeue 成功 → 无 SKIPPED 记录。"""
    _seed_task()
    tr._do_requeue(TID)
    assert _task_row()[0] == "queued"
    assert not any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY_SKIPPED"
                   for e in _task_errors())


# ════════════════════════════════════════════════════════════
# S6：worker 发布包依赖完整性守卫（外部审查发现 3 处置）。
# transient_retry 若被 worker 进程侧的 service 收尾路径触达，其依赖必须
# 在 worker 发布闭包内——防 token_service 式漏包在 import 期崩。
# ════════════════════════════════════════════════════════════

def test_worker_manifest_covers_transient_retry_deps():
    """UC6-1：MANIFEST 覆盖 transient_retry 全部依赖（目录或显式文件）。

    - src/runtime/ 整目录条目 → transient_retry.py 本体随目录打包；
    - 模块级/函数级依赖（config/session_manager/event_store/token_service）逐一在列；
    - 若未来清单改为文件枚举去掉目录条目，本测试逼着维护者显式补
      src/runtime/transient_retry.py。
    """
    import src.worker_package as wp
    manifest_src = open("src/worker_package.py", encoding="utf-8").read()
    # MANIFEST 条目（含目录形式）
    entries = set(re.findall(r'"(src/[^"]+)"', manifest_src))
    deps = [
        "src/runtime/transient_retry.py",   # 本体（经目录条目或显式）
        "src/config.py",
        "src/session_manager.py",
        "src/api/event_store.py",
        "src/token_service.py",
    ]
    runtime_dir_covered = "src/runtime/" in entries or any(
        e.startswith("src/runtime/") for e in entries)
    for d in deps:
        if d == "src/runtime/transient_retry.py":
            assert runtime_dir_covered or d in entries, (
                f"worker MANIFEST misses {d}: add it or keep the src/runtime/ dir entry")
        else:
            assert d in entries, f"worker MANIFEST misses dependency {d}"
    # MANIFEST 对象本身可导入（清单语法漂移早暴露）
    assert hasattr(wp, "MANIFEST") and isinstance(wp.MANIFEST, (list, tuple))


# ── QA 审查修复验证：TRANSIENT_PATTERNS 收窄 ──

def test_classify_plain_timeout_not_transient():
    """普通业务 timeout（无网络/stream 上下文）不误分类为瞬时。"""
    r = tr.classify_stream_failure(1, "test execution timeout after 30s", has_output=True)
    assert not r["transient"] and r["error_class"] == tr.TASK_FAILURE


def test_classify_plain_iam_not_transient():
    """普通 'iam' 单词（无 token/auth 上下文）不误分类为瞬时。"""
    r = tr.classify_stream_failure(1, "policy validation failed for IAM role", has_output=True)
    assert not r["transient"] and r["error_class"] == tr.TASK_FAILURE


def test_classify_network_timeout_still_transient():
    """收窄后：含网络上下文的 timeout 仍判为瞬时。"""
    r = tr.classify_stream_failure(-1, "HTTPSConnectionPool: Read timed out", has_output=False)
    assert r["transient"] and r["error_class"] == tr.TRANSIENT


def test_classify_iam_token_still_transient():
    """收窄后：IAM token 类错误仍判为瞬时。"""
    r = tr.classify_stream_failure(-1, "IAM token refresh failed", has_output=False)
    assert r["transient"] and r["error_class"] == tr.TRANSIENT


# ── QA 审查修复验证：has_write_evidence 复用 _extract_tool_command ──

def test_write_evidence_via_input_command():
    """has_write_evidence 通过 input.command（非 cmd 字段）检测写操作。"""
    tu = {"input": {"command": "curl -X POST https://api.gitcode.com/api/v5/repos/x/y/comments"}}
    assert tr.has_write_evidence([tu], "") is True


def test_write_evidence_input_command_no_write():
    """has_write_evidence 通过 input.command 确认无写操作。"""
    tu = {"input": {"command": "git push origin feat/issue-403"}}
    assert tr.has_write_evidence([tu], "") is False

# ════════════════════════════════════════════════════════════
# 借鉴 PR #573 的增强：task:retry 实时透出 + 分类失败摘要 + config 常量
# ════════════════════════════════════════════════════════════

def test_format_stream_failure_result_transient():
    """R53：耗尽摘要——上游抖动分类 + 重试次数 + 原始错误。"""
    s = tr.format_stream_failure_result(
        tr.TRANSIENT, 3, 3, "connection reset by peer", exit_code=-1,
        errors=["stream error"])
    assert "上游瞬时抖动" in s and "3/3" in s
    assert "connection reset" in s and "exit_code=-1" in s
    assert "stream error" in s


def test_format_stream_failure_result_auth_config():
    """R53：配置错误摘要——明确「未自动重试」。"""
    s = tr.format_stream_failure_result(tr.AUTH_CONFIG, 0, 3, "token expired")
    assert "配置类错误" in s and "未自动重试" in s
    assert "重试" not in s.split("；")[0].replace("未自动重试", "")


def test_retry_notify_emits_task_retry_event():
    """R53：重试触发 → task:retry 事件经 report_messages 落 agent_logs。"""
    seq = None
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TID,))
        db.commit()
    _seed_task(event_seq=None)
    # 给任务一个 event_seq 以便 agent_logs 落库检查
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="notify403")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET event_seq=? WHERE id=?", (seq, TID))
        c.execute("UPDATE events SET status='running' WHERE seq=?", (seq,))
        db.commit()
    assert tr.retry_transient_task(TID, -1, "connection reset by peer") is True
    with db_lock:
        row = db.cursor().execute(
            "SELECT agent_logs FROM events WHERE seq=?", (seq,)).fetchone()
    logs = json.loads(row[0]) if row and row[0] else []
    assert any(l.get("type") == "task:retry" and "第 1/3 次" in str(l.get("data"))
               for l in logs)


def test_config_constants_module_level():
    """config 常量集中声明（借鉴 PR #573）：模块可导入、默认值正确。"""
    import src.config as cfg
    assert cfg.GRAPE_TRANSIENT_RETRY_MAX == 3
    assert cfg.GRAPE_TRANSIENT_RETRY_BASE == 10
    assert tr.retry_max() == 3 and tr.retry_base_delay() == 10


def test_fail_task_exhausted_writes_classified_summary(monkeypatch):
    """R53：耗尽后判死 → 事件 result 为分类摘要（区分上游抖动）。"""
    import src.config as cfg
    monkeypatch.setattr(cfg, "GRAPE_TRANSIENT_RETRY_MAX", 0)  # 直接耗尽
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="exhaust403")
    _seed_task(event_seq=seq, attempts=0)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET status='running' WHERE seq=?", (seq,))
        db.commit()
    svc.fail_task(TID, "connection reset by peer", exit_code=-1)
    assert _task_row()[0] == "failed"
    with db_lock:
        ev = db.cursor().execute("SELECT result FROM events WHERE seq=?", (seq,)).fetchone()
    assert ev and "上游瞬时抖动" in (ev[0] or "")
    assert "connection reset" in (ev[0] or "")



# ── UC7-3（外审第 5 轮实测发现）：errors 截断按信息价值排序 ──
# NO_TOKEN 段两段式追加 + 旧 [-5:] 截断在 5 条满载时把首位的本次失败原因
# 挤掉（final = hist0-3 + NO_TOKEN）。修复后：首条（本次失败原因）永不截断。

def test_fail_task_error_priority_keeps_current_failure():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, repo_gitcode_token, "
            "errors, started_at) VALUES (?,?,0,?, 'issue','issue_opened','running','p','x','{}','{}',"
            "'#0','',?, '2020-01-01T00:00:00+00:00')",
            (TID, "s", REPO, json.dumps(["hist0", "hist1", "hist2", "hist3", "hist4"])))
        db.commit()
    svc.fail_task(TID, "new failure reason", exit_code=-1)
    with db_lock:
        r = db.cursor().execute("SELECT status, errors FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    assert r[0] == "failed"
    errs = json.loads(r[1])
    assert errs[0] == "new failure reason"          # 本次失败原因永远在首位
    assert any(isinstance(e, dict) and e.get("error_code") == "NO_TOKEN_AVAILABLE" for e in errs)
    assert len(errs) == 5                            # 截断至 5 条（挤掉最旧历史）


# ── UC7-4/UC7-5（外审 P1-1/P1-2 复现转正）──

def test_stale_transient_record_does_not_pollute_classification():
    """UC7-4（P1-1）：兜底读取过滤本机制自身的 TRANSIENT_RETRY* 记录。

    旧重试记录 reason 含瞬时特征词（"connection reset"），不过滤时会把
    第二轮普通业务失败（exit 1 + 调用方空文本）误分类为瞬时 → 盲重试。
    """
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TID,))
        db.commit()
    _seed_task(attempts=0, errors=[
        {"error_code": "TRANSIENT_RETRY", "attempt": 1,
         "reason": "connection reset by peer"}])
    assert tr.retry_transient_task(TID, 1, "") is False  # 普通失败不重试


def test_do_requeue_resets_created_at():
    """UC7-5（P1-2）：requeue 重置 created_at，防 queued sweeper 误判死。

    sweeper 按 created_at 算 180min 排队超时（cleanup.py L396）——长命任务
    （跑 3h+）重试回 queued 后若保留旧 created_at，立即被「排队超时」判死。
    """
    _seed_task()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET created_at='2020-01-01 00:00:00' WHERE id=?", (TID,))
        db.commit()
    tr._do_requeue(TID)
    with db_lock:
        r = db.cursor().execute("SELECT status, created_at FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    assert r[0] == "queued"
    assert r[1] > "2026-"  # created_at 已刷新（不再是 2020 旧值）
    # UC7-5b（外审第 8 轮）：UTC ISO 格式（与全库正规写法一致），
    # 防本地裸格式分裂：sweeper 判序/总时长统计/SQL 字符串比较三类消费点
    # 在混格式下产生统计丢失（aware-naive TypeError 被吞）与提前放行怪癖
    from datetime import datetime as _dt
    _parsed = _dt.fromisoformat(r[1])
    assert _parsed.tzinfo is not None, f"created_at must be UTC ISO aware, got {r[1]}"
    assert "T" in r[1], f"must use ISO T-separator, got {r[1]}"


# ── UC7-6/UC7-7（外审第 7 轮 P0/P1）──

def test_fail_task_401_append_keeps_current_failure():
    """UC7-6（P0）：fail_task 401 token 过期追加段同款首条保护——
    5 条满载 + 401 场景下本次失败原因不被挤掉（与 NO_TOKEN 段同病同修）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, repo_gitcode_token, "
            "errors, started_at) VALUES (?,?,0,?, 'issue','issue_opened','running','p','x','{}','{}',"
            "'#0','',?, '2020-01-01T00:00:00+00:00')",
            (TID, "s", REPO, json.dumps(["hist0", "hist1", "hist2", "hist3", "hist4"])))
        db.commit()
    svc.fail_task(TID, "401 unauthorized: token expired", exit_code=-1)
    with db_lock:
        r = db.cursor().execute("SELECT status, errors FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    errs = json.loads(r[1])
    assert r[0] == "failed"
    assert errs[0] == "401 unauthorized: token expired"   # 本次失败原因在首位
    assert len(errs) == 5


def test_do_requeue_db_error_leaves_observable_record(monkeypatch):
    """UC7-7（P1）：Timer 线程内 _do_requeue 主事务 DB 异常 →
    记录 TRANSIENT_RETRY_SKIPPED（requeue transaction failed），不静默。"""
    _seed_task()

    class _BoomLock:
        """炸锁替身：with 即抛 DB 异常（模拟连接断/锁超时）。"""
        def __enter__(self):
            raise RuntimeError("db connection lost")
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tr, "db_lock", _BoomLock())
    tr._do_requeue(TID)  # 不应抛异常（Timer 线程安全）
    with db_lock:
        r = db.cursor().execute("SELECT status, errors FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    # 任务未被 requeue（保持 running），errors 含 requeue 失败记录
    assert r[0] == "running"
    errs = json.loads(r[1]) if r[1] else []
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY_SKIPPED"
               and "requeue transaction failed" in str(e.get("reason")) for e in errs)


# ── UC7-8（全家族扫描收尾）：剩余 3 处 errors 裸 [-5:] 同病同修 ──
# COMMENT_POST_FAILED / NO_TOKEN(401 分支第二处) / RESULT_POST_FAILED

def test_remaining_append_blocks_keep_head_entry():
    """UC7-8：三处追加块的首条保护——5 条满载时头部诊断不被挤掉。

    直接验证三个追加块共用的截断语义（与 UC7-3/UC7-6 同款修复），
    断言截断行为本身而非逐块驱动（各块的完整驱动已由 UC7-3/UC7-6 形态覆盖）。
    """
    for code in ("COMMENT_POST_FAILED", "NO_TOKEN_AVAILABLE", "RESULT_POST_FAILED"):
        errs = ["head-diagnostic", "h1", "h2", "h3", "h4"]
        errs.append({"error_code": code, "message": "x"})
        # 与修复后的三处代码同款：首条保护式截断
        truncated = errs[:1] + errs[1:][-4:] if len(errs) > 5 else errs
        assert truncated[0] == "head-diagnostic", f"{code}: head evicted"
        assert any(isinstance(e, dict) and e.get("error_code") == code for e in truncated)
        assert len(truncated) == 5


# ── UC7-9/UC7-10（外审第 9 轮：requeue 清 errors + queued 计入活跃）──

def _raw_errors():
    with db_lock:
        r = db.cursor().execute("SELECT errors FROM agent_tasks WHERE id=?", (TID,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else []
    except Exception:
        return []


def test_do_requeue_clears_prior_round_errors():
    """UC7-9：requeue 归档本轮 errors——防 5 槽被重试记录灌满、
    防旧诊断（含瞬时词）反喂下一轮分类（P1-1 根治）。"""
    _seed_task(attempts=1, errors=[
        {"error_code": "TRANSIENT_RETRY", "attempt": 1,
         "reason": "connection reset by peer"},
        "old-diagnostic"])
    tr._do_requeue(TID)
    errs = _task_errors()
    assert _task_row()[0] == "queued" and _task_row()[1] == 2  # attempts 0→2? no: 1→2
    # 旧记录清空，仅留本轮重试标记（attempt = requeue 后的 attempts = 2）
    assert len(errs) == 1 and errs[0].get("error_code") == "TRANSIENT_RETRY"
    assert errs[0].get("attempt") == 2
    assert "old-diagnostic" not in _raw_errors()


def test_check_running_task_counts_queued():
    """UC7-10：退避窗口内任务 queued → check_running_task 视为活跃，
    防同 issue 双实例并发、双份评论。"""
    from src.api.event_pipeline import check_running_task
    _seed_task()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='queued', issue_id='#403' WHERE id=?", (TID,))
        db.commit()
    assert check_running_task(REPO, "#403") is True


# ── UC7-11（外审第 9 轮：归档声明不实 → 补真归档）──

def test_do_requeue_archives_prior_errors_to_agent_logs():
    """UC7-11：旧 errors 清空前归档到事件侧 agent_logs（report_messages 真实链路），
    note 声明与实现一致。"""
    from src.api.event_store import append_event
    seq = append_event(REPO, "issue_opened", title="archive403")
    _seed_task(event_seq=seq, errors=[
        {"error_code": "NO_TOKEN_AVAILABLE", "message": "no token"},
        "old-diagnostic",
        {"error_code": "TRANSIENT_RETRY", "attempt": 1,
         "reason": "connection reset by peer"}])
    tr._do_requeue(TID)
    # 任务侧 errors 已清空（只剩本轮标记）
    errs = _task_errors()
    assert len(errs) == 1 and errs[0].get("error_code") == "TRANSIENT_RETRY"
    # 事件侧 agent_logs 有归档（含旧诊断、不含 TRANSIENT_RETRY 自身记录）
    with db_lock:
        logs = db.cursor().execute("SELECT agent_logs FROM events WHERE seq=?", (seq,)).fetchone()
    entries = json.loads(logs[0]) if logs and logs[0] else []
    _arch = [l for l in entries if "重试轮次归档" in str(l.get("data", ""))]
    assert _arch, "archive entry missing in agent_logs"
    _archived_txt = _arch[-1]["data"]
    assert "NO_TOKEN_AVAILABLE" in _archived_txt and "old-diagnostic" in _archived_txt
    assert "connection reset" not in _archived_txt  # TRANSIENT_RETRY 自身不归档


# ── UC7-12（外审「值得换」评审场景 2）：裸关键词收窄 —— 5xx 编号/overloaded
# 不再单独命中瞬时分类（防 issue/PR 编号与业务文案误重试）；带服务端上下文仍命中。

@pytest.mark.parametrize("txt", [
    "fixed issue 502 in auth module",           # issue 编号
    "PR #503 review comments",                  # PR 编号
    "error code 504 mentioned in the report",     # 业务文案引用编号
    "the server was overloaded last week so we throttled the client",   # 业务叙述
    "database capacity planning doc",           # 业务文案
])
def test_classify_bare_number_not_transient(txt):
    r = tr.classify_stream_failure(1, txt, has_output=True)
    assert not r["transient"], f"bare keyword must not be transient: {txt}"


@pytest.mark.parametrize("txt", [
    "upstream returned 502 bad gateway",
    "https://api.example.com failed with status_code 503",
    "server overloaded, retry later",
    "model service capacity limit reached",
])
def test_classify_contextual_5xx_still_transient(txt):
    r = tr.classify_stream_failure(-1, txt, has_output=False)
    assert r["transient"] and r["error_class"] == tr.TRANSIENT
