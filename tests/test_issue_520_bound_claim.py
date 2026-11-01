"""Issue #520：bound claim（按 task/event 原子认领）+ 能力协商 + 许可对号 lookup 测试。

覆盖 Spec v2 验收用例 1-12：
1  bound claim 精确领取目标 B，旧任务 A 状态不变
2  task/event 任一不匹配 → 无任务，不改其他任务
3  expected 只传一项 → 明确 4xx 拒绝
4  未传 expected（旧路径）→ FIFO 保持
5  能力协商端点返回 200 + bound_claim 能力位
6  空队列 bound claim 返回 null
7  lookup 对号成功 + bound claim 闭环
8  lookup 未命中返回 null
9  他人 runtime_id → 403（凭据归属校验）
10 event_seq=0 / 缺参 → 4xx 拒绝
11 claim-capabilities 含 task_lookup 能力位
12 FIFO 回归（claim / claim-by-tag 主路径零改动）
"""
import hashlib
import secrets

import pytest

from fastapi.testclient import TestClient

from src.session_manager import db, db_lock
from src.runtime import service


def _make_token(name: str) -> str:
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?, 'global')",
                  (hashlib.sha256(token.encode()).hexdigest(), name))
        db.commit()
    return token


@pytest.fixture(autouse=True)
def _clean():
    _sql = (
        "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:b520/%'",
        "DELETE FROM engine_capabilities WHERE runtime_id IN "
        "(SELECT id FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b520-%'))",
        "DELETE FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b520-%')",
        "DELETE FROM runtime_credentials WHERE name LIKE 'b520-%'",
        "DELETE FROM events WHERE repo_key LIKE 'gitcode:b520/%'",
    )
    with db_lock:
        for s in _sql:
            try:
                db.cursor().execute(s)
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        for s in _sql:
            try:
                db.cursor().execute(s)
            except Exception:
                pass
        db.commit()


@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


def _register(client, token, device="b520-dev"):
    r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                    json={"device_name": device, "runtimes": [{"name": "opencode"}]})
    assert r.status_code == 200, r.text
    return r.json()["runtimes"][0]["id"]


def _enqueue(tid, runtime_id, event_seq, created_off=0, **kw):
    from datetime import datetime, timedelta, timezone
    tid = service.enqueue_task(
        agent_session_id=kw.pop("agent_session_id", f"sess-{tid}"),
        runtime_id=runtime_id,
        prompt="p", event_seq=event_seq,
        issue_id=kw.pop("issue_id", "#520"),
        repo_key="gitcode:b520/repo", agent_type="issue", skill="issue",
        issue_session_id=kw.pop("issue_session_id", ""),
        **kw)
    if created_off:
        _old = (datetime.now(timezone.utc) - timedelta(seconds=created_off)).isoformat()
        with db_lock:
            db.cursor().execute("UPDATE agent_tasks SET created_at=? WHERE id=?", (_old, tid))
            db.commit()
    return tid


class TestBoundClaim:
    def test_case1_bound_claim_exact_target(self, client):
        token = _make_token("b520-cred-1")
        rid = _register(client, token)
        old_a = _enqueue("a", rid, 5201, created_off=120)
        target_b = _enqueue("b", rid, 5202)
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": target_b, "expected_event_seq": 5202})
        assert r.status_code == 200, r.text
        task = r.json()["task"]
        assert task and task["id"] == target_b, "bound claim 必须精确领取目标 B"
        assert task["status"] == "running"
        assert service.get_task(old_a)["status"] == "queued", "旧任务 A 状态不变"

    def test_case2_bound_claim_mismatch_no_side_effect(self, client):
        token = _make_token("b520-cred-2")
        rid = _register(client, token)
        a = _enqueue("a", rid, 5211, created_off=120)
        b = _enqueue("b", rid, 5212)
        # event_seq 不匹配
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": b, "expected_event_seq": 999999})
        assert r.status_code == 200 and r.json()["task"] is None
        # task_id 不匹配
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": "t_nonexistent", "expected_event_seq": 5212})
        assert r.status_code == 200 and r.json()["task"] is None
        for tid in (a, b):
            t = service.get_task(tid)
            assert t["status"] == "queued", f"不匹配时不得修改其他任务: {tid}"

    def test_case2b_bound_claim_serial_slot_blocked(self, client):
        token = _make_token("b520-cred-2b")
        rid = _register(client, token)
        first = _enqueue("s1", rid, 5221, issue_session_id="is-b520")
        running = _enqueue("s2", rid, 5222, issue_session_id="is-b520")
        # 先领 first（占住串行槽）
        claimed = service.claim(rid)
        assert claimed and claimed["id"] == first
        # 同串行槽的 running 任务不可被 bound claim 领取
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": running, "expected_event_seq": 5222})
        assert r.status_code == 200 and r.json()["task"] is None
        assert service.get_task(running)["status"] == "queued"

    def test_case3_expected_partial_rejected(self, client):
        token = _make_token("b520-cred-3")
        rid = _register(client, token)
        # bound 端点只传一项 → 模型成对校验 422
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": "t_x"})
        assert 400 <= r.status_code < 500, r.text
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_event_seq": 5202})
        assert 400 <= r.status_code < 500, r.text
        # bound 端点完全不传 → 400（缺参明确拒绝）
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 400, r.text
        # 旧 claim 路径只传一项同样拒绝（不静默 FIFO）
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": "t_x"})
        assert 400 <= r.status_code < 500, r.text

    def test_case4_old_path_fifo_unchanged(self, client):
        token = _make_token("b520-cred-4")
        rid = _register(client, token)
        a = _enqueue("a", rid, 5231, created_off=120)
        b = _enqueue("b", rid, 5232)
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim",
                        headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 200, r.text
        task = r.json()["task"]
        assert task and task["id"] == a, "未传 expected 保持 FIFO（先入队先领）"
        assert service.get_task(b)["status"] == "queued"

    def test_case5_capabilities_endpoint(self, client):
        token = _make_token("b520-cred-5")
        rid = _register(client, token)
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/claim-capabilities",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        caps = r.json()["capabilities"]
        assert caps["bound_claim"] is True, "能力协商必须暴露 bound_claim 位"

    def test_case6_bound_claim_empty_queue(self, client):
        token = _make_token("b520-cred-6")
        rid = _register(client, token)
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": "t_none", "expected_event_seq": 1})
        assert r.status_code == 200 and r.json()["task"] is None, "空队列 → 无任务（许可保留）"

    def test_case7_lookup_then_bound_closed_loop(self, client):
        token = _make_token("b520-cred-7")
        rid = _register(client, token)
        target = _enqueue("t7", rid, 5247)
        # ① lookup 对号
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/lookup?expected_event_seq=5247",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        found = r.json()["task"]
        assert found and found["task_id"] == target
        assert found["event_seq"] == 5247 and found["issue_id"] == "#520"
        assert set(found) == {"task_id", "issue_id", "event_seq", "agent_type", "created_at"}
        # ② bound claim 原子领取
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": found["task_id"],
                              "expected_event_seq": found["event_seq"]})
        assert r.status_code == 200 and r.json()["task"]["id"] == target
        assert service.get_task(target)["status"] == "running"
        # ③ 已被领走 → lookup 未命中（许可已消费）
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/lookup?expected_event_seq=5247",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json()["task"] is None

    def test_case8_lookup_miss(self, client):
        token = _make_token("b520-cred-8")
        rid = _register(client, token)
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/lookup?expected_event_seq=888888",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json() == {"task": None}, "未入队 → null（许可保留）"

    def test_case9_other_runtime_403(self, client):
        token_a = _make_token("b520-cred-9a")
        token_b = _make_token("b520-cred-9b")
        rid_a = _register(client, token_a, device="b520-dev-a")
        rid_b = _register(client, token_b, device="b520-dev-b")
        target = _enqueue("t9", rid_a, 5259)
        # 凭据 A 访问他人 runtime B → 403
        r = client.get(f"/api/runtime/runtimes/{rid_b}/tasks/lookup?expected_event_seq=5259",
                       headers={"Authorization": f"Bearer {token_a}"})
        assert r.status_code == 403, r.text
        r = client.post(f"/api/runtime/runtimes/{rid_b}/tasks/claim-bound",
                        headers={"Authorization": f"Bearer {token_a}"},
                        json={"expected_task_id": target, "expected_event_seq": 5259})
        assert r.status_code == 403, r.text
        r = client.get(f"/api/runtime/runtimes/{rid_b}/tasks/claim-capabilities",
                       headers={"Authorization": f"Bearer {token_a}"})
        assert r.status_code == 403, r.text
        # 无凭据 → 401
        r = client.get(f"/api/runtime/runtimes/{rid_a}/tasks/lookup?expected_event_seq=5259")
        assert r.status_code == 401
        assert service.get_task(target)["status"] == "queued", "归属校验失败不得触发领取"

    def test_case10_lookup_seq_zero_or_missing(self, client):
        token = _make_token("b520-cred-10")
        rid = _register(client, token)
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/lookup?expected_event_seq=0",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, r.text
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/lookup",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, r.text

    def test_case11_task_lookup_capability_bit(self, client):
        token = _make_token("b520-cred-11")
        rid = _register(client, token)
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/claim-capabilities",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        caps = r.json()["capabilities"]
        assert caps["task_lookup"] is True, "能力协商必须暴露 task_lookup 位"

    def test_case12_lookup_pool_task_shared_queue(self, client):
        """runtime_id=0 共享池任务的 lookup 骨架回归（claim 候选骨架含共享池）。"""
        token = _make_token("b520-cred-12")
        rid = _register(client, token)
        target = _enqueue("t12", 0, 5212, pool_agent_type="issue", pool_tag="b520tag")
        r = client.get(f"/api/runtime/runtimes/{rid}/tasks/lookup?expected_event_seq=5212",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        found = r.json()["task"]
        assert found and found["task_id"] == target, "共享池任务可被 lookup 对号"


class TestBoundClaimByTag:
    def _setup_tag_engine(self, client, tag="b520tag"):
        token = _make_token("b520-cred-tag")
        rid = _register(client, token, device="b520-dev-tag")
        with db_lock:
            db.cursor().execute(
                "INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
                (rid, tag))
            db.commit()
        return token, rid

    def test_bound_claim_by_tag_exact(self, client):
        token, rid = self._setup_tag_engine(client)
        old_a = _enqueue("ta", 0, 5261, pool_agent_type="issue", pool_tag="b520tag", created_off=120)
        target_b = _enqueue("tb", 0, 5262, pool_agent_type="issue", pool_tag="b520tag")
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-by-tag-bound/b520tag",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": target_b, "expected_event_seq": 5262})
        assert r.status_code == 200, r.text
        task = r.json()["task"]
        assert task and task["id"] == target_b, "tag bound claim 精确领取目标"
        assert service.get_task(old_a)["status"] == "queued", "旧任务不变"

    def test_bound_claim_by_tag_mismatch(self, client):
        token, rid = self._setup_tag_engine(client)
        a = _enqueue("tc", 0, 5271, pool_agent_type="issue", pool_tag="b520tag")
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-by-tag-bound/b520tag",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"expected_task_id": a, "expected_event_seq": 999999})
        assert r.status_code == 200 and r.json()["task"] is None
        assert service.get_task(a)["status"] == "queued", "不匹配不得领取"
        # 缺 expected → 400
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-by-tag-bound/b520tag",
                        headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 400, r.text

    def test_case12_claim_by_tag_fifo_regression(self, client):
        """旧 claim-by-tag 路径 FIFO 回归（未传 expected 保持原行为）。"""
        token, rid = self._setup_tag_engine(client)
        a = _enqueue("td", 0, 5281, pool_agent_type="issue", pool_tag="b520tag", created_off=120)
        _enqueue("te", 0, 5282, pool_agent_type="issue", pool_tag="b520tag")
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim-by-tag/b520tag",
                        headers={"Authorization": f"Bearer {token}"}, json={})
        assert r.status_code == 200, r.text
        task = r.json()["task"]
        assert task and task["id"] == a, "claim-by-tag 未传 expected 保持 FIFO"
