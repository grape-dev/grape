# -*- coding: utf-8 -*-
"""11-09 合入特性验收（模拟数据驱动真实函数）。

覆盖：#1564 #1579 #1577 #1547 #1546 #1599 #1545 #1573 #1566
约束：sqlite 测试库（conftest 强制 DEVOPS_DB_BACKEND=sqlite + 独立 test_sessions.db），
     不打网络、不压生产库、不改动生产数据。

说明：
- #1564 的 market 保存/应用两路已有完整回归（tests/test_issue_1564_skill_scope_market_paths.py），
  本文件只补「校验口径」的分支断言，避免重复。
- #1566 属前端渲染，按静态核算口径：用 node 执行源文件里的那行 map 表达式（非重写副本）。
"""
import json
import os
import re
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


def _sfx():
    return uuid.uuid4().hex[:8]


def _mk_skill(scope="user", owner="", sname="", agent_type="issue"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, file_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (sname, sname, "desc", agent_type, sname, "", scope, owner))
        db.commit()
    return sname


def _rm_skill(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


# ============================== #1564 ==============================
def test_1564_user_scope_rejected_in_repo_scope():
    """#1758 固定口径：repo 场景引用 user 技能一律拦（含本人），市场口径同样全拦。"""
    from src.api.routes.scenes import _collect_scene_skill_scope_violations
    repo = f"gitcode:t{_sfx()}/r-{_sfx()}"
    mine, other, shared = f"usk_{_sfx()}", f"usk_{_sfx()}", f"rsk_{_sfx()}"
    _mk_skill("user", "alice", mine)
    _mk_skill("user", "bob", other)
    _mk_skill("repo", "", shared)
    nodes = [
        {"id": "n1", "tag": "coding", "skill": mine},
        {"id": "n2", "tag": "qa", "skill": other},
        {"id": "n3", "tag": "sre", "skill": shared},
        {"id": "n4", "tag": "issue", "skill": mine, "enabled": False},  # 停用节点不校验
    ]
    try:
        repo_v = _collect_scene_skill_scope_violations(
            nodes, repo, username="alice", user_id="1", allow_own_user=True)
        assert sorted(v["node_id"] for v in repo_v) == ["n1", "n2"], repo_v
        market_v = _collect_scene_skill_scope_violations(
            nodes, repo, username="alice", user_id="1", allow_own_user=False)
        assert sorted(v["node_id"] for v in market_v) == ["n1", "n2"], market_v
        assert market_v[0]["expected_scope"] == "repo"
        assert market_v[0]["actual_scope"] == "user"
    finally:
        for s in (mine, other, shared):
            _rm_skill(s)


def test_1564_violation_projection_shape():
    """对外 violations[] 只暴露 5 个字段（owner_id 等内部字段不外泄）。"""
    from src.api.routes.scenes import _project_scene_scope_violation
    v = {"node_id": "n1", "node_tag": "qa", "skill": "s", "actual_scope": "user",
         "expected_scope": "repo", "owner_id": "alice"}
    out = _project_scene_scope_violation(v)
    assert set(out) == {"node_id", "node_tag", "skill", "actual_scope", "expected_scope"}


# ============================== #1579 ==============================
def test_1579_note_hook_pr_number_sources():
    """PR 号来源链：payload 结构化字段 > URL 兜底；Note Hook 的 iid(评论 id) 不得当 PR 号。"""
    from src.api.event_pipeline import _extract_pr_number
    # 1) payload 的 merge_request.iid 命中
    num, src = _extract_pr_number({}, {"merge_request": {"iid": 1011}}, "note_hook_pr")
    assert (num, src) == ("1011", "payload")
    # 2) 只有 noteable_iid
    num, _ = _extract_pr_number({}, {"object_attributes": {"noteable_iid": "#1024"}}, "note_hook_pr")
    assert num == "1024"
    # 3) 结构化 URL 兜底（payload 缺号时）
    num, src = _extract_pr_number(
        {}, {"object_attributes": {"url": "https://gitcode.com/g/grape/pulls/1011#note_5"}},
        "note_hook_pr")
    assert (num, src) == ("1011", "url")
    # 4) Note Hook 的 object_attributes.iid 是评论 id，不得采用
    num, src = _extract_pr_number({}, {"object_attributes": {"iid": 999}}, "note_hook_pr")
    assert (num, src) == ("", "")
    # 5) 普通事件走 object_attributes.iid（契约不变）
    num, _ = _extract_pr_number({}, {"object_attributes": {"iid": "77"}}, "a2a_event")
    assert num == "77"
    # 6) 标题不再参与猜号（真实标题是 MR 标题）
    num, _ = _extract_pr_number({"title": "fix: #1412 note_hook_pr #1011"}, {}, "note_hook_pr")
    assert num == ""


# ============================== #1577 ==============================
def test_1577_env_split_and_single_address_compat():
    """单地址部署：两变量同值（向前兼容）；双地址：业务基址不被 worker 地址覆盖。"""
    from src.user.db import set_meta_value
    from src.prompt_builder import resolve_task_server_env, _resolve_business_server_url
    from src.runtime.agent import _apply_local_worker_base_override

    set_meta_value("task_business_api_url", "https://grape.example.com/api")
    set_meta_value("worker_connect_url", "https://grape.example.com/runtime")
    try:
        env = resolve_task_server_env(runtime_id=0)
        assert env["GRAPE_SERVER_URL"] == "https://grape.example.com/api"
        assert env["RUNTIME_SERVER_URL"] == "https://grape.example.com/runtime"
        # 双地址（path 不同）：本机 worker 覆盖 runtime，业务基址保留
        task = {"server_url": env["RUNTIME_SERVER_URL"]}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:3000")
        assert task["server_url"] == "http://127.0.0.1:3000"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:3000"
        assert env["GRAPE_SERVER_URL"] == "https://grape.example.com/api"
        # 显式业务基址优先于本机回环回退
        assert _resolve_business_server_url(runtime_id=0) == "https://grape.example.com/api"
        # 单地址：同 scheme+host+port+path → 业务基址跟随本机覆盖（改造前语义）
        set_meta_value("task_business_api_url", "https://same.example.com")
        set_meta_value("worker_connect_url", "https://same.example.com/")
        env2 = resolve_task_server_env(runtime_id=0)
        _apply_local_worker_base_override(env2, {"server_url": env2["RUNTIME_SERVER_URL"]},
                                          "http://127.0.0.1:3000")
        assert env2["GRAPE_SERVER_URL"] == "http://127.0.0.1:3000"
    finally:
        set_meta_value("task_business_api_url", "")
        set_meta_value("worker_connect_url", "")


# ============================== #1547 ==============================
def test_1547_codegraph_unindexed_detection():
    """codegraph 占位文案/空目录树 → 降级；正常输出不误判。"""
    from src.project_info.builder import _codegraph_unindexed
    assert _codegraph_unindexed(["", "arch", "mod", "chains"]) is True
    assert _codegraph_unindexed(["No files indexed. Run codegraph index first."]) is True
    assert _codegraph_unindexed(["tree/a/b.py", "no relevant code found"]) is True
    assert _codegraph_unindexed(["src/\n  api/\n  runtime/", "", "modules", "chains"]) is False
    assert _codegraph_unindexed([]) is False


# ============================== #1546 ==============================
def test_1546_agent_task_id_direct_lookup_and_legacy_fallback():
    """agent_task_id 直连优先；历史空值行按 event_seq+created_at 受约束兜底；无命中不猜。"""
    from src.runtime.scheduler import _get_exec_agent_task, ZOMBIE_YOUNG_GRACE_SECONDS
    assert ZOMBIE_YOUNG_GRACE_SECONDS == 120
    atid = f"t_{_sfx()}"
    seq = 900000 + int(_sfx(), 16) % 100000
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, event_type, "
            "status, created_at, prompt) VALUES (?,?,?,?,?,?,?,?)",
            (atid, f"s_{_sfx()}", 1, seq, "autopilot_event", "completed",
             "2026-11-09 10:00:00", "test"))
        db.commit()
        try:
            row = _get_exec_agent_task(c, seq + 1, agent_task_id=atid)
            assert row and row[0] == atid and row[1] == "completed"
            # 历史行：agent_task_id 空，按 event_seq + created_at >= started_at 兜底
            row2 = _get_exec_agent_task(c, seq, agent_task_id="", started_at="2026-11-09 09:00:00")
            assert row2 and row2[0] == atid
            # created_at 早于 started_at → 不匹配（防跨序列撞号）
            assert _get_exec_agent_task(c, seq, started_at="2026-11-09 11:00:00") is None
            # 两路都无 → None，不退回裸 event_seq
            assert _get_exec_agent_task(c, seq + 777, agent_task_id="") is None
        finally:
            c.execute("DELETE FROM agent_tasks WHERE id=?", (atid,))
            db.commit()


# ============================== #1599 ==============================
def test_1599_resume_stage_and_review_outcome():
    """哨兵 owner_agent 回落 issue；noop 拆出机器可读 reason；不改写名义 status。"""
    from src.api.issues_center import _resume_stage
    from src.api.routes.issues import _review_outcome
    assert _resume_stage("pending_review") == "issue"
    assert _resume_stage("bot_takeover") == "issue"
    assert _resume_stage("issue_closed") == "issue"
    assert _resume_stage("") == "issue"
    assert _resume_stage("coding") == "coding"
    assert _review_outcome("noop:already_target") == ("noop", "already_target")
    assert _review_outcome("noop:not_pending_review") == ("noop", "not_pending_review")
    assert _review_outcome("noop") == ("noop", "not_pending_review")
    assert _review_outcome("passed_processing") == ("passed_processing", "")


def test_1599_status_stage_convergence_on_resume():
    """pending_review 恢复：status 翻回 processing，stage 从 pending_review 回落到真实节点。"""
    from src.api.issues_center import _resume_issue_waiting, _read_issue_status
    repo, num = f"gitcode:t{_sfx()}/r", int(_sfx(), 16) % 900000 + 100000
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO issue (repo_key, issue_number, status, stage, owner_agent, "
                  "user_id, assignee) VALUES (?,?,?,?,?,?,?)",
                  (repo, num, "pending_review", "pending_review", "coding", 1, "tester"))
        db.commit()
    try:
        assert _resume_issue_waiting(repo, num) is True
        assert _read_issue_status(repo, num) == "processing"
        with db_lock:
            stage = db.cursor().execute(
                "SELECT stage FROM issue WHERE repo_key=? AND issue_number=?",
                (repo, num)).fetchone()[0]
        assert stage == "coding"  # 哨兵 pending_review → owner_agent 真实节点
        assert _resume_issue_waiting(repo, num) is False  # 幂等
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?",
                                (repo, num))
            db.commit()


# ============================== #1545 ==============================
def test_1545_post_policy_strict_clean_off():
    """strict：无结构化 conclusion 即不回帖；clean：报错文本不回帖；off：应当照贴。"""
    import src.api.event_pipeline as ep

    err = "HTTP 500 upstream error: socket timeout"
    structured = "已修复 3 个文件，测试通过。"
    plain_ok = "顺手把 README 更新了一下，结论如上。"
    os.environ.pop("GRAPE_RESULT_POST_POLICY", None)

    # strict（默认）
    assert ep._result_post_policy() == "strict"
    assert ep._apply_agent_result_policy(structured, structured, "", "t", 1, "r", 1, "coding") == structured
    assert ep._apply_agent_result_policy(err, "", err, "t", 1, "r", 1, "coding") == ""
    assert ep._apply_agent_result_policy(plain_ok, "", plain_ok, "t", 1, "r", 1, "coding") == ""
    # clean
    os.environ["GRAPE_RESULT_POST_POLICY"] = "clean"
    assert ep._apply_agent_result_policy(err, "", err, "t", 1, "r", 1, "coding") == ""
    assert ep._apply_agent_result_policy(plain_ok, "", plain_ok, "t", 1, "r", 1, "coding") == plain_ok
    # off 档单独断言（见下方 test_1545_policy_off_spec_mismatch）
    os.environ.pop("GRAPE_RESULT_POST_POLICY", None)


def test_1545_policy_off_restores_posting():
    """policy=off = 关闭抑制、恢复照贴（修复前实现返回空串＝全抑制，与本断言冲突）。"""
    import src.api.event_pipeline as ep
    os.environ["GRAPE_RESULT_POST_POLICY"] = "off"
    try:
        out = ep._apply_agent_result_policy("已修复 3 个文件。", "已修复 3 个文件。", "", "t", 1, "r", 1, "coding")
    finally:
        os.environ.pop("GRAPE_RESULT_POST_POLICY", None)
    assert out == "已修复 3 个文件。"


# ============================== #1573 ==============================
def test_1573_pr_merged_resumes_pending_review_then_advances():
    """qa 阶段 pending_review + PR 合并 → 先恢复 processing，再推进 stage qa→sre。"""
    from src.api.session_store import set_issue_stage
    from src.api.issues_center import _resume_issue_waiting, _read_issue_status
    repo, num = f"gitcode:t{_sfx()}/r", int(_sfx(), 16) % 900000 + 100000
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO issue (repo_key, issue_number, status, stage, owner_agent, "
                  "user_id, assignee) VALUES (?,?,?,?,?,?,?)",
                  (repo, num, "pending_review", "qa", "qa", 1, "tester"))
        db.commit()
    try:
        # 复刻 run_pipeline 中 #1573 的处理顺序（钩子本体在 run_pipeline 里，见源码 2387-2405）
        assert _read_issue_status(repo, num) == "pending_review"
        assert _resume_issue_waiting(repo, num) is True
        set_issue_stage(repo, f"#{num}", "sre", "sre")
        assert _read_issue_status(repo, num) == "processing"
        with db_lock:
            stage = db.cursor().execute(
                "SELECT stage FROM issue WHERE repo_key=? AND issue_number=?",
                (repo, num)).fetchone()[0]
        assert stage == "sre"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?",
                                (repo, num))
            db.commit()


# ============================== #1566 ==============================
def test_1566_scene_field_passthrough_runs_real_source_line():
    """用 node 执行 dashboard.js 中的真实 map 表达式，断言 description 不被丢弃。"""
    root = os.path.join(os.path.dirname(__file__), "..")
    js_path = os.path.join(root, "src/api/static/js/pages/dashboard.js")
    src = open(js_path, encoding="utf-8").read()
    m = re.search(r"scenesData\.scenes\.map\(s => Object\.assign\(\{\}, s, \{scope: 'repo'\}\)\)", src)
    assert m, "未找到 #1566 的整行透传表达式"
    script = (
        "const scenesData={scenes:[{id:1,name:'a',display_name:'A',"
        "description:'场景描述',pr_subscription_enabled:true,icon:'x'}]};"
        f"const out=({m.group(0)});console.log(JSON.stringify(out[0]));"
    )
    node = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert node.returncode == 0, node.stderr
    scene = json.loads(node.stdout.strip())
    assert scene["description"] == "场景描述"
    assert scene["icon"] == "x"
    assert scene["scope"] == "repo"
    assert scene["pr_subscription_enabled"] is True

def test_1599_same_stage_rewrite_keeps_pending_review():
    """修复：同 stage 幂等重写不得抹掉审核态；阶段真变化时才同步 status。"""
    from src.api.session_store import set_issue_stage
    repo, num = f"gitcode:t{_sfx()}/r", int(_sfx(), 16) % 900000 + 100000
    with db_lock:
        db.cursor().execute(
            "INSERT INTO issue (repo_key, issue_number, status, stage, owner_agent, user_id, assignee) "
            "VALUES (?,?,?,?,?,?,?)", (repo, num, "pending_review", "sre", "sre", 1, "tester"))
        db.commit()
    try:
        # 重派/重试场景：同一 stage 重复写
        set_issue_stage(repo, f"#{num}", "sre", "sre")
        with db_lock:
            row = db.cursor().execute("SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
                                      (repo, num)).fetchone()
        assert row == ("pending_review", "sre"), row
        # 阶段真推进：审核点应当收敛（不留幽灵点）
        set_issue_stage(repo, f"#{num}", "done", "done")
        with db_lock:
            row2 = db.cursor().execute("SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
                                       (repo, num)).fetchone()
        assert row2 == ("completed", "done"), row2
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (repo, num))
            db.commit()


def test_1599_awaiting_input_survives_stage_advance():
    """修复：等用户澄清（awaiting_input）不被后台阶段推进吞掉。"""
    from src.api.session_store import set_issue_stage
    repo, num = f"gitcode:t{_sfx()}/r", int(_sfx(), 16) % 900000 + 100000
    with db_lock:
        db.cursor().execute(
            "INSERT INTO issue (repo_key, issue_number, status, stage, owner_agent, user_id, assignee) "
            "VALUES (?,?,?,?,?,?,?)", (repo, num, "awaiting_input", "qa", "qa", 1, "tester"))
        db.commit()
    try:
        set_issue_stage(repo, f"#{num}", "sre", "sre")
        with db_lock:
            row = db.cursor().execute("SELECT status, stage FROM issue WHERE repo_key=? AND issue_number=?",
                                      (repo, num)).fetchone()
        assert row == ("awaiting_input", "sre"), row
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (repo, num))
            db.commit()
