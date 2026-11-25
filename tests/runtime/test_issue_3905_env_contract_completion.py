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
                               evt={"type": "pr_review", "issue_id": "#3905",
                                    "actor": "alice",
                                    "repository": "owner/test-3905-envcontract",
                                    "payload": {"pr_number": "2170"}},
                               issue_id="#3905", runtime_id=0)
    with db_lock:
        row = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE repo_key=? AND event_seq=9302",
            (REPO,)).fetchone()
    assert row is not None, "失败快照必须落库（重派唯一参数来源）"
    env = decrypt_env_extra(row[0])
    assert env.get("ISSUE_NUMBER") == "3905"
    assert env.get("PR_NUMBER") == "2170", "失败快照必须带 PR 号（重派侧 PR_NUMBER 的唯一来源）"
    assert env.get("ACCOUNT") == "alice"
    assert env.get("REPO_KEY") == REPO


def test_create_failed_task_survives_env_assembly_failure():
    """PR #2170 复审 Optional：build_task_env 装配抛异常时仍须落失败快照。

    失败快照是重派唯一参数来源，装配异常吞掉整行 → 重派命中「无关联任务」；
    降级为最小可解析子集（repo 标识 + ACCOUNT + Issue/PR 编号）即满足重派前置。
    """
    with patch("src.kernel.prompt_builder.build_devops_agent_prompt", return_value=""), \
            patch.object(ep, "build_task_env", side_effect=RuntimeError("boom")):
        ep._create_failed_task(REPO, 9304, "coding", "", "i3905skill", skill_path="",
                               skill_id=0,
                               evt={"type": "issue_opened", "issue_id": "#3905",
                                    "actor": "alice",
                                    "repository": "owner/test-3905-envcontract"},
                               issue_id="#3905", runtime_id=0)
    with db_lock:
        row = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE repo_key=? AND event_seq=9304",
            (REPO,)).fetchone()
    assert row is not None, "装配异常不得丢失败快照（重派唯一环境来源）"
    env = decrypt_env_extra(row[0])
    assert env.get("REPO_KEY") == REPO
    assert env.get("ISSUE_NUMBER") == "3905"
    assert env.get("ACCOUNT") == "alice"


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
    """源码契约：autopilot direct 派发传给 _create_direct_task 的 evt 必须带事件类型，
    否则 _dispatch_ident → resolve_actor_account 自动判定为「不回退属主」。

    守卫必须落在**真实调用点** `_execute_autopilot_direct`（PR #2170 复审 Required：
    原守卫断言 `_execute_autopilot_agent`，那里只是构造 evt，传参在 direct 侧 →
    假绿）。同时要求快照 evt 携带本次执行真实的 env_extra。
    """
    import inspect
    src = inspect.getsource(sch._execute_autopilot_direct)
    assert "evt=None" in src, "direct 侧必须保留 typed evt 形参（复用 agent 侧构造的 evt）"
    assert "autopilot_event" in src, "direct 派发的 evt 必须显式带 autopilot 事件类型"
    assert '"_env_extra"' in src, "快照 evt 必须附本次执行的真实 env_extra"
    assert "_dispatch_evt" in src
    # autopilot 账户解析仍在 agent 侧（创建者 → 属主兜底）单一实现
    _agent_src = inspect.getsource(sch._execute_autopilot_agent)
    assert "_resolve_autopilot_account" in _agent_src
    assert "task_creator_account" in inspect.getsource(sch._resolve_autopilot_account)
    assert "ACCOUNT_MISSING" in inspect.getsource(sch._resolve_autopilot_account)


def test_scheduler_autopilot_direct_passes_typed_evt_and_env_extra(monkeypatch):
    """行为契约（PR #2170 复审 Required）：autopilot direct 建任务时传入的 evt
    必须让 resolve_actor_account 走「仓库属主回退」，且快照带上现场 ACCOUNT /
    GRAPE_WORK_BRANCH——否则 autopilot direct 快照 ACCOUNT 仍为空（重派读侧 degraded）。
    """
    _seed_session()
    captured = {}

    def _fake_create_direct_task(repo_key, seq, agent_type, backend, skill_name,
                               skill_path, engine_id, evt, issue_id="", skill_id=0,
                               skip_event_config=False):
        captured["evt"] = evt
        return ""

    monkeypatch.setattr(sch, "_skip_autopilot_for_takeover", lambda *a, **k: False)
    monkeypatch.setattr(sch, "_update_execution_result", lambda *a, **k: None)
    monkeypatch.setattr(ep, "_create_direct_task", _fake_create_direct_task)
    monkeypatch.setattr(ep, "_get_issue_creator", lambda *a, **k: "")

    env_extra = {"ACCOUNT": "owner", "GRAPE_WORK_BRANCH": "arch-weekly-11-ljt",
                 "REPO_KEY": REPO, "EMPTY_ONE": ""}
    sch._execute_autopilot_direct(
        REPO, {"id": 7}, 42, "issue", "i3905skill", "", "", "", "/tmp/i3905-ws", "prompt",
        "", "[Autopilot:issue]", env_extra, "owner/test-3905-envcontract",
        {"type": "autopilot_event", "repo_key": REPO, "issue_id": "", "payload": {}})

    evt = captured["evt"]
    assert evt.get("type") == "autopilot_event", "direct 建任务 evt 必须带事件类型"
    assert evt["_env_extra"]["ACCOUNT"] == "owner"
    assert evt["_env_extra"]["GRAPE_WORK_BRANCH"] == "arch-weekly-11-ljt"
    assert "EMPTY_ONE" not in evt["_env_extra"], "空值不得覆盖快照侧解析结果"
    # 行为闭环：该 evt 经 _dispatch_ident 的账号解析链能回退到仓库属主
    assert ep.resolve_actor_account(evt, REPO) == "owner"
    # 事件类型缺失即降级（回归对照，证明上面的断言不是恒真）
    assert ep.resolve_actor_account(
        {"repo_key": REPO, "issue_id": "", "payload": {}}, REPO) == ""


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


def test_autopilot_task_does_not_reset_issue_streak(monkeypatch):
    """PR #2170 复审 Optional：autopilot 任务不得把 issue 侧连续全缺计数清零。

    autopilot/chat 本无 ISSUE 变量（full_missing 恒 false），此前任一条即把进程级
    单计数归零，混合流量下「连续 N 次全缺」可能永不触发。
    """
    monkeypatch.setattr(ex, "RUNTIME_CONTRACT_STREAK_ALERT", 2)
    ex.reset_runtime_env_contract_streak()
    errors = []
    with patch.object(ex.log, "warning"), \
            patch.object(ex.log, "error", lambda msg, *a, **k: errors.append(msg)):
        ex._apply_runtime_env_contract(
            {}, event_type="issue_opened", repo_key="",
            label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ", backend_name="hermes")
        ex._apply_runtime_env_contract(
            {}, event_type="autopilot_event", repo_key="",
            label="[Autopilot:issue|repo:owner/test-3905|engine:hermes-1] ",
            backend_name="hermes")
        ex._apply_runtime_env_contract(
            {}, event_type="issue_opened", repo_key="",
            label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ", backend_name="hermes")
    assert errors, "非 issue 域任务不得稀释 issue 侧连续全缺计数"


def test_contract_streak_isolated_per_backend_and_agent_type(monkeypatch):
    """PR #2170 复审 Optional：连续计数按 (backend, agent_type) 分桶，互不清零。"""
    monkeypatch.setattr(ex, "RUNTIME_CONTRACT_STREAK_ALERT", 2)
    ex.reset_runtime_env_contract_streak()
    errors = []
    with patch.object(ex.log, "warning"), \
            patch.object(ex.log, "error", lambda msg, *a, **k: errors.append(msg)):
        ex._apply_runtime_env_contract(
            {}, event_type="issue_opened", repo_key="",
            label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ", backend_name="hermes")
        # 另一 backend + agent_type 的部分缺失任务（full_missing=false）不得清零 hermes/issue
        ex._apply_runtime_env_contract(
            {"ACCOUNT": "alice"}, event_type="issue_opened", repo_key="",
            label="[Agent:qa|repo:owner/test-3905|engine:codex-1] ", backend_name="codex")
        ex._apply_runtime_env_contract(
            {}, event_type="issue_opened", repo_key="",
            label="[Agent:issue|repo:owner/test-3905|engine:hermes-1] ", backend_name="hermes")
    assert errors, "其他 backend/agent_type 的任务不得清零本桶连续全缺计数"


def test_alerts_rule_and_metric_name_are_published():
    """指标名/告警规则单一来源：deploy/alerts.yml 必须消费同一指标名。"""
    from src.domains.insight.observability import RUNTIME_ENV_CONTRACT_MISSING_METRIC
    assert RUNTIME_ENV_CONTRACT_MISSING_METRIC == "grape_runtime_env_contract_missing_total"
    path = os.path.join(ROOT, "deploy", "alerts.yml")
    text = open(path, encoding="utf-8").read()
    assert "GrapeRuntimeEnvContractMissing" in text
    assert RUNTIME_ENV_CONTRACT_MISSING_METRIC in text
