# -*- coding: utf-8 -*-
"""Issue #627：已完成需求的生产续接（发布）子单入口（方案 C：复用现有子任务机制）。

验证点（对应 Spec Acceptance Criteria 6 项 + PR #799 审查修复保留）：
1. completed 需求 + 用户批准 → 创建发布子单（parent_issue_id + is_subtask_mode=1 + 绑定要素），
   子单 stage=sre 直通 sre 执行部署，状态可查询
2. 拒绝链全分支：非终态需求 / 重复提交 / 候选不匹配 / 批准过期 / 目标非法 / 未验收 / 未授权 / 会话 token
3. 拒绝路径保留原验收证据（issue 行与 sre 验收事件不被改动、零写入——不建平台 Issue、不写子单、不增删事件）
4. 回填三分支：deployed_success / deploy_failed / not_executed（阶段 completed ≠ 发布成功），子单终态即发布结果
5. _TASK_STATUS_FLOWS / _STAGE_TO_TASK_STATUS 终态语义未修改；旁路 production_release 表已移除
6. tk_ 会话 token（Agent 执行期）调用创建入口 → 403（未授权拒绝）
"""
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient

from src.api.main import app
from src.session_manager import db, db_lock
from src.auth import grape_account_db

client = TestClient(app)

REPO_KEY = "gitcode:issue627-org/repo"
OWNER = "issue627-org"
REPO = "repo"

_uid = None
_headers = {}
_OTHER_HEADERS = {}   # 非归属用户（未授权调用方）

_SHA = "a" * 40
_SHA2 = "b" * 40
_ENV = "prod-shanghai"
_EVIDENCE = "sre-验收报告#77"

DEFAULT_TTL = 24 * 3600

_MOCK_ISSUE_SEQ = [9000]
_post_mock = None
_patcher = None


def _jwt(user):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user["id"], user["username"], get_token_version(user["id"]))


def _fake_post_gitcode_issue(api_token, owner, repo, title, body_text="", assignee=""):
    """Mock 平台建单：返回自增 issue number；真实建单文案可通过捕获验证。"""
    _MOCK_ISSUE_SEQ[0] += 1
    return {"number": _MOCK_ISSUE_SEQ[0], "title": title,
            "body": body_text or "", "html_url": f"https://gitcode.com/{owner}/{repo}/issues/{_MOCK_ISSUE_SEQ[0]}"}


def setup_module():
    global _uid, _headers, _OTHER_HEADERS, _patcher, _post_mock
    from src.api.db_setup import init_database
    init_database()
    _clean()
    _uname = f"issue627_user_{uuid.uuid4().hex[:6]}"
    _other = f"issue627_other_{uuid.uuid4().hex[:6]}"
    _uid = (grape_account_db.create_grape_account(_uname, email=None, password="pw123")
            or grape_account_db.get_grape_account_by_username(_uname))
    _other_acc = (grape_account_db.create_grape_account(_other, email=None, password="pw123")
                  or grape_account_db.get_grape_account_by_username(_other))
    _headers = {"Authorization": f"Bearer {_jwt(_uid)}"}
    _OTHER_HEADERS = {"Authorization": f"Bearer {_jwt(_other_acc)}"}
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
            (REPO_KEY, OWNER, REPO, f"{OWNER}/{REPO}"))
        # 归属用户托管该仓库（a2a jwt 分支鉴权 + 派发链路需要）
        c.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
            "VALUES (?,?,?,?,1,1)",
            (_uid["id"], REPO_KEY, OWNER, REPO))
        db.commit()
    # 平台建单 mock（沿用 test_issues_api 建单测试模式）：测试环境无真实 GitCode
    _patcher = mock.patch("src.api.issues_center._post_gitcode_issue",
                          side_effect=_fake_post_gitcode_issue)
    _post_mock = _patcher.start()


def teardown_module():
    global _patcher, _post_mock
    if _patcher:
        _patcher.stop()
        _patcher = None
        _post_mock = None
    _clean()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (REPO_KEY,))
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("issue", "issue_state", "events", "agent_tasks", "production_release"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _seed_issue(status="completed", stage="done", number=77):
    """建 issue 卡片行（用户归属 = _uid），返回卡片 id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, "
            "completed_reason, completed_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (_uid["id"], "issue627_tester", REPO_KEY, number, f"Task #{number}", status,
             "user_confirmed"))
        tid = c.lastrowid
        db.commit()
    if stage:
        from src.api.session_store import set_issue_stage
        set_issue_stage(REPO_KEY, f"#{number}", stage, "user")
    return tid


def _seed_sre_event(number=77, sha=_SHA, status="completed"):
    """建一条已完成 sre 验收事件（payload 携带候选 SHA + 验收证据）。"""
    from src.api.event_store import append_event
    return append_event(
        repo_key=REPO_KEY, event_type="a2a_event", title=f"Task #{number}",
        issue_id=f"#{number}", source="a2a", agent_type="sre", actor="sre",
        summary="SRE 验收完成", status=status,
        payload={"issue_number": number, "candidate_sha": sha, "evidence": _EVIDENCE,
                 "context": "sre 验收完成"},
    )


def _body(sha=_SHA, env=_ENV, evidence=_EVIDENCE, approved_at=None):
    return {
        "candidate_sha": sha,
        "target_env": env,
        "evidence_ref": evidence,
        "approved_at": approved_at or datetime.now(timezone.utc).isoformat(),
    }


def _post_create(issue_card_id, body=None, headers=None):
    if headers is None:
        headers = _headers
    return client.post(f"/api/issues/{issue_card_id}/production-release",
                       json=body or _body(), headers=headers)


def _subissue_count(number):
    """统计源需求下已创建的发布子单数（issue 行 parent_issue_number + release_target_env 非空）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM issue WHERE repo_key=? AND parent_issue_number=? "
                  "AND release_target_env != ''",
                  (REPO_KEY, number))
        return c.fetchone()[0]


def _get_subissue_row(number):
    """取源需求下最近一条发布子单 issue 行（方案 C：子单就是 issue 行）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, issue_number, parent_issue_number, is_subtask_mode, "
            "release_candidate_sha, release_target_env, release_evidence_ref, "
            "release_result, release_failure_reason, release_approved_by, release_approved_at, "
            "status, stage, completed_reason "
            "FROM issue WHERE repo_key=? AND parent_issue_number=? AND release_target_env != '' "
            "ORDER BY id DESC LIMIT 1", (REPO_KEY, number))
        return c.fetchone()


def _get_issue_row(number):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_reason, completed_at, title FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (REPO_KEY, number))
        return c.fetchone()


def _get_events_count():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=?", (REPO_KEY,))
        return c.fetchone()[0]


def _latest_sre_a2a_event(number):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq, payload FROM events WHERE repo_key=? AND issue_id=? "
            "AND agent_type='sre' AND event_type='a2a_event' ORDER BY seq DESC LIMIT 1",
            (REPO_KEY, f"#{number}"))
        return c.fetchone()


# ===================== 创建入口：拒绝链 =====================

class TestCreateRejects:
    def test_reject_non_terminal_issue(self):
        """非终态需求（active）→ 400 not_terminal（应走正常流水线，不用发布入口）。"""
        _clean()
        tid = _seed_issue(status="active", stage="sre", number=101)
        r = _post_create(tid)
        assert r.status_code == 400, r.text
        assert "not_terminal" in r.text or "终态" in r.text

    def test_reject_no_sre_acceptance(self):
        """无已完成 sre 事件（未验收）→ 400 no_sre_acceptance。"""
        _clean()
        tid = _seed_issue(status="completed", number=102)
        r = _post_create(tid)
        assert r.status_code == 400, r.text
        assert "no_sre_acceptance" in r.text or "验收" in r.text

    def test_reject_candidate_mismatch(self):
        """候选 SHA 与 sre 验收记录不一致 → 400 candidate_mismatch。"""
        _clean()
        tid = _seed_issue(status="completed", number=103)
        _seed_sre_event(number=103, sha=_SHA)
        r = _post_create(tid, _body(sha=_SHA2))
        assert r.status_code == 400, r.text
        assert "candidate_mismatch" in r.text or "候选" in r.text

    def test_reject_approval_expired(self):
        """批准过期（approved_at 超过 TTL）→ 400 approval_expired。"""
        _clean()
        tid = _seed_issue(status="completed", number=104)
        _seed_sre_event(number=104, sha=_SHA)
        _old = (datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_TTL + 3600)).isoformat()
        r = _post_create(tid, _body(approved_at=_old))
        assert r.status_code == 400, r.text
        assert "approval_expired" in r.text or "过期" in r.text

    def test_reject_invalid_target_env(self):
        """目标环境为空 → 400 invalid_target。"""
        _clean()
        tid = _seed_issue(status="completed", number=105)
        _seed_sre_event(number=105, sha=_SHA)
        r = _post_create(tid, _body(env=""))
        assert r.status_code == 400, r.text
        assert "invalid_target" in r.text or "目标" in r.text

    def test_reject_unauthorized_caller(self):
        """无凭据调用（未授权）→ 401/403（未授权调用拒绝）。"""
        _clean()
        tid = _seed_issue(status="completed", number=106)
        _seed_sre_event(number=106, sha=_SHA)
        r = _post_create(tid, headers={})
        assert r.status_code in (401, 403), r.text
        assert _subissue_count(106) == 0, "未授权调用不得创建发布子单"

    def test_reject_session_token(self):
        """tk_ 会话 token（Agent 执行期）→ 403（require_no_session_token 语义）。"""
        _clean()
        tid = _seed_issue(status="completed", number=107)
        _seed_sre_event(number=107, sha=_SHA)
        r = _post_create(tid, headers={"Authorization": "Bearer tk_fake_session_token"})
        assert r.status_code in (401, 403), r.text
        assert _subissue_count(107) == 0, "会话 token 通道不得创建发布子单"

    def test_reject_invalid_sha_format(self):
        """候选 SHA 非 40 位格式 → 400（绑定一致性前置格式校验）。"""
        _clean()
        tid = _seed_issue(status="completed", number=108)
        _seed_sre_event(number=108, sha=_SHA)
        r = _post_create(tid, _body(sha="shortsha"))
        assert r.status_code == 400, r.text

    def test_rejects_leave_evidence_untouched(self):
        """拒绝路径保留原验收证据：issue 行与 sre 事件零改动、零写入（平台 Issue 未建、子单未建）。"""
        _clean()
        tid = _seed_issue(status="completed", number=109)
        _seed_sre_event(number=109, sha=_SHA)
        _before_issue = _get_issue_row(109)
        _before_events = _get_events_count()
        _mocked_calls = _post_mock.call_count
        r = _post_create(tid, _body(sha=_SHA2))   # candidate_mismatch 拒绝
        assert r.status_code == 400
        assert _get_issue_row(109) == _before_issue, "拒绝路径不得改动 issue 行"
        assert _get_events_count() == _before_events, "拒绝路径不得增删验收事件"
        assert _subissue_count(109) == 0, "拒绝路径不得创建发布子单"
        assert _post_mock.call_count == _mocked_calls, "拒绝路径不得调用平台建单"


# ===================== 创建入口：成功 + 唯一性 =====================

class TestCreateSuccess:
    def test_create_binds_all_elements(self):
        """成功创建：发布子单绑定 父需求/仓库/候选SHA/证据/目标环境/批准来源，stage=sre 直通。"""
        _clean()
        tid = _seed_issue(status="completed", number=120)
        _seed_sre_event(number=120, sha=_SHA)
        r = _post_create(tid)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is True
        assert data.get("sub_issue_number")
        sub_num = data["sub_issue_number"]
        # 落库核验：子单行（issue 表）绑定要素齐全
        row = _get_subissue_row(120)
        assert row, "发布子单 issue 行应存在"
        (_id, _num, _parent, _subtask, _sha, _env, _evid, _res, _reason,
         _ap, _at, _status, _stage, _creason) = row
        assert _num == sub_num
        assert _parent == 120
        assert _subtask == 1, "发布子单必须 is_subtask_mode=1（复用子任务机制）"
        assert _sha == _SHA and _env == _ENV
        assert _evid == _EVIDENCE
        assert _ap == _uid["id"] and _at, "批准来源（用户身份+时间）必须落库"
        assert _status == "processing", f"子单应处于 processing，实际 {_status}"
        assert _stage == "sre", f"子单应直通 sre，实际 {_stage}"
        assert _res in ("", None), f"未回填前 release_result 应为空，实际 {_res}"

    def test_create_dispatches_sre_a2a_event(self):
        """创建即派发 sre a2a_event（挂子单 issue 时间线），payload 携带发布上下文。"""
        _clean()
        tid = _seed_issue(status="completed", number=121)
        _seed_sre_event(number=121, sha=_SHA)
        r = _post_create(tid)
        assert r.status_code == 200, r.text
        sub_num = r.json().get("sub_issue_number")
        row = _latest_sre_a2a_event(sub_num)
        assert row, "应创建 sre a2a_event（子单 issue 时间线）"
        _seq, _payload = row
        import json as _j
        _p = _j.loads(_payload or "{}")
        assert int(_p.get("issue_number") or 0) == sub_num
        assert _p.get("parent_issue") == "#121"
        assert _p.get("is_subtask_mode") in (True, 1)
        assert "production_release" not in _p, "旁路 production_release 锚点应已移除"
        _ctx = _p.get("context", "")
        assert _SHA[:12] in _ctx and _ENV in _ctx, "发布上下文应携带候选SHA/目标环境"

    def test_duplicate_submission_rejected(self):
        """同（需求+SHA+环境）重复提交 → 409 duplicate（唯一性约束）。"""
        _clean()
        tid = _seed_issue(status="completed", number=122)
        _seed_sre_event(number=122, sha=_SHA)
        r1 = _post_create(tid)
        assert r1.status_code == 200, r1.text
        r2 = _post_create(tid)
        assert r2.status_code == 409, r2.text
        assert "duplicate" in r2.text or "重复" in r2.text
        assert _subissue_count(122) == 1, "重复提交不得新建子单"

    def test_query_endpoint_returns_status_and_bindings(self):
        """GET 查询：返回发布子单列表，含状态/结果/失败原因/绑定要素。"""
        _clean()
        tid = _seed_issue(status="completed", number=123)
        _seed_sre_event(number=123, sha=_SHA)
        _post_create(tid)
        r = client.get(f"/api/issues/{tid}/production-release", headers=_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        items = data.get("releases") if isinstance(data, dict) else data
        assert items and len(items) >= 1
        _it = items[0]
        assert _it.get("candidate_sha") == _SHA
        assert _it.get("target_env") == _ENV
        assert _it.get("parent_issue_number") == 123
        assert "status" in _it and "result" in _it and "failure_reason" in _it


# ===================== 结果三分支回填 =====================

class TestResultBranches:
    def test_fill_deployed_success(self):
        """exit 0 + deploy_url 证据 → deployed_success（子单终态）。"""
        from src.api.issues_center import create_release_subissue, _backfill_release_subissue_result
        _clean()
        tid = _seed_issue(status="completed", number=130)
        _seed_sre_event(number=130, sha=_SHA)
        out = _create_direct(130)
        assert out
        sub_num, seq = out
        _backfill_release_subissue_result(
            repo_key=REPO_KEY, issue_number=sub_num, exit_code=0,
            result_text='{"artifacts": ["deploy_url: https://prod.example.com/app"]}')
        row = _get_subissue_row(130)
        assert row[7] == "deployed_success", row
        assert row[8] == ""
        assert row[11] == "completed", "子单终态 status=completed"
        assert row[12] == "done", "子单终态 stage=done"

    def test_fill_deploy_failed(self):
        """exit != 0 → deploy_failed，失败原因可查询。"""
        from src.api.issues_center import _backfill_release_subissue_result
        _clean()
        tid = _seed_issue(status="completed", number=131)
        _seed_sre_event(number=131, sha=_SHA)
        out = _create_direct(131)
        sub_num, seq = out
        _backfill_release_subissue_result(
            repo_key=REPO_KEY, issue_number=sub_num, exit_code=1,
            result_text="部署失败：目标主机不可达")
        row = _get_subissue_row(131)
        assert row[7] == "deploy_failed"
        assert row[8] and "不可达" in row[8], "失败原因应可查询"

    def test_fill_not_executed(self):
        """exit 0 但无部署证据 → not_executed（阶段 completed 不视为发布成功）。"""
        from src.api.issues_center import _backfill_release_subissue_result
        _clean()
        tid = _seed_issue(status="completed", number=132)
        _seed_sre_event(number=132, sha=_SHA)
        out = _create_direct(132)
        sub_num, seq = out
        _backfill_release_subissue_result(
            repo_key=REPO_KEY, issue_number=sub_num, exit_code=0,
            result_text="任务结束但未执行部署")
        row = _get_subissue_row(132)
        assert row[7] == "not_executed"

    def test_completed_stage_is_not_release_success(self):
        """源需求仍是 completed，但发布结果独立判定——两者互不穿透。"""
        from src.api.issues_center import _backfill_release_subissue_result
        _clean()
        tid = _seed_issue(status="completed", number=133)
        _seed_sre_event(number=133, sha=_SHA)
        out = _create_direct(133)
        sub_num, seq = out
        _backfill_release_subissue_result(
            repo_key=REPO_KEY, issue_number=sub_num, exit_code=0, result_text="noop")
        assert _get_issue_row(133)[0] == "completed", "源需求终态不变"
        assert _get_subissue_row(133)[7] == "not_executed", "发布结果独立判定"


def _create_direct(number, sha=_SHA):
    """绕过 HTTP 直建发布子单（供回填三分支测试用），返回 (sub_issue_number, seq)。"""
    from src.api.issues_center import create_release_subissue
    out = create_release_subissue(
        repo_key=REPO_KEY, issue_number=number, candidate_sha=sha,
        target_env=_ENV, evidence_ref=_EVIDENCE,
        approved_by=_uid["id"],
        approved_at=datetime.now(timezone.utc).isoformat(),
        api_token="test_tok", dispatch=False)
    return (out.get("sub_issue_number"), out.get("seq"))


# ===================== 回填集成（complete_task / fail_task） =====================

class TestBackfillIntegration:
    """complete_task/fail_task 钩子按 task.issue_id 反查发布子单回填三分支。"""

    def _mk_sre_task_for_subissue(self, sub_num):
        """为发布子单建 sre 事件 + agent_tasks 行（普通 sre 任务，无旁路锚点）。"""
        import json as _json
        from src.api.event_store import append_event
        from src.runtime.service import enqueue_task
        _seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title=f"Sub #{sub_num}",
            issue_id=f"#{sub_num}", source="a2a", agent_type="sre", actor="",
            summary="生产发布", status="running",
            payload={"repository": f"{OWNER}/{REPO}", "issue_number": sub_num,
                     "context": f"发布子单 #{sub_num} 生产部署", "parent_issue": "#150"})
        _task_id = enqueue_task(
            agent_session_id="sess-issue627", runtime_id=0, event_seq=_seq, pool_agent_type="sre",
            issue_id=f"#{sub_num}", repo_key=REPO_KEY, agent_type="sre", skill="cloud-deploy",
            prompt="p", cwd="/tmp", runtime_session_id="", issue_session_id="",
            event_type="a2a_event", account="", user_id=0, repo_gitcode_token="",
            env_extra={}, skill_path="", skill_id=0, pool_tag="",
            pool_engine_ids="")
        return _task_id, _seq

    def test_complete_task_backfills_deployed_success(self):
        """exit 0 + deploy_url 证据 → 发布子单 deployed_success（钩子链路）。"""
        from src.runtime.service import complete_task
        _clean()
        _seed_issue(status="completed", number=150)
        _seed_sre_event(number=150, sha=_SHA)
        out = _create_direct(150)
        sub_num = out[0]
        task_id, seq = self._mk_sre_task_for_subissue(sub_num)
        complete_task(task_id, '{"artifacts": ["deploy_url: https://prod.example.com/x"]}',
                      exit_code=0)
        row = _get_subissue_row(150)
        assert row[7] == "deployed_success", row
        assert row[11] == "completed", "子单终态 status=completed"
        # B1 加固：completed_reason 应用层拼接（release_<result>），与 SQL 后端无关
        assert row[13] == "release_deployed_success", row

    def test_complete_task_backfills_deploy_failed(self):
        """exit != 0 → deploy_failed（失败原因可查询）。"""
        from src.runtime.service import complete_task
        _clean()
        _seed_issue(status="completed", number=151)
        _seed_sre_event(number=151, sha=_SHA)
        out = _create_direct(151)
        sub_num = out[0]
        task_id, seq = self._mk_sre_task_for_subissue(sub_num)
        complete_task(task_id, "部署失败：镜像拉取超时", exit_code=1)
        row = _get_subissue_row(151)
        assert row[7] == "deploy_failed"
        assert "镜像" in row[8]
        assert row[13] == "release_deploy_failed", row

    def test_complete_task_backfills_not_executed(self):
        """exit 0 但无部署证据 → not_executed（completed ≠ 发布成功）。"""
        from src.runtime.service import complete_task
        _clean()
        _seed_issue(status="completed", number=152)
        _seed_sre_event(number=152, sha=_SHA)
        out = _create_direct(152)
        sub_num = out[0]
        task_id, seq = self._mk_sre_task_for_subissue(sub_num)
        complete_task(task_id, "noop", exit_code=0)
        row = _get_subissue_row(152)
        assert row[7] == "not_executed"

    def test_normal_sre_task_unaffected(self):
        """普通 sre 任务（非发布子单）→ 钩子零影响（不报错不回填）。"""
        from src.runtime.service import complete_task
        from src.api.event_store import append_event
        from src.runtime.service import enqueue_task
        _clean()
        # 普通需求（无 release 字段）走正常 sre
        _seed_issue(status="completed", number=153)
        _seed_sre_event(number=153, sha=_SHA)
        _seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title="t", issue_id="#153",
            source="a2a", agent_type="sre", actor="", summary="普通 sre", status="running",
            payload={"repository": f"{OWNER}/{REPO}", "issue_number": 153})
        task_id = enqueue_task(
            agent_session_id="sess-issue627b", runtime_id=0, event_seq=_seq, pool_agent_type="sre",
            issue_id="#153", repo_key=REPO_KEY, agent_type="sre", skill="cloud-deploy",
            prompt="p", cwd="/tmp", runtime_session_id="", issue_session_id="",
            event_type="a2a_event", account="", user_id=0, repo_gitcode_token="",
            env_extra={}, skill_path="", skill_id=0, pool_tag="",
            pool_engine_ids="")
        complete_task(task_id, '{"artifacts": ["deploy_url: https://x.example.com"]}',
                      exit_code=0)
        assert _subissue_count(153) == 0, "普通 sre 任务不得产生发布子单回填"


class TestStateMachineIntact:
    def test_task_status_flows_completed_still_terminal(self):
        """_TASK_STATUS_FLOWS 终态语义未修改：completed 仍为空元组（无出边）。"""
        from src.api.issues_center import _TASK_STATUS_FLOWS
        assert _TASK_STATUS_FLOWS.get("completed") == ()
        assert "completed" in _TASK_STATUS_FLOWS

    def test_stage_to_task_status_untouched(self):
        """session_store._STAGE_TO_TASK_STATUS 排除逻辑未动（done→completed 映射保留）。"""
        import src.api.session_store as ss
        import inspect
        src_text = inspect.getsource(ss.set_issue_stage)
        assert "'done': 'completed'" in src_text or "\"done\": \"completed\"" in src_text

    def test_production_release_table_removed(self):
        """旁路 production_release 表应已移除（零平行表）。"""
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='production_release'").fetchone()
        assert row is None, "旁路 production_release 表应已移除"


# ===================== PR #799 审查修复保留（R1/R2/R3/R4） =====================

class TestReviewFixesPR799:
    """QA 审查报告 R1（SHA 大小写唯一性）/ R2（fail_task 回填）/ R3（evidence_ref 上限）/ R4（端点同步化）。"""

    def test_r1_case_variant_duplicate_rejected(self):
        """R1：小写建子单后，大写变体（同 SHA 不同大小写）提交 → 409 duplicate，不得建第二张子单。"""
        _clean()
        tid = _seed_issue(status="completed", number=160)
        _seed_sre_event(number=160, sha=_SHA)
        r1 = _post_create(tid, body=_body(sha=_SHA))
        assert r1.status_code == 200, r1.text
        r2 = _post_create(tid, body=_body(sha=_SHA.upper()))
        assert r2.status_code == 409, f"大小写变体应命中唯一性拒绝，实际 {r2.status_code}: {r2.text}"
        assert _subissue_count(160) == 1, "大小写变体不得新建子单"

    def test_r1_sha_normalized_to_lower_on_store(self):
        """R1：落库前统一小写——大写提交后库内 release_candidate_sha 为小写形态。"""
        _clean()
        tid = _seed_issue(status="completed", number=161)
        _seed_sre_event(number=161, sha=_SHA)
        r = _post_create(tid, body=_body(sha=_SHA.upper()))
        assert r.status_code == 200, r.text
        row = _get_subissue_row(161)
        assert row[4] == _SHA, f"落库 SHA 应统一为小写，实际 {row[4]}"
        assert r.json().get("candidate_sha") == _SHA

    def test_r2_fail_task_backfills_deploy_failed(self):
        """R2：worker 失败上报终态（fail_task）→ 发布子单回填 deploy_failed。"""
        from src.runtime.service import fail_task
        _clean()
        _seed_issue(status="completed", number=162)
        _seed_sre_event(number=162, sha=_SHA)
        out = _create_direct(162)
        sub_num = out[0]
        task_id, seq = self._mk_sre_task_for_subissue(sub_num)
        fail_task(task_id, "部署失败：目标主机不可达", exit_code=1)
        row = _get_subissue_row(162)
        assert row[7] == "deploy_failed", f"fail_task 终态后子单必须回填结果，实际 {row[7]}"
        assert row[8] and "不可达" in row[8], "失败原因应可查询"

    def test_r2_fail_task_backfills_then_complete_no_override(self):
        """R2 幂等：fail_task 回填 deploy_failed 后，迟到 complete_task 不得覆盖结果。"""
        from src.runtime.service import fail_task, complete_task
        _clean()
        _seed_issue(status="completed", number=163)
        _seed_sre_event(number=163, sha=_SHA)
        out = _create_direct(163)
        sub_num = out[0]
        task_id, seq = self._mk_sre_task_for_subissue(sub_num)
        fail_task(task_id, "部署失败：节点掉电", exit_code=1)
        complete_task(task_id, '{"artifacts": ["deploy_url: https://x.example.com"]}', exit_code=0)
        row = _get_subissue_row(163)
        assert row[7] == "deploy_failed", "已完成回填的失败结果不得被迟到上报覆盖"

    def test_r2_fail_task_normal_sre_unaffected(self):
        """R2 保险：普通 sre 任务（非发布子单）fail_task 零影响。"""
        from src.runtime.service import fail_task
        from src.api.event_store import append_event
        from src.runtime.service import enqueue_task
        _clean()
        _seed_issue(status="completed", number=164)
        _seed_sre_event(number=164, sha=_SHA)
        _seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title="t", issue_id="#164",
            source="a2a", agent_type="sre", actor="", summary="普通 sre", status="running",
            payload={"repository": f"{OWNER}/{REPO}", "issue_number": 164})
        task_id = enqueue_task(
            agent_session_id="sess-issue627c", runtime_id=0, event_seq=_seq, pool_agent_type="sre",
            issue_id="#164", repo_key=REPO_KEY, agent_type="sre", skill="cloud-deploy",
            prompt="p", cwd="/tmp", runtime_session_id="", issue_session_id="",
            event_type="a2a_event", account="", user_id=0, repo_gitcode_token="",
            env_extra={}, skill_path="", skill_id=0, pool_tag="", pool_engine_ids="")
        fail_task(task_id, "worker 崩溃", exit_code=1)
        assert _subissue_count(164) == 0, "普通 sre 任务失败不得产生发布子单回填"

    def test_r3_evidence_ref_length_capped(self):
        """R3：evidence_ref 超长（>512 字符）→ 请求被拒且零写入。"""
        _clean()
        tid = _seed_issue(status="completed", number=165)
        _seed_sre_event(number=165, sha=_SHA)
        r = _post_create(tid, body=_body(evidence="x" * 600))
        assert r.status_code >= 400, f"超长 evidence_ref 应被拒绝，实际 {r.status_code}"
        assert _subissue_count(165) == 0, "拒绝路径零写入"

    def test_r4_create_endpoint_is_sync(self):
        """R4：创建端点为同步 def（线程池执行），不在事件循环内同步阻塞等待 a2a 派发。"""
        import inspect
        from src.api.routes.issues import create_production_release_endpoint
        assert not inspect.iscoroutinefunction(create_production_release_endpoint), \
            "创建端点应改为同步 def，避免 ThreadPoolExecutor+asyncio.run 阻塞事件循环"

    def _mk_sre_task_for_subissue(self, sub_num):
        """建带发布子单上下文的 sre 事件 + agent_tasks 行（同 TestBackfillIntegration）。"""
        from src.api.event_store import append_event
        from src.runtime.service import enqueue_task
        _seq = append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title=f"Sub #{sub_num}",
            issue_id=f"#{sub_num}", source="a2a", agent_type="sre", actor="",
            summary="生产发布", status="running",
            payload={"repository": f"{OWNER}/{REPO}", "issue_number": sub_num,
                     "context": f"发布子单 #{sub_num} 生产部署", "parent_issue": "#162"})
        _task_id = enqueue_task(
            agent_session_id=f"sess-issue627-r{sub_num}", runtime_id=0, event_seq=_seq,
            pool_agent_type="sre", issue_id=f"#{sub_num}", repo_key=REPO_KEY,
            agent_type="sre", skill="cloud-deploy", prompt="p", cwd="/tmp",
            runtime_session_id="", issue_session_id="", event_type="a2a_event",
            account="", user_id=0, repo_gitcode_token="", env_extra={}, skill_path="",
            skill_id=0, pool_tag="", pool_engine_ids="")
        return _task_id, _seq


# ===================== PR #818 审查修复（唯一性 DB 兜底 + 失败显式上报 + 回声兜底）=====

class TestReviewFixesPR818:
    """QA note 197606972：R1 唯一性部分唯一索引 / R2 绑定失败显式上报 / Optional 回声兜底。"""

    def test_unique_index_exists_and_blocks_duplicate_rows(self):
        """R1：部分唯一索引 uq_release_subissue 存在；DB 层拒绝同绑定第二行（并发兜底）。"""
        import sqlite3 as _sq
        with db_lock:
            c = db.cursor()
            _idx = c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_release_subissue'"
            ).fetchone()
        assert _idx, "部分唯一索引 uq_release_subissue 应已创建（DB 唯一性兜底）"
        _clean()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "parent_issue_number, release_candidate_sha, release_target_env) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_uid["id"], "t", REPO_KEY, 9101, "sub1", 180, _SHA, _ENV))
            db.commit()
            try:
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                    "parent_issue_number, release_candidate_sha, release_target_env) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (_uid["id"], "t", REPO_KEY, 9102, "sub2", 180, _SHA, _ENV))
                db.commit()
                raised = False
            except Exception as _ie:
                db.rollback()
                raised = "unique" in str(_ie).lower()
        assert raised, "同（repo+parent+sha+env）第二行应被唯一索引拒绝"
        # 普通 issue（release_target_env 空）不受部分索引影响
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "parent_issue_number, release_candidate_sha, release_target_env) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_uid["id"], "t", REPO_KEY, 9103, "normal", 180, _SHA, ""))
            db.commit()

    def test_race_unique_violation_maps_to_409(self):
        """R1：并发绕过前置查询命中唯一约束（_sync_release_fields 抛唯一冲突）→ 409 duplicate。"""
        import sqlite3 as _sq
        from src.api.issues_center import create_release_subissue, ProductionReleaseError
        _clean()
        _seed_issue(status="completed", number=181)
        _seed_sre_event(number=181, sha=_SHA)
        with mock.patch("src.api.issues_center._sync_release_fields",
                        side_effect=_sq.IntegrityError(
                            "UNIQUE constraint failed: issue.repo_key, issue.parent_issue_number")):
            try:
                create_release_subissue(
                    repo_key=REPO_KEY, issue_number=181, candidate_sha=_SHA,
                    target_env=_ENV, evidence_ref=_EVIDENCE, approved_by=_uid["id"],
                    approved_at=datetime.now(timezone.utc).isoformat(),
                    api_token="t", dispatch=False)
                raised = None
            except ProductionReleaseError as _pe:
                raised = _pe
        assert raised is not None, "唯一冲突应抛 ProductionReleaseError 而非静默 200"
        assert getattr(raised, "status", None) == 409, \
            f"唯一约束冲突应映射 409，实际 {getattr(raised, 'status', None)}"
        assert raised.code == "duplicate"

    def test_bind_failure_raises_archive_failed(self):
        """R2：绑定要素落库失败（非唯一冲突）→ 显式 release_archive_failed，不再静默 200。"""
        from src.api.issues_center import create_release_subissue, ProductionReleaseError
        _clean()
        _seed_issue(status="completed", number=182)
        _seed_sre_event(number=182, sha=_SHA)
        with mock.patch("src.api.issues_center._sync_release_fields",
                        side_effect=RuntimeError("db locked")):
            try:
                create_release_subissue(
                    repo_key=REPO_KEY, issue_number=182, candidate_sha=_SHA,
                    target_env=_ENV, evidence_ref=_EVIDENCE, approved_by=_uid["id"],
                    approved_at=datetime.now(timezone.utc).isoformat(),
                    api_token="t", dispatch=False)
                raised = None
            except ProductionReleaseError as _pe:
                raised = _pe
        assert raised is not None, "绑定失败应显式抛错（R2 部分失败不得静默）"
        assert getattr(raised, "status", None) == 500
        assert raised.code == "release_archive_failed"

    def test_stage_failure_raises_archive_failed(self):
        """R2：set_issue_stage 失败 → 显式 release_archive_failed（子单不得无阶段路由返回 200）。"""
        from src.api.issues_center import create_release_subissue, ProductionReleaseError
        _clean()
        _seed_issue(status="completed", number=183)
        _seed_sre_event(number=183, sha=_SHA)
        with mock.patch("src.api.session_store.set_issue_stage",
                        side_effect=RuntimeError("stage write failed")):
            try:
                create_release_subissue(
                    repo_key=REPO_KEY, issue_number=183, candidate_sha=_SHA,
                    target_env=_ENV, evidence_ref=_EVIDENCE, approved_by=_uid["id"],
                    approved_at=datetime.now(timezone.utc).isoformat(),
                    api_token="t", dispatch=False)
                raised = None
            except ProductionReleaseError as _pe:
                raised = _pe
        assert raised is not None, "置阶段失败应显式抛错（R2 部分失败不得静默）"
        assert getattr(raised, "status", None) == 500
        assert raised.code == "release_archive_failed"

    def test_is_release_subissue_accepts_hash_prefixed_id(self):
        """Optional-3：issue_opened 回声 issue_id 形如 '#N' 也能识别（原 int('#N') 恒 False）。"""
        from src.api.issues_center import is_release_subissue
        _clean()
        tid = _seed_issue(status="completed", number=184)
        _seed_sre_event(number=184, sha=_SHA)
        r = _post_create(tid)
        assert r.status_code == 200, r.text
        sub_num = r.json()["sub_issue_number"]
        assert is_release_subissue(REPO_KEY, f"#{sub_num}") is True
        assert is_release_subissue(REPO_KEY, sub_num) is True
        assert is_release_subissue(REPO_KEY, "#184") is False, "源需求（非子单）应为 False"

    def test_is_release_subissue_title_prefix_fallback(self):
        """Optional-3：绑定字段落库前的竞态窗口，标题前缀 `发布子单：` 兜底识别。"""
        from src.api.issues_center import is_release_subissue
        _clean()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title) "
                "VALUES (?,?,?,?,?)",
                (_uid["id"], "t", REPO_KEY, 9201,
                 "发布子单：需求 #185 → prod-shanghai @ aaaaaaaaaaaa"))
            db.commit()
        assert is_release_subissue(REPO_KEY, "#9201") is True, \
            "绑定要素落库前（竞态窗口）应按标题前缀兜底识别"
        _seed_issue(status="active", stage="vod", number=9202)
        assert is_release_subissue(REPO_KEY, "#9202") is False, "普通 issue 不得误判"

    def test_legacy_production_release_table_dropped(self):
        """Optional-4：旧库遗留 production_release 表在迁移中被 DROP。"""
        with db_lock:
            c = db.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS production_release (id INTEGER PRIMARY KEY)")
            # 重置本条迁移的盖章，模拟未跑过该迁移的旧库
            c.execute(
                "DELETE FROM grape_t_schema_migrations WHERE version='202611020002'")
            db.commit()
        from src.api.db_setup import init_database
        init_database()
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='production_release'"
            ).fetchone()
        assert row is None, "迁移应 DROP 遗留 production_release 表"
