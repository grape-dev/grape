"""Issue #2792 切片 2：A2A 入口把 skipped_nodes 落库 + 回传 + 提醒用户（seam = POST /api/a2a）。

Spec（评论 id 201190348）实现路径 2/3：
- `skipped_nodes` 写入 A2A 事件 payload（缺省不写空数组）；
- 响应体回传，便于调用方与前端消费；
- `skipped_nodes` 非空时经既有 VCS 评论能力在 Issue 上提醒，失败不阻断交办。

桩点：提醒评论的投递函数 `_post_issue_comment_ledgered` 被替换为记录器
（不发真评论、不出网），既验证「有没有提醒」也验证「提醒失败不阻断交办」。
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitcode:owner/repo"
ISSUE_NUM = "7"


@pytest.fixture()
def client(monkeypatch):
    """干净托管仓库 + 场景绑定 Issue，端点走 queue_remote（不真起 agent）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' "
                  "OR id LIKE 'sess-2792-%'")
        c.execute("DELETE FROM agent_runtimes WHERE credential_id IN "
                  "(SELECT id FROM runtime_credentials WHERE name LIKE 'i2792-cred-%')")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'i2792-cred-%'")
        try:
            c.execute("DELETE FROM skill WHERE skill_name='implementation' AND scope='repo'")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            c.execute("DELETE FROM issue WHERE repo_key='gitcode:owner/repo'")
        except Exception:
            pass
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo')")
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("h" + uuid.uuid4().hex, "i2792-cred-" + uuid.uuid4().hex[:6], "i2792-dev"))
        _cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, "
            "scope, owner_id, installed, healthy) "
            "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
        _rid = c.lastrowid
        for _agent in ("issue", "coding", "qa", "sre"):
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable) "
                "VALUES (?, 'gitcode:owner/repo', ?, ?, 'idle', 1)",
                ("sess-2792-" + _agent, _agent, _rid))
        try:
            c.execute(
                "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                "skill_path, scope, owner_id, is_active) "
                "VALUES ('implementation','implementation','test skill','coding',"
                "'implementation','repo','gitcode:owner/repo',1)")
        except Exception:
            pass
        db.commit()
    _seed_issue("vod")
    os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
    from fastapi.testclient import TestClient
    from src.api.main import app
    _c = TestClient(app)
    yield _c
    os.environ.pop("DEVOPS_EXECUTION_MODE", None)
    with db_lock:
        _cc = db.cursor()
        _cc.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
        _cc.execute("DELETE FROM issue WHERE repo_key='gitcode:owner/repo'")
        _cc.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%'")
        _cc.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' "
                    "OR id LIKE 'sess-2792-%'")
        db.commit()


def _seed_issue(stage: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:owner/repo'")
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage, "
            "owner_agent, scene_id, assignee) VALUES (0, 'gitcode:owner/repo', ?, "
            "'#2792 跳步披露', 'active', ?, 'vod', 'default', 'tester')",
            (int(ISSUE_NUM), stage))
        db.commit()


def _set_stage(stage: str):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE issue SET stage=? WHERE repo_key='gitcode:owner/repo' AND issue_number=?",
                  (stage, int(ISSUE_NUM)))
        db.commit()


def _body(agent: str, context: str = "测试 A2A 跳步披露") -> dict:
    return {
        "agent": agent,
        "payload": {
            "repository": "owner/repo",
            "account": "owner/repo",
            "issue_number": ISSUE_NUM,
            "pr_number": "",
            "context": context,
        },
    }


def _event_payload(seq: int) -> dict:
    with db_lock:
        row = db.cursor().execute(
            "SELECT payload FROM events WHERE seq=?", (seq,)).fetchone()
    assert row, f"事件 seq={seq} 未落库"
    try:
        return json.loads(row[0] or "{}")
    except Exception:
        return {}


@pytest.fixture()
def posted(monkeypatch):
    """截获提醒评论投递（不出网），返回收到的 (issue_num, body) 列表。

    投递身份同样是桩：提醒只在能取到 bot 身份时才真的发评论，测试里补一个假 bot token，
    否则「无 bot 身份 → 不发」的降级分支会先短路，提醒就发不出来。
    """
    calls: list = []

    def _fake(repository, issue_num, comment, bot_token, repo_key="", origin_task=""):
        calls.append((str(issue_num), comment, bot_token))

    monkeypatch.setattr(
        "src.domains.runtime.pipeline.event_pipeline._post_issue_comment_ledgered", _fake)
    monkeypatch.setattr(
        "src.kernel.token_service.get_bot_identity_token", lambda repo_key: "bot-tok-test")
    return calls


def test_forward_skip_is_persisted_echoed_and_notified(client, posted):
    """vod 直交 qa（跳过 issue/coding）：payload 落库、响应回传、Issue 有提醒。"""
    r = client.post("/api/a2a", json=_body("qa"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] > 0, data
    assert data["skipped_nodes"] == ["issue", "coding"], data
    assert _event_payload(data["seq"])["skipped_nodes"] == ["issue", "coding"]
    assert posted and posted[0][0] == ISSUE_NUM
    assert "issue" in posted[0][1] and "coding" in posted[0][1]


def test_no_bot_identity_skips_notice_without_touching_vcs(client, monkeypatch):
    """无 bot 身份时不发提醒、不出网：降级只告警，交办照常。

    没有 bot token 时仍去调 VCS 评论接口，等于让一次异常交办同步触发一次无凭据
    外呼（拖慢交办、在测试里制造真实网络依赖）；这里锁住「先判身份，无身份直接降级」。
    """
    called = []
    monkeypatch.setattr(
        "src.domains.runtime.pipeline.event_pipeline._post_issue_comment_ledgered",
        lambda *a, **k: called.append(a))
    monkeypatch.setattr(
        "src.kernel.token_service.get_bot_identity_token", lambda repo_key: "")

    r = client.post("/api/a2a", json=_body("qa"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] > 0, data
    assert data["skipped_nodes"] == ["issue", "coding"], data
    assert called == [], "无 bot 身份不得触达 VCS 评论接口"
    assert _event_payload(data["seq"])["skipped_nodes"] == ["issue", "coding"]


def test_adjacent_hop_writes_no_skipped_nodes_key(client, posted):
    """vod→issue 正常顺推：不落键、不回传、不发提醒（零语义变更）。"""
    r = client.post("/api/a2a", json=_body("issue"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] > 0, data
    assert "skipped_nodes" not in data
    assert "skipped_nodes" not in _event_payload(data["seq"])
    assert posted == []


def test_rollback_hop_is_not_annotated_and_not_rejected(client, posted):
    """qa→coding 打回：合法回滚，事件照建、不标注、不拒绝（Spec 验收③）。"""
    _set_stage("qa")
    r = client.post("/api/a2a", json=_body("coding"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] > 0, data
    assert data.get("skipped") != "duplicate_a2a"
    assert "skipped_nodes" not in data
    assert "skipped_nodes" not in _event_payload(data["seq"])
    assert posted == []


def test_notice_failure_does_not_block_handoff(client, monkeypatch):
    """提醒投递失败只告警，不阻断交办（Spec：失败不阻断）。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("vcs comment unavailable")

    monkeypatch.setattr(
        "src.domains.runtime.pipeline.event_pipeline._post_issue_comment_ledgered", _boom)
    r = client.post("/api/a2a", json=_body("qa"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] > 0, data
    assert data["skipped_nodes"] == ["issue", "coding"], data


def test_forward_skip_switch_defaults_off(client, posted):
    """规则开关默认关闭：本期零语义变更（前向越站照旧交办，只标注）。"""
    from src.domains.runtime.routes import webhook as _wh

    assert _wh._REJECT_FORWARD_SKIP is False
    r = client.post("/api/a2a", json=_body("qa"))
    assert r.status_code == 200, r.text


def test_forward_skip_switch_rejects_when_enabled(client, posted, monkeypatch):
    """Spec 案 1 开关位：打开后前向越站被 422 拒绝（不建事件、不发提醒）。

    本期不启用（平台未决策），但开关位必须真的接线——只留一个没人读的常量等于没预留。
    """
    monkeypatch.setattr(
        "src.domains.runtime.routes.webhook._REJECT_FORWARD_SKIP", True)
    r = client.post("/api/a2a", json=_body("qa"))
    assert r.status_code == 422, r.text
    data = r.json()
    assert data["skipped"] == "forward_skip_rejected"
    assert data["skipped_nodes"] == ["issue", "coding"]
    assert data["handoff_result"] == "rejected"
    with db_lock:
        n = db.cursor().execute(
            "SELECT COUNT(*) FROM events WHERE repo_key=?", (REPO_KEY,)).fetchone()[0]
    assert n == 0, "被拒绝的越站不得留下事件"
    assert posted == [], "被拒绝的越站不得发提醒"


def test_rollback_hop_is_not_rejected_even_with_switch_on(client, posted, monkeypatch):
    """案 1 只收窄「前向越站」：开关打开后 qa→coding 打回仍必须放行。"""
    monkeypatch.setattr(
        "src.domains.runtime.routes.webhook._REJECT_FORWARD_SKIP", True)
    _set_stage("qa")
    r = client.post("/api/a2a", json=_body("coding"))
    assert r.status_code == 200, r.text
    assert r.json()["seq"] > 0