"""Issue #4367 C：env_extra 解密失败可观测——诊断槽 + 计数器（不再只是一条 WARNING）。

事故（#4367）：执行侧解不开 kid=dev 信封 → `decrypt_env_extra` 只落一条 WARNING
就按空 env 继续（每小时 52 个任务拿不到下发凭据，日志里无人认领）。本组用例锁定：

- 保留 WARNING（既有检索词「credential key unknown for kid=」不变）；
- `agent_tasks.errors` 结构化诊断槽（code=env_extra_unknown_kid 携 kid、
  diagnostic=True、同 code 幂等覆盖、保留最近 5 条）；
- insight 计数器 `grape_env_extra_decrypt_failures_total{code,kid}` 递增
  （与 record_env_extra_plaintext_row 同级口径，/metrics 出口 + 告警规则消费）；
- 嵌套在 claim 的 db_lock 段内（单库拓扑）时跳过诊断槽写（不提前提交外层
  事务——QA #3770 同款锁序结论），计数/告警不受影响；
- 无 task_id 的读点（worker 多数路径）只计数 + 告警，不写诊断槽。
"""
import base64
import json
import os
import sys
import uuid

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import env_extra_security as sec
from src.domains.runtime.engine.env_extra_security import (
    ENVELOPE_PREFIX, ENV_EXTRA_UNKNOWN_KID, decrypt_env_extra,
)

REPO = "gitcode:owner/test-4367-envextra"

# 伪造信封：kid 不在本进程密钥环 → UnknownKeyError（确定性，不依赖环内容）
_NONCE_B64 = base64.b64encode(b"\x01" * 12).decode()
_CT_B64 = base64.b64encode(b"\x02" * 32).decode()
UNKNOWN_KID_ENVELOPE = f"{ENVELOPE_PREFIX}credX-4367:{_NONCE_B64}:{_CT_B64}"


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
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def _insert_task(errors: str = "") -> str:
    tid = f"t_4367{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "skill, prompt, env_extra, status, errors) VALUES (?,?,?,?,?,?,?,?,'completed',?)",
            (tid, f"ses-{tid}", 0, REPO, "coding", "coding-cli", "p",
             UNKNOWN_KID_ENVELOPE, errors))
        db.commit()
    return tid


def _errors_slot(task_id: str):
    with db_lock:
        row = db.cursor().execute(
            "SELECT errors FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    if not row or not row[0]:
        return []
    parsed = json.loads(row[0])
    return parsed if isinstance(parsed, list) else [row[0]]


def _counter(code: str, kid: str) -> float:
    from src.domains.insight import observability as obs
    return obs.registry.get_sample_value(
        "grape_env_extra_decrypt_failures_total", {"code": code, "kid": kid}) or 0


# ---------- 诊断槽 + 计数器（主链路） ----------

def test_unknown_kid_writes_diag_slot_and_increments_counter(monkeypatch):
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    tid = _insert_task()
    before = _counter(ENV_EXTRA_UNKNOWN_KID, "credX-4367")

    env = decrypt_env_extra(UNKNOWN_KID_ENVELOPE, task_id=tid)

    assert env == {}, "解不开按空 env 处理（读链路不被观测失败拖垮）"
    entries = [e for e in _errors_slot(tid)
               if isinstance(e, dict) and e.get("error_code") == ENV_EXTRA_UNKNOWN_KID]
    assert len(entries) == 1, "agent_tasks.errors 必须写入诊断槽"
    entry = entries[0]
    assert entry["kid"] == "credX-4367"
    assert entry.get("diagnostic") is True, "诊断标记：不被当真实失败原因"
    assert entry.get("ts"), "诊断槽须带时间戳"
    assert _counter(ENV_EXTRA_UNKNOWN_KID, "credX-4367") == before + 1
    assert len(logs.warnings) == 1, "WARNING 保留"
    assert "credential key unknown for kid=credX-4367" in logs.warnings[0]
    assert "按空 env 处理" in logs.warnings[0]


def test_diag_slot_idempotent_per_code_and_keeps_last_five():
    """同 code 幂等覆盖（不因重复失败堆条目）；最多保留 5 条（同 service 口径）。"""
    tid = _insert_task()

    for _ in range(7):
        decrypt_env_extra(UNKNOWN_KID_ENVELOPE, task_id=tid)

    slot = _errors_slot(tid)
    same_code = [e for e in slot
                 if isinstance(e, dict) and e.get("error_code") == ENV_EXTRA_UNKNOWN_KID]
    assert len(slot) <= 5, "诊断槽最多保留 5 条"
    assert len(same_code) == 1, "同 error_code 幂等覆盖"


def test_diag_slot_preserves_existing_entries():
    """已有诊断条目（如 SESSION_TOKEN_*）不得被覆盖/清空。"""
    prior = json.dumps([{"error_code": "SESSION_TOKEN_ISSUE_FAILED",
                         "reason": "x", "diagnostic": True, "ts": "2026-01-01T00:00:00+00:00"}])
    tid = _insert_task(errors=prior)

    decrypt_env_extra(UNKNOWN_KID_ENVELOPE, task_id=tid)

    slot = _errors_slot(tid)
    codes = [e.get("error_code") for e in slot if isinstance(e, dict)]
    assert "SESSION_TOKEN_ISSUE_FAILED" in codes
    assert ENV_EXTRA_UNKNOWN_KID in codes


def test_generic_decrypt_failure_counts_under_generic_code(monkeypatch):
    """非 unknown-kid 的解密失败（环内 kid + 损坏密文）→ env_extra_decrypt_failed。"""
    import base64 as _b64
    from src.kernel import security_config as sc
    import src.domains.mgmt.auth.credential_crypto as cc
    # 显式控制环：kid=dev 在环内（sec._load 可控），配损坏密文 → 非 UnknownKeyError
    monkeypatch.setattr(sc, "_config", None)
    monkeypatch.setattr(sc, "_load",
                        lambda: {"credential_keys": "dev:" + _b64.b64encode(b"0" * 32).decode()})
    monkeypatch.setattr(cc, "_loaded", False)
    monkeypatch.setattr(cc, "_current_key_id", "")
    monkeypatch.setattr(cc, "_keys", {})

    bad_ct = base64.b64encode(b"\xff" * 32).decode()
    envelope = f"{ENVELOPE_PREFIX}dev:{_NONCE_B64}:{bad_ct}"
    before = _counter("env_extra_decrypt_failed", "dev")

    assert decrypt_env_extra(envelope) == {}
    assert _counter("env_extra_decrypt_failed", "dev") == before + 1


def test_no_task_id_counts_and_warns_without_diag_slot(monkeypatch):
    """无 task_id（worker 多数读点）：只计数 + WARNING，不碰 DB。"""
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    before = _counter(ENV_EXTRA_UNKNOWN_KID, "credX-4367")

    assert decrypt_env_extra(UNKNOWN_KID_ENVELOPE) == {}
    assert _counter(ENV_EXTRA_UNKNOWN_KID, "credX-4367") == before + 1
    assert len(logs.warnings) == 1


# ---------- 嵌套持锁降级（QA #3770 同款锁序结论） ----------

def test_nested_db_lock_skips_diag_write_but_counts(monkeypatch):
    """claim 的 db_lock 段内读任务：跳过诊断槽写（不提前提交外层事务），计数照常。"""
    logs = _Capture()
    monkeypatch.setattr(sec, "log", logs)
    tid = _insert_task()
    before = _counter(ENV_EXTRA_UNKNOWN_KID, "credX-4367")

    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET prompt='uncommitted-4367' WHERE id=?", (tid,))
        env = decrypt_env_extra(UNKNOWN_KID_ENVELOPE, task_id=tid)
        db.rollback()

    assert env == {}
    assert _errors_slot(tid) == [], "嵌套持锁不得写诊断槽（外层事务未被提前提交）"
    with db_lock:
        row = db.cursor().execute(
            "SELECT prompt FROM agent_tasks WHERE id=?", (tid,)).fetchone()
    assert row[0] == "p", "诊断槽写不得提交外层未完成事务（rollback 必须能回退）"
    assert _counter(ENV_EXTRA_UNKNOWN_KID, "credX-4367") == before + 1, "计数不受降级影响"
    assert len(logs.warnings) == 1


# ---------- 指标出口与告警（验收口径） ----------

def test_counter_registered_with_contract_codes_and_unknown_bucket():
    from src.domains.insight import observability as obs

    assert obs.ENV_EXTRA_DECRYPT_FAILURES_METRIC == "grape_env_extra_decrypt_failures_total"
    assert set(obs.ENV_EXTRA_DECRYPT_FAILURE_CODES) == {"env_extra_unknown_kid",
                                                       "env_extra_decrypt_failed"}

    before = obs.registry.get_sample_value(
        "grape_env_extra_decrypt_failures_total",
        {"code": "unknown", "kid": "unknown"}) or 0
    obs.record_env_extra_decrypt_failure("whatever-code")
    after = obs.registry.get_sample_value(
        "grape_env_extra_decrypt_failures_total",
        {"code": "unknown", "kid": "unknown"})
    assert after == before + 1, "契约外 code 归 unknown（不丢数、不加高基数）"


def test_alert_rule_watches_the_new_counter():
    """可告警验收：deploy/alerts.yml 消费新计数器（指标名单一来源，防表达式漂移）。"""
    rule = (os.path.join(ROOT, "deploy", "alerts.yml"))
    text = open(rule, encoding="utf-8").read()
    assert "GrapeEnvExtraDecryptFailing" in text
    assert "grape_env_extra_decrypt_failures_total" in text
    assert "env_extra_unknown_kid" in text
