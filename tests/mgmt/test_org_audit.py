"""Issue #2260 组织审计查询接口 GET /api/orgs/{org_id}/audit 验收测试。

覆盖：
  鉴权：owner/admin 200；member / 非成员 403 PERMISSION_DENIED；org 不存在 404 ORG_NOT_FOUND；tk_ 403
  隔离：跨组织不可见；org_repo_transfer 按 source_org_id/target_org_id 归属
  过滤：event_type 精确 / 前缀（org_repo_/org_context_/org_knowledge_）/ 别名 org_dispatch_；
        别名 org_provision_（已接供给审计）与未知前缀 → 无匹配时 200 空列表
  分页：id 倒序、limit 默认 50/最大 200/下限 1、offset 夹取、total 与分页一致
  时间：since 生效
  脱敏：detail 不含 token/secret/正文键，原始 secret 不出现在响应中

运行：python -m pytest tests/mgmt/test_org_audit.py -v
"""
import json
import os
import sys

import unittest

# 强制使用独立测试库，防止 TestClient 误连生产库（与 test_issue_395_bugfixes.py 同款）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

_UID = 0
_USERNAME = "test_2260"
_HDR = {}

_CREATED_ORG_IDS = []
_CREATED_USER_IDS = []
_CREATED_AUDIT_IDS = []


def _ensure_user(username: str) -> int:
    from src.domains.mgmt.auth.password import hash_password

    # D105 第二批：grape_accounts.id 为 TEXT 主键（uuid7 应用层生成，lastrowid 退役）
    from src.kernel.id_gen import uuid7_str

    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username=?", (username,))
        row = c.fetchone()
        if row:
            return row[0]
        uid = uuid7_str()
        c.execute(
            "INSERT INTO grape_accounts (id,username,email,password_hash,nickname,status,token_version) "
            "VALUES (?,?,?,?,?,1,0)",
            (uid, username, None, hash_password("test123"), username))
        db.commit()
    _CREATED_USER_IDS.append(uid)
    return uid


def setup_module():
    global _UID, _HDR
    from src.kernel.auth_sdk.jwt_utils import create_access_token

    _UID = _ensure_user(_USERNAME)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_accounts SET token_version=0 WHERE id=?", (_UID,))
        db.commit()
    _HDR = {"Authorization": "Bearer " + create_access_token(_UID, _USERNAME, 0)}


def teardown_module():
    with db_lock:
        c = db.cursor()
        for aid in _CREATED_AUDIT_IDS:
            c.execute("DELETE FROM audit_logs WHERE id=?", (aid,))
        for oid in _CREATED_ORG_IDS:
            c.execute("DELETE FROM org_member WHERE org_id=?", (oid,))
            c.execute("DELETE FROM organization WHERE id=?", (oid,))
        for uid in _CREATED_USER_IDS:
            c.execute("DELETE FROM org_member WHERE user_id=?", (uid,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_UID,))
        db.commit()


def _mk_org(name: str, owner_id: int, member_id: int = 0, member_role: str = "") -> int:
    """建非个人组织并加 owner；可选再加一名指定角色成员。"""
    # D105 第三批：organization.id 为 TEXT 主键（create_org/应用层 uuid7 生成，lastrowid 退役）
    from src.kernel.id_gen import uuid7_str

    with db_lock:
        c = db.cursor()
        org_id = uuid7_str()
        c.execute(
            "INSERT INTO organization (id,name,slug,owner_id,is_personal,description) "
            "VALUES (?,?,?,?,0,'issue2260 test org')",
            (org_id, name, name, owner_id))
        c.execute("INSERT INTO org_member (org_id,user_id,role) VALUES (?,?,'owner')",
                  (org_id, owner_id))
        if member_id and member_role:
            c.execute("INSERT OR REPLACE INTO org_member (org_id,user_id,role) VALUES (?,?,?)",
                      (org_id, member_id, member_role))
        db.commit()
    _CREATED_ORG_IDS.append(org_id)
    return org_id


def _insert_audit(event_type: str, detail: dict, created_at: str = "2026-06-01 00:00:00",
                  user_id: int = 0, operator_id: int = 0, ip: str = "") -> int:
    """直接写入一条审计行（不带哈希链，仅用于查询侧用例）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COALESCE(MAX(seq),0) FROM audit_logs")
        seq = (c.fetchone()[0] or 0) + 1
        c.execute(
            "INSERT INTO audit_logs (seq,event_type,user_id,operator_id,ip,detail,prev_hash,curr_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, event_type, user_id, operator_id, ip,
             json.dumps(detail, ensure_ascii=False), "", "", created_at))
        db.commit()
        aid = c.lastrowid
    _CREATED_AUDIT_IDS.append(aid)
    return aid


def _get(org_id, **params):
    return client.get(f"/api/orgs/{org_id}/audit", params=params, headers=_HDR)


# ============================ 鉴权 ============================

class TestAuth:
    def test_owner_200(self):
        org = _mk_org("issue2260_auth_owner", _UID)
        r = _get(org)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"

    def test_admin_200(self):
        owner = _ensure_user("test_2260_admin_owner")
        org = _mk_org("issue2260_auth_admin", owner, member_id=_UID, member_role="admin")
        r = _get(org)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"

    def test_member_403_permission_denied(self):
        owner = _ensure_user("test_2260_member_owner")
        org = _mk_org("issue2260_auth_member", owner, member_id=_UID, member_role="member")
        r = _get(org)
        assert r.status_code == 403, f"Got {r.status_code}: {r.text[:300]}"
        assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"

    def test_non_member_403_permission_denied(self):
        owner = _ensure_user("test_2260_other_owner")
        org = _mk_org("issue2260_auth_nonmember", owner)
        r = _get(org)
        assert r.status_code == 403, f"Got {r.status_code}: {r.text[:300]}"
        assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"

    def test_org_not_found_404(self):
        r = _get(99999999)
        assert r.status_code == 404, f"Got {r.status_code}: {r.text[:300]}"
        assert r.json()["detail"]["error_code"] == "ORG_NOT_FOUND"

    def test_session_token_403(self):
        org = _mk_org("issue2260_auth_sess", _UID)
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {
            "id": _UID, "username": _USERNAME, "auth_type": "session_token"}
        try:
            r = _get(org)
            assert r.status_code == 403, f"Got {r.status_code}: {r.text[:300]}"
        finally:
            app.dependency_overrides.pop(get_current_user_or_api_key, None)


# ============================ 契约字段 ============================

class TestContract:
    def test_response_fields(self):
        org = _mk_org("issue2260_contract", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "gitcode:o/r"},
                      user_id=_UID, operator_id=_UID, ip="1.2.3.4")
        r = _get(org)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"items", "total", "limit", "offset"}
        item = body["items"][0]
        assert set(item.keys()) >= {
            "id", "seq", "event_type", "operator_id", "user_id", "ip", "detail", "created_at"}
        assert item["event_type"] == "org_repo_bind"
        assert item["ip"] == "1.2.3.4"
        assert body["total"] == 1 and body["limit"] == 50 and body["offset"] == 0


# ============================ 事件过滤 ============================

class TestEventTypeFilter:
    def test_exact_match(self):
        org = _mk_org("issue2260_filter_exact", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a/1"})
        _insert_audit("org_repo_unbind", {"org_id": org, "repo_key": "a/2"})
        r = _get(org, event_type="org_repo_bind")
        assert r.status_code == 200
        body = r.json()
        assert [i["event_type"] for i in body["items"]] == ["org_repo_bind"]
        assert body["total"] == 1

    def test_prefix_org_repo(self):
        org = _mk_org("issue2260_filter_repo", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a/1"})
        _insert_audit("org_repo_unbind", {"org_id": org, "repo_key": "a/2"})
        _insert_audit("org_repo_transfer", {"repo_key": "a/3", "source_org_id": org, "target_org_id": org})
        _insert_audit("org_context_create", {"org_id": org, "context_id": 1})
        r = _get(org, event_type="org_repo_")
        types = sorted(i["event_type"] for i in r.json()["items"])
        assert types == ["org_repo_bind", "org_repo_transfer", "org_repo_unbind"]

    def test_prefix_org_context_and_knowledge(self):
        org = _mk_org("issue2260_filter_ctx", _UID)
        _insert_audit("org_context_create", {"org_id": org, "context_id": 1})
        _insert_audit("org_context_publish", {"org_id": org, "context_id": 1})
        _insert_audit("org_knowledge_create", {"org_id": org, "knowledge_id": 2})
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a/1"})
        ctx = _get(org, event_type="org_context_").json()
        assert sorted(i["event_type"] for i in ctx["items"]) == ["org_context_create", "org_context_publish"]
        knw = _get(org, event_type="org_knowledge_").json()
        assert [i["event_type"] for i in knw["items"]] == ["org_knowledge_create"]

    def test_alias_org_dispatch_maps_to_real_events(self):
        org = _mk_org("issue2260_filter_dispatch", _UID)
        _insert_audit("dispatch_created", {"org_id": org, "dispatch_id": 1})
        _insert_audit("target_created", {"org_id": org, "dispatch_id": 1, "repo_key": "a/1"})
        _insert_audit("dispatch_completed", {"org_id": org, "dispatch_id": 1})
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a/2"})
        r = _get(org, event_type="org_dispatch_")
        types = sorted(i["event_type"] for i in r.json()["items"])
        assert types == ["dispatch_completed", "dispatch_created", "target_created"]

    def test_alias_org_provision_no_rows(self):
        org = _mk_org("issue2260_filter_provision", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a/1"})
        r = _get(org, event_type="org_provision_")
        assert r.status_code == 200
        assert r.json()["items"] == [] and r.json()["total"] == 0

    def test_unknown_prefix_empty(self):
        org = _mk_org("issue2260_filter_unknown", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a/1"})
        r = _get(org, event_type="zzz_unknown_")
        assert r.status_code == 200
        assert r.json()["items"] == [] and r.json()["total"] == 0

    def test_non_org_event_exact_empty(self):
        """非组织事件（如 login_success）不进入白名单 → 空列表。"""
        org = _mk_org("issue2260_filter_nonorg", _UID)
        _insert_audit("login_success", {"org_id": org})
        r = _get(org, event_type="login_success")
        assert r.status_code == 200 and r.json()["items"] == []


# ============================ 组织隔离 ============================

class TestIsolation:
    def test_cross_org_invisible(self):
        org_a = _mk_org("issue2260_iso_a", _UID)
        org_b = _mk_org("issue2260_iso_b", _UID)
        _insert_audit("org_repo_bind", {"org_id": org_a, "repo_key": "a/1"})
        _insert_audit("org_repo_bind", {"org_id": org_b, "repo_key": "b/1"})
        a = _get(org_a).json()
        b = _get(org_b).json()
        assert a["total"] == 1 and b["total"] == 1
        assert a["items"][0]["detail"]["repo_key"] == "a/1"
        assert b["items"][0]["detail"]["repo_key"] == "b/1"

    def test_transfer_visible_for_source_and_target(self):
        org_a = _mk_org("issue2260_iso_tr_a", _UID)
        org_b = _mk_org("issue2260_iso_tr_b", _UID)
        org_c = _mk_org("issue2260_iso_tr_c", _UID)
        _insert_audit("org_repo_transfer", {
            "repo_key": "a/1", "source_org_id": org_a, "target_org_id": org_b})
        assert _get(org_a).json()["total"] == 1
        assert _get(org_b).json()["total"] == 1
        assert _get(org_c).json()["total"] == 0

    def test_string_org_id_matches(self):
        org = _mk_org("issue2260_iso_str_org", _UID)
        _insert_audit("org_repo_bind", {"org_id": str(org), "repo_key": "a/1"})
        body = _get(org).json()
        assert body["total"] == 1
        assert body["items"][0]["detail"]["repo_key"] == "a/1"


# ============================ 分页 / 排序 ============================

class TestPagination:
    def _seed(self, org, n):
        for i in range(n):
            _insert_audit("org_repo_bind", {"org_id": org, "repo_key": f"r/{i}"},
                          created_at="2026-06-01 00:00:00")

    def test_id_desc_and_total_consistent(self):
        org = _mk_org("issue2260_page_order", _UID)
        self._seed(org, 5)
        body = _get(org, limit=2, offset=0).json()
        assert body["total"] == 5
        assert body["limit"] == 2 and body["offset"] == 0
        ids = [i["id"] for i in body["items"]]
        assert len(ids) == 2 and ids == sorted(ids, reverse=True)

    def test_offset_slices_consistently(self):
        org = _mk_org("issue2260_page_offset", _UID)
        self._seed(org, 5)
        p0 = _get(org, limit=2, offset=0).json()
        p1 = _get(org, limit=2, offset=2).json()
        assert p0["total"] == p1["total"] == 5
        assert {i["id"] for i in p0["items"]}.isdisjoint({i["id"] for i in p1["items"]})

    def test_limit_clamped_to_max_200(self):
        org = _mk_org("issue2260_page_max", _UID)
        self._seed(org, 250)
        body = _get(org, limit=9999).json()
        assert body["limit"] == 200
        assert body["total"] == 250
        assert len(body["items"]) == 200

    def test_limit_lower_bound_clamped(self):
        org = _mk_org("issue2260_page_min", _UID)
        self._seed(org, 2)
        body = _get(org, limit=0).json()
        assert body["limit"] == 1
        assert len(body["items"]) == 1

    def test_negative_offset_clamped(self):
        org = _mk_org("issue2260_page_negoff", _UID)
        body = _get(org, offset=-5).json()
        assert body["offset"] == 0


# ============================ since ============================

class TestSince:
    def test_since_filters(self):
        org = _mk_org("issue2260_since", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "old"}, created_at="2020-01-01 00:00:00")
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "new"}, created_at="2030-01-01 00:00:00")
        assert _get(org).json()["total"] == 2
        body = _get(org, since="2025-01-01").json()
        assert body["total"] == 1
        assert body["items"][0]["detail"]["repo_key"] == "new"

    def test_since_with_datetime(self):
        org = _mk_org("issue2260_since_dt", _UID)
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "a"}, created_at="2030-05-01 10:00:00")
        _insert_audit("org_repo_bind", {"org_id": org, "repo_key": "b"}, created_at="2030-05-01 20:00:00")
        body = _get(org, since="2030-05-01 12:00:00").json()
        assert body["total"] == 1 and body["items"][0]["detail"]["repo_key"] == "b"

    def test_since_rejects_timezone_and_invalid_format(self):
        org = _mk_org("issue2260_since_invalid", _UID)
        for value in ("2030-05-01T12:00:00Z", "2030-05-01T12:00:00+08:00",
                      "not-a-date"):
            r = _get(org, since=value)
            assert r.status_code == 400, f"{value}: {r.status_code} {r.text[:300]}"
            assert r.json()["detail"]["error_code"] == "INVALID_SINCE"


# ============================ 脱敏 ============================

class TestMasking:
    def test_sensitive_and_content_keys_masked(self):
        org = _mk_org("issue2260_mask", _UID)
        _insert_audit("org_repo_bind", {
            "org_id": org,
            "repo_key": "a/1",
            "token": "SUPER_SECRET_TOKEN_VALUE",
            "secret": "SUPER_SECRET_VALUE",
            "password": "SUPER_SECRET_PASSWORD",
            "content": "正文不应出现",
            "body": "正文不应出现",
        })
        r = _get(org)
        text = r.text
        assert "SUPER_SECRET_TOKEN_VALUE" not in text
        assert "SUPER_SECRET_VALUE" not in text
        assert "SUPER_SECRET_PASSWORD" not in text
        assert "正文不应出现" not in text
        detail = r.json()["items"][0]["detail"]
        assert detail["token"] == "[REDACTED]"
        assert detail["secret"] == "[REDACTED]"
        assert "content" not in detail and "body" not in detail

    def test_nested_masking(self):
        org = _mk_org("issue2260_mask_nested", _UID)
        _insert_audit("dispatch_created", {
            "org_id": org,
            "dispatch_id": 7,
            "context": {"prompt": "不应出现", "meta": {"api_key": "NESTED_SECRET_KEY"}},
        })
        r = _get(org, event_type="org_dispatch_")
        assert "NESTED_SECRET_KEY" not in r.text
        assert "不应出现" not in r.text
        detail = r.json()["items"][0]["detail"]
        assert "prompt" not in detail["context"]
        assert detail["context"]["meta"]["api_key"] == "[REDACTED]"

    def test_mask_detail_pure(self):
        from src.domains.mgmt.auth.audit import mask_detail
        src = {"org_id": 1, "refresh_token": "x", "body": "y",
               "list": [{"cookie": "c", "text": "t"}]}
        out = mask_detail(src)
        assert out["org_id"] == 1
        assert out["refresh_token"] == "[REDACTED]"
        assert "body" not in out
        assert out["list"][0]["cookie"] == "[REDACTED]"
        assert "text" not in out["list"][0]

    def test_deep_nesting_fail_closed(self):
        """深度超限必须丢弃子树（fail-closed），不得原样返回。"""
        org = _mk_org("issue2260_mask_deep", _UID)
        _insert_audit("org_repo_bind", {
            "org_id": org,
            "l1": {"l2": {"l3": {"l4": "DEEP_LEVEL_SECRET"}}},
        })
        r = _get(org)
        assert "DEEP_LEVEL_SECRET" not in r.text
        assert r.json()["items"][0]["detail"]["l1"]["l2"]["l3"]["l4"] == "[REDACTED]"

    def test_deep_nesting_containers_dropped(self):
        """超限的 dict/list 子树整体丢弃，不泄露内部任意键值。"""
        from src.domains.mgmt.auth.audit import mask_detail
        deep = {"a": {"b": {"c": [{"d": {"raw_password_value": "LEAK"}}]}}}
        out = mask_detail(deep)
        assert "LEAK" not in json.dumps(out, ensure_ascii=False)
        assert out["a"]["b"]["c"] == ["[REDACTED]"]

    def test_suffix_and_camel_sensitive_keys_masked(self):
        """敏感键按后缀/关键字匹配：*_token / *_secret / camelCase 均需脱敏。"""
        org = _mk_org("issue2260_mask_suffix", _UID)
        _insert_audit("org_repo_bind", {
            "org_id": org,
            "user_access_token": "SUFFIX_TOKEN_VALUE",
            "db_secret": "SUFFIX_SECRET_VALUE",
            "clientSecret": "CAMEL_SECRET_VALUE",
            "X-Auth-Token": "HEADER_TOKEN_VALUE",
            "apiKey": "CAMEL_APIKEY_VALUE",
            "repo_key": "a/1",
        })
        r = _get(org)
        for secret in ("SUFFIX_TOKEN_VALUE", "SUFFIX_SECRET_VALUE",
                       "CAMEL_SECRET_VALUE", "HEADER_TOKEN_VALUE",
                       "CAMEL_APIKEY_VALUE"):
            assert secret not in r.text
        detail = r.json()["items"][0]["detail"]
        assert detail["user_access_token"] == "[REDACTED]"
        assert detail["db_secret"] == "[REDACTED]"
        # 非敏感键不得误伤（repo_key 是业务标识，需保留）
        assert detail["repo_key"] == "a/1"

    def test_named_key_variants_masked_without_business_false_positives(self):
        """下划线/连字符 key 变体必须脱敏，业务 key 标识不得误伤。"""
        org = _mk_org("issue2260_mask_key_variants", _UID)
        _insert_audit("org_repo_bind", {
            "org_id": org,
            "private_key": "LEAK_PRIVATE_KEY",
            "access_key": "LEAK_ACCESS_KEY",
            "signing_key": "LEAK_SIGNING_KEY",
            "secret_key": "LEAK_SECRET_KEY",
            "sshkey": "LEAK_SSH_KEY",
            "private-key": "LEAK_HYPHEN_KEY",
            "repo_key": "keep-repo",
            "old_repo_key": "keep-old-repo",
            "context_id": "keep-context",
        })
        r = _get(org)
        for secret in ("LEAK_PRIVATE_KEY", "LEAK_ACCESS_KEY", "LEAK_SIGNING_KEY",
                       "LEAK_SECRET_KEY", "LEAK_SSH_KEY", "LEAK_HYPHEN_KEY"):
            assert secret not in r.text
        detail = r.json()["items"][0]["detail"]
        for key in ("private_key", "access_key", "signing_key", "secret_key",
                    "sshkey", "private-key"):
            assert detail[key] == "[REDACTED]"
        assert detail["repo_key"] == "keep-repo"
        assert detail["old_repo_key"] == "keep-old-repo"
        assert detail["context_id"] == "keep-context"


# ============================ 查询侧切片顺序（先切片后脱敏） ============================

class TestQuerySlicesBeforeMasking:
    def test_mask_only_applied_to_page_window(self, monkeypatch):
        """total 需全量匹配，但脱敏只对当前分页窗口执行（防审计量增长后 CPU 线性增长）。"""
        from src.domains.mgmt.auth import audit as audit_mod

        org = _mk_org("issue2260_mask_window", _UID)
        for i in range(6):
            _insert_audit("org_repo_bind", {"org_id": org, "repo_key": f"r/{i}"})

        calls = []
        orig = audit_mod.mask_detail

        def _spy(value, _depth=0, *args, **kwargs):
            if _depth == 0:                    # 只统计顶层（递归子调用不计）
                calls.append(1)
            return orig(value, _depth, *args, **kwargs)

        monkeypatch.setattr(audit_mod, "mask_detail", _spy)
        body = audit_mod.query_org_audit(org, limit=2, offset=1)
        assert body["total"] == 6
        assert len(body["items"]) == 2
        assert len(calls) == 2


# ============================ 白名单解析（纯函数） ============================

class TestResolveEventTypes:
    def test_empty_returns_org_universe(self):
        from src.domains.mgmt.auth.audit import ORG_AUDIT_EVENT_TYPES, _resolve_event_types
        assert _resolve_event_types("") == set(ORG_AUDIT_EVENT_TYPES)
        assert "org_repo_bind" in ORG_AUDIT_EVENT_TYPES
        assert "dispatch_created" in ORG_AUDIT_EVENT_TYPES

    def test_dispatch_alias(self):
        from src.domains.mgmt.auth.audit import _resolve_event_types
        assert _resolve_event_types("org_dispatch_") == {
            "dispatch_created", "target_created", "dispatch_completed",
            "dispatch_partial_failed", "dispatch_cancelled", "target_retried"}

    def test_user_input_never_becomes_event_name(self):
        """非白名单输入不得拼进查询集合（防注入）。"""
        from src.domains.mgmt.auth.audit import _resolve_event_types
        assert _resolve_event_types("org_repo_bind' OR '1'='1") == set()


class TestParseDetail:
    def test_invalid_json_returns_empty_dict(self):
        from src.domains.mgmt.auth.audit import _parse_detail
        assert _parse_detail("{not-json") == {}

    def test_non_dict_json_returns_empty_dict(self):
        from src.domains.mgmt.auth.audit import _parse_detail
        assert _parse_detail('["not", "an", "object"]') == {}
        assert _parse_detail('"scalar"') == {}

    def test_dict_passthrough(self):
        from src.domains.mgmt.auth.audit import _parse_detail
        value = {"org_id": 1}
        assert _parse_detail(value) == value


if __name__ == "__main__":
    unittest.main(verbosity=2)
