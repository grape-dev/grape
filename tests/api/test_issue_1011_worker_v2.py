"""Issue #1011 原子契约测试：worker 侧 v2-first / v1-fallback（404 语义二分）。

覆盖（Issue 正文 In Scope 4 / UC-I / AC「v2 404 语义二分守卫」）：
- 404 二分判定：空白 404/405（`{"detail":"Not Found"}`）= 旧服务端；
  body 错误码 UNKNOWN_ENGINE_UID = uid 失效
- I1 v2-first：uid 已知 → claim 先调 v2；空白 404 → 记忆性切换 v1（后续不再撞 v2）
- UNKNOWN_ENGINE_UID → 回退 v1 id（uid 漂移窗口不误判为旧服务端）
- I2 register 响应 engine_uid 持久化：落盘 + 重启（新实例）恢复
- heartbeat-v2 first（engine_uids 携带）/ 上报 v2 first（complete/fail engine_uid）
- 旧服务端 register 响应无 engine_uid 字段 → 静默忽略不崩（A8 兼容）

测试 seam：RuntimeClient 单实例 + 打桩 `_request_raw` / `_request`（无网络）。
"""
import asyncio
import json
import os

import pytest

from src.domains.runtime.engine.client import RuntimeClient


UID_A = "11111111-1111-5111-8111-111111111111"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPE_WORKER_UID_FILE", str(tmp_path / "uids.json"))
    c = RuntimeClient("http://server", "rta_tok")
    c._uid_by_rid = {1: UID_A}
    return c


def _stub(client, raw_results, v1_results):
    """raw: list[(status, data)] 供 _request_raw 依次返回；v1: list[data] 供 _request。"""
    raw_iter = iter(raw_results)
    v1_iter = iter(v1_results)

    async def fake_raw(method, path, body=None, retries=1):
        item = next(raw_iter, None)
        if item is None:
            return 0, None, ""
        if len(item) == 2:
            return item[0], item[1], json.dumps(item[1] or {})
        return item

    async def fake_req(method, path, body=None, retries=4):
        data = next(v1_iter, None)
        return data

    client._request_raw = fake_raw
    client._request = fake_req
    return raw_iter


# ---------------------------------------------------------------------------
# 404 语义二分判定（静态）
# ---------------------------------------------------------------------------

class Test404Semantics:
    def test_blank_not_found_is_old_server(self, client):
        assert client._is_blank_not_found(404, {"detail": "Not Found"}, '{"detail":"Not Found"}')
        assert client._is_blank_not_found(405, None, "")

    def test_uid_stale_is_not_old_server(self, client):
        data = {"detail": {"code": "UNKNOWN_ENGINE_UID", "message": "..."}}
        assert not client._is_blank_not_found(404, data, json.dumps(data))
        assert client._is_uid_stale(data, json.dumps(data))
        assert client._is_uid_stale(None, '{"code":"UNKNOWN_ENGINE_UID"}')

    def test_business_code_404_not_blank(self, client):
        assert not client._is_blank_not_found(404, {"code": "UNKNOWN_ENGINE_UID"}, "")
        assert not client._is_blank_not_found(200, {}, "")


# ---------------------------------------------------------------------------
# I1 · v2-first / fallback / 记忆
# ---------------------------------------------------------------------------

class TestClaimV2First:
    def test_v2_hit(self, client):
        _stub(client, [(200, {"task": {"id": "t1"}})], [])
        resp = _run(client.claim(1))
        assert resp["task"]["id"] == "t1"

    def test_blank_404_switch_and_remember(self, client):
        _stub(client, [(404, {"detail": "Not Found"}, '{"detail":"Not Found"}')],
              [{"task": None, "scan_request": False}])
        resp = _run(client.claim(1))
        assert resp is not None, "空白 404 应回退 v1"
        assert client._v2_enabled is False, "应记忆性关闭 v2"
        # 第二轮：不再撞 v2（raw 无可用返回 → 直接 v1）
        _stub(client, [], [{"task": None}])
        resp2 = _run(client.claim(1))
        assert resp2 == {"task": None}

    def test_uid_stale_fallback_v1(self, client):
        stale = (404, {"detail": {"code": "UNKNOWN_ENGINE_UID", "message": "x"}}, "")
        _stub(client, [stale], [{"task": {"id": "t1"}}])
        resp = _run(client.claim(1))
        assert resp["task"]["id"] == "t1"
        assert client._v2_enabled is True, "uid 失效不得误判为旧服务端（保持 v2 许可）"

    def test_claim_by_tag_v2_first(self, client):
        raw_iter = _stub(client, [(200, {"task": {"id": "t2"}})], [{"task": None}])
        resp = _run(client.claim_by_tag(1, "coding"))
        assert resp["task"]["id"] == "t2"

    def test_no_uid_goes_v1_directly(self, client):
        _stub(client, [], [{"task": {"id": "t3"}}])
        resp = _run(client.claim(99))
        assert resp["task"]["id"] == "t3"


class TestHeartbeatV2:
    def test_heartbeat_v2_first(self, client):
        seen = {}

        async def fake_raw(method, path, body=None, retries=1):
            seen["path"] = path
            seen["body"] = body
            return 200, {"status": "ok", "cancelled_tasks": []}, ""

        client._request_raw = fake_raw
        resp = _run(client.heartbeat([1]))
        assert seen["path"] == "/api/runtime/heartbeat-v2"
        assert seen["body"]["engine_uids"] == [UID_A]
        assert seen["body"]["runtime_ids"] == [1]

    def test_heartbeat_blank_404_fallback_v1(self, client):
        async def fake_raw(method, path, body=None, retries=1):
            return 404, {"detail": "Not Found"}, '{"detail":"Not Found"}'

        async def fake_req(method, path, body=None, retries=2):
            assert path == "/api/runtime/heartbeat"
            assert "engine_uids" not in (body or {})
            return {"status": "ok", "cancelled_tasks": []}

        client._request_raw = fake_raw
        client._request = fake_req
        resp = _run(client.heartbeat([1]))
        assert resp["status"] == "ok"
        assert client._v2_enabled is False

    def test_heartbeat_no_uid_v1(self, client):
        async def fake_raw(method, path, body=None, retries=1):
            raise AssertionError("v2 should not be called without uid")

        async def fake_req(method, path, body=None, retries=2):
            assert path == "/api/runtime/heartbeat"
            return {"status": "ok"}

        client._request_raw = fake_raw
        client._request = fake_req
        _run(client.heartbeat([77]))


class TestReportV2:
    def test_complete_v2_first_and_fallback(self, client):
        stale = (404, {"detail": {"code": "UNKNOWN_ENGINE_UID"}}, "")
        _stub(client, [stale], [{"status": "completed"}])
        resp = _run(client.complete("t1", "done", 0, engine_uid=UID_A))
        assert resp == {"status": "completed"}
        # 空白 404 → 关 v2 + v1 兜底
        client2 = RuntimeClient("http://server", "rta_tok")
        client2._v2_enabled = True
        _stub(client2, [(404, {"detail": "Not Found"}, '{"detail":"Not Found"}')], [{"status": "completed"}])
        resp2 = _run(client2.complete("t1", "done", 0, engine_uid=UID_A))
        assert resp2 == {"status": "completed"}
        assert client2._v2_enabled is False

    def test_fail_v2_first(self, client):
        _stub(client, [(200, {"status": "failed"})], [])
        resp = _run(client.fail("t1", "boom", engine_uid=UID_A))
        assert resp == {"status": "failed"}

    def test_progress_v2_first(self, client):
        _stub(client, [(200, {"status": "ok"})], [])
        _run(client.report_progress("t1", engine_uid=UID_A))   # 不抛即过

    def test_progress_blank_404_fallback_v1(self, client):
        async def fake_req(method, path, body=None, retries=1):
            assert path.endswith("/progress") and "-v2" not in path
            return {"status": "ok"}

        async def fake_raw(method, path, body=None, retries=1):
            return 404, {"detail": "Not Found"}, '{"detail":"Not Found"}'

        client._request_raw = fake_raw
        client._request = fake_req
        _run(client.report_progress("t1", engine_uid=UID_A))


class TestCapabilitiesProbe:
    def test_capabilities_v2_detected(self, client):
        async def fake_raw(method, path, body=None, retries=1):
            return 200, {"runtime_id": 1, "engine_uid": UID_A,
                         "capabilities": {"bound_claim": True, "task_lookup": True,
                                          "uid_claim": True}}, ""
        client._request_raw = fake_raw
        caps = _run(client.claim_v2_capabilities(UID_A))
        assert caps["capabilities"]["uid_claim"] is True

    def test_capabilities_old_server_404(self, client):
        async def fake_raw(method, path, body=None, retries=1):
            return 404, {"detail": "Not Found"}, '{"detail":"Not Found"}'
        client._request_raw = fake_raw
        assert _run(client.claim_v2_capabilities(UID_A)) is None


# ---------------------------------------------------------------------------
# I2 · register uid 持久化 / A8 旧服务端兼容
# ---------------------------------------------------------------------------

class TestUidPersistence:
    def test_register_persists_and_reloads(self, client, tmp_path, monkeypatch):
        async def fake_req(method, path, body=None, retries=4):
            return {"runtimes": [{"id": 1, "name": "a", "engine_uid": UID_A},
                                 {"id": 2, "name": "b", "engine_uid": ""}],
                    "probe_url": ""}
        client._request = fake_req
        ids, _ = _run(client.register([{"name": "a"}, {"name": "b"}]))
        assert ids == [1, 2]
        assert client.uid_for(1) == UID_A
        assert client.uid_for(2) == ""   # 空值不进映射
        store = os.environ["GRAPE_WORKER_UID_FILE"]
        assert os.path.exists(store), "uid 映射应落盘"
        # 重启：新实例从本地文件恢复（claim 不依赖服务端 id 记忆）
        c2 = RuntimeClient("http://server", "rta_tok")
        assert c2.uid_for(1) == UID_A

    def test_old_server_register_no_uid_field(self, client):
        client._uid_by_rid = {}   # 清空 fixture 种子（模拟旧服务端无 uid 场景）
        async def fake_req(method, path, body=None, retries=4):
            return {"runtimes": [{"id": 1, "name": "a"}], "probe_url": ""}
        client._request = fake_req
        ids, _ = _run(client.register([{"name": "a"}]))
        assert ids == [1]
        assert client.uid_for(1) == ""
