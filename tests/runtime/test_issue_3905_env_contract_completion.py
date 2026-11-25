"""Issue #3905：运行时 env 契约三变量（ACCOUNT / GRAPE_ISSUE_ID / GRAPE_WORK_BRANCH）
在 issue / autopilot 派发路径稳定就位，不再「全程 degraded env」。

覆盖（对齐 Issue #3905 Spec Acceptance）：
1. 填源：`_create_direct_task` / `_create_failed_task` 的 env 快照不再为空，含
   REPO_* / ISSUE_NUMBER / GRAPE_ISSUE_ID / ACCOUNT（快照是重派/接管/worker 的唯一 env 来源）；
2. 验收：mock **不含 ISSUE_NUMBER** 的 issue 任务（事件只带 issue_id），快照 + 契约函数
   走完后四变量（含 GRAPE_ISSUE_ID / GRAPE_WORK_BRANCH）最终全部就位；
3. 账户兜底：无 actor 的 autopilot 显式放行「仓库属主」回退并留审计
   （ACCOUNT_OWNER_FALLBACK）；人工/a2a 事件路径仍禁止回退（#1819 语义不回归）；
   autopilot 仍不可得时 ACCOUNT_MISSING 的 owner_fallback 为 true；
4. ACP 白名单：backend=hermes 的子进程 env 透传 GRAPE_ISSUE_ID；
5. 可观测：契约缺失按 backend/agent_type/var 计数，连续三变量全缺升级 ERROR；
   deploy/alerts.yml 有可查询/可告警的规则（指标名单一）。
"""
import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.domains.runtime.engine.env_extra_security import decrypt_env_extra  # noqa: E402
from src.domains.runtime.pipeline import event_pipeline as ep  # noqa: E402
from src.domains.runtime.pipeline.account_env import owner_account_for_repo  # noqa: E402
from src.domains.runtime.engine import scheduler as sch  # noqa: E402
from src.domains.runtime.agents import executor as ex  # noqa: E402

REPO = "gitcode:owner/test-3905-envcontract"
REPO_LIKE = REPO + "%"
CONTRACT_KEYS = ("DEVOPS_EVENT_TYPE", "ACCOUNT", "GRAPE_ISSUE_ID", "GRAPE_WORK_BRANCH")


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE ?", (REPO_LIKE,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE ?", (REPO_LIKE,))
        c.execute("DELETE FROM issue WHERE repo_key LIKE ?", (REPO_LIKE,))
        db.commit()


@pytest.fixture(autouse=True)
def _isolate():
    _clean()
    ex.reset_runtime_env_contract_streak()
    yield
    _clean()
    ex.reset_runtime_env_contract_streak()


def _seed_session(agent_type="issue"):
    with db_lock:
        c = db.cursor()
        sid = "i3905sess-%s" % uuid.uuid4().hex[:8]
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status, skill) "
            "VALUES (?,?,?,?,?)",
            (sid, REPO, agent_type, "idle", "i3905skill"))
        db.commit()
    return sid


def _raw_env_extra(task_id):
    with db_lock:
        row = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    return row[0] if row else None


# ---------- 1) 填源：direct / failed 快照不再为空 ----------

def test_create_direct_task_snapshot_carries_issue_env():
    _seed_session()
    evt = {"type": "issue_opened", "issue_id": "#3905", "actor": "alice",
           "repository": "owner/test-3905-envcontract"}

    tid = ep._create_direct_task(REPO, 9301, "issue", "", "i3905skill", skill_path="",
                                 engine_id=0, evt=evt, skip_event_config=True)
    assert tid, "direct 任务行必须创建成功"

    raw = _raw_env_extra(tid)
    assert raw and raw != "{}", "direct 快照不得再落空对象（Issue #3905 主缺陷）"
    env = decrypt_env_extra(raw)
    assert env.get("ISSUE_NUMBER") == "3905", "快照必须带仓库 Issue 编号（契约回退的唯一前置）"
    assert env.get("ACCOUNT") == "alice"
    assert env.get("REPO_FULL_NAME") == "owner/test-3905-envcontract"
    assert env.get("REPO_OWNER") == "owner"
    assert env.get("REPO_KEY") == REPO


def test_create_failed_task_snapshot_carries_issue_and_pr_env():
    with patch("src.kernel.prompt_builder.build_devops_agent_prompt", return_value=""):
        ep._create_failed_task(REPO, 9302, "coding", "", "i3905skill", skill_path="",
                               skill_id=0,
                               evt={"type": "issue_opened", "issue_id": "#3905",
                                    "actor": "alice",
                                    "repository": "owner/test-3905-envcontract"},
                               issue_id="#3905", runtime_id=0)
    with db_lock:
        row = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE repo_key=? AND event_seq=9302",
            (REPO,)).fetchone()
    assert row is not None, "失败快照必须落库（重派唯一参数来源）"
    env = decrypt_env_extra(row[0])
    assert env.get("ISSUE_NUMBER") == "3905"
    assert env.get("ACCOUNT") == "alice"
    assert env.get("REPO_KEY") == REPO


# ---------- 2) 验收：无 ISSUE_NUMBER 的 issue 任务，四变量最终齐备 ----------

def test_issue_task_without_explicit_issue_number_ends_with_four_contract_vars():
    """Spec Acceptance：mock 不含 ISSUE_NUMBER 的事件 → 快照补源 → 契约函数补齐。

    事件只带 ``issue_id``（webhook 常态），env/event 里没有现成的 ISSUE_NUMBER；
    快照组装必须补出它，随后 _apply_runtime_env_contract 才能解析出内部卡片 id 与场景基线。
    """
    _seed_session()
    evt = {"type": "issue_opened", "issue_id": "#3905", "actor": "alice",
           "repository": "owner/test-3905-envcontract"}
    assert "ISSUE_NUMBER" not in evt and "ISSUE_NUMBER" not in (evt.get("payload") or {})

    tid = ep._create_direct_task(REPO, 9303, "issue", "", "i3905skill", skill_path="",
                                 engine_id=0, evt=evt, skip_event_config=True)
    env = dict(decrypt_env_extra(_raw_env_extra(tid)))
    assert env.get("ISSUE_NUMBER") == "3905", "填源后 ISSUE_NUMBER 必须就位"

    with patch("src.domains.runtime.pipeline.event_pipeline.resolve_issue_card_id",
               return_value="18794"), \
            patch("src.domains.runtime.engine.repogit._read_scene_work_branch",
                  return_value="arch-weekly-11-ljt"):
        ex._apply_runtime_env_contract(env, event_type="issue_opened",
                                       account=env.get("ACCOUNT", ""), repo_key=REPO,
                                       label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ",
                                       backend_name="hermes")

    for name in CONTRACT_KEYS:
        assert (env.get(name) or "").strip(), f"契约变量 {name} 必须最终就位"
    # 快照阶段 _inject_issue_pr_env 已解析出内部卡片 id（缺行时会先补建），
    # 契约函数只在缺失时回填，不得用仓库 Issue 编号顶替卡片 id。
    assert env["GRAPE_ISSUE_ID"].isdigit()
    assert env["GRAPE_ISSUE_ID"] != env["ISSUE_NUMBER"]
    assert env["GRAPE_WORK_BRANCH"] == "arch-weekly-11-ljt"


# ---------- 3) 账户兜底：仅 autopilot 放行仓库属主 ----------

def test_owner_account_for_repo_derives_owner_segment():
    assert owner_account_for_repo(REPO) == "owner"
    assert owner_account_for_repo("github:some-org/repo") == "some-org"
    assert owner_account_for_repo("") == ""


def test_autopilot_event_without_actor_falls_back_to_repo_owner(caplog):
    with patch.object(ep, "_get_issue_creator", return_value=""):
        with caplog.at_level("INFO"):
            account = ep.resolve_actor_account(
                {"type": "autopilot_event", "payload": {}}, REPO)
    assert account == "owner"
    text = "\n".join(r.getMessage() for r in caplog.records if "ACCOUNT_" in r.getMessage())
    assert "ACCOUNT_OWNER_FALLBACK" in text, "owner 兜底必须留审计"
    assert "ACCOUNT_MISSING" not in text, "兜底命中后不得再报 ACCOUNT_MISSING"


def test_a2a_event_never_falls_back_to_repo_owner():
    """回归（Issue #1819 安全语义）：人工/事件驱动路径禁止把仓库属主当触发人。"""
    with patch.object(ep, "_get_issue_creator", return_value=""):
        account = ep.resolve_actor_account(
            {"type": "a2a_event", "payload": {"account": "owner/repo"}}, REPO)
    assert account == ""


def test_autopilot_missing_marks_owner_fallback_true(caplog):
    with caplog.at_level("INFO"):
        account = ep.resolve_actor_account(
            {"type": "autopilot_event", "payload": {}}, "")
    assert account == ""
    missing = [r.getMessage() for r in caplog.records
               if "ACCOUNT_MISSING" in r.getMessage()]
    assert missing and '"owner_fallback": true' in missing[-1]


def test_scheduler_autopilot_account_prefers_task_creator():
    with patch("src.domains.runtime.pipeline.account_env.task_creator_account",
               return_value="alice"):
        assert sch._resolve_autopilot_account({"created_by": 7}, REPO, 1) == "alice"


def test_scheduler_autopilot_account_falls_back_to_owner():
    with patch("src.domains.runtime.pipeline.account_env.task_creator_account",
               return_value=""):
        assert sch._resolve_autopilot_account({"created_by": 0}, REPO, 1) == "owner"


def test_scheduler_autopilot_evt_carries_event_type_for_owner_fallback():
    """源码契约：scheduler 传给 direct 派发的 evt 必须带 autopilot 事件类型，
    否则 _dispatch_ident → resolve_actor_account 自动判定为「不回退属主」。"""
    import inspect
    src = inspect.getsource(sch._execute_autopilot_agent)
    assert '"type": "autopilot_event"' in src
    assert "_resolve_autopilot_account" in src
    assert "task_creator_account" in src
    assert "ACCOUNT_MISSING" in src


# ---------- 4) ACP 白名单补 GRAPE_ISSUE_ID ----------

def test_acp_context_whitelist_forwards_grape_issue_id():
    assert "GRAPE_ISSUE_ID" in ex._ACP_CONTEXT_ENV_KEYS
    env = ex._build_acp_envs({}, {"GRAPE_ISSUE_ID": "18794", "ACCOUNT": "alice",
                                  "SECRET_NOT_ALLOWED": "x"})
    assert env["GRAPE_ISSUE_ID"] == "18794"
    assert env["ACCOUNT"] == "alice"
    assert "SECRET_NOT_ALLOWED" not in env, "白名单之外的变量不得透传"


# ---------- 5) 可观测：结构化计数 + 连续全缺告警 ----------

def test_contract_missing_is_counted_by_backend_agent_type_and_var():
    seen = []
    with patch("src.domains.insight.observability.record_runtime_env_contract_missing",
               lambda backend, agent_type, var: seen.append((backend, agent_type, var))):
        ex._apply_runtime_env_contract(
            {"DEVOPS_EVENT_TYPE": "issue_opened"}, event_type="issue_opened",
            repo_key=REPO, label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ",
            backend_name="hermes")
    assert seen, "契约缺失必须结构化计数"
    assert ("hermes", "issue", "ACCOUNT") in seen or ("hermes", "issue", "ACCOUNT") in [
        (b.lower(), a.lower(), v.upper()) for b, a, v in seen]
    assert {v for _b, _a, v in seen} == {"ACCOUNT", "GRAPE_ISSUE_ID", "GRAPE_WORK_BRANCH"}


def test_consecutive_full_missing_upgrades_to_error(monkeypatch):
    monkeypatch.setattr(ex, "RUNTIME_CONTRACT_STREAK_ALERT", 2)
    ex.reset_runtime_env_contract_streak()
    errors = []
    with patch.object(ex.log, "warning"), \
            patch.object(ex.log, "error", lambda msg, *a, **k: errors.append(msg)):
        for _ in range(2):
            ex._apply_runtime_env_contract(
                {}, event_type="issue_opened", repo_key="",
                label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ",
                backend_name="hermes")
    assert errors, "连续 N 次三变量全缺必须升级为 ERROR（非仅 WARNING）"
    assert "consecutive" in errors[-1]


def test_autopilot_task_reports_only_event_type_and_account():
    """autopilot 无 Issue 上下文：卡片 id / 场景基线不适用，不得再报（否则计数永不清零）。"""
    warnings = []
    with patch.object(ex.log, "warning", lambda msg, *a, **k: warnings.append(msg)):
        ex._apply_runtime_env_contract(
            {}, event_type="autopilot_event", repo_key=REPO,
            label="[Autopilot:issue|repo:owner/test-3905|engine:hermes-1] ",
            backend_name="hermes")
    text = " ".join(warnings)
    assert "ACCOUNT" in text
    assert "GRAPE_ISSUE_ID" not in text
    assert "GRAPE_WORK_BRANCH" not in text


def test_issue_scoped_task_still_reports_all_three_when_number_missing():
    """issue 域任务缺 ISSUE_NUMBER 时仍须报三变量（缺陷可观测性不因 scope 收窄而丢失）。"""
    warnings = []
    with patch.object(ex.log, "warning", lambda msg, *a, **k: warnings.append(msg)):
        ex._apply_runtime_env_contract(
            {"DEVOPS_EVENT_TYPE": "issue_opened"}, event_type="issue_opened", repo_key=REPO,
            label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ", backend_name="hermes")
    text = " ".join(warnings)
    for name in ("ACCOUNT", "GRAPE_ISSUE_ID", "GRAPE_WORK_BRANCH"):
        assert name in text


def test_single_partial_missing_does_not_raise_streak_alert(monkeypatch):
    monkeypatch.setattr(ex, "RUNTIME_CONTRACT_STREAK_ALERT", 1)
    ex.reset_runtime_env_contract_streak()
    errors = []
    with patch.object(ex.log, "warning"), \
            patch.object(ex.log, "error", lambda msg, *a, **k: errors.append(msg)):
        ex._apply_runtime_env_contract(
            {"ACCOUNT": "alice", "GRAPE_ISSUE_ID": "18794", "GRAPE_WORK_BRANCH": "b"},
            event_type="issue_opened", repo_key=REPO,
            label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ", backend_name="hermes")
    assert not errors, "三变量齐备时不应触发连续全缺告警"


def test_alerts_rule_and_metric_name_are_published():
    """指标名/告警规则单一来源：deploy/alerts.yml 必须消费同一指标名。"""
    from src.domains.insight.observability import RUNTIME_ENV_CONTRACT_MISSING_METRIC
    assert RUNTIME_ENV_CONTRACT_MISSING_METRIC == "grape_runtime_env_contract_missing_total"
    path = os.path.join(ROOT, "deploy", "alerts.yml")
    text = open(path, encoding="utf-8").read()
    assert "GrapeRuntimeEnvContractMissing" in text
    assert RUNTIME_ENV_CONTRACT_MISSING_METRIC in text