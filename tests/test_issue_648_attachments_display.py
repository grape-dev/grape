"""Issue #648 单测：issue 附件展示链路打通。

覆盖：
1. _attachments_markdown_block：图片/非图片/空/无效条目
2. sync_issue_attachment_comment：成功、非 gitcode、无 token、VCS 异常降级
3. _issue_attachments（routes）：正常聚合、多 repo_key 探测、异常降级 []、空编号 []
4. issues_detail 端点：响应含 attachments 字段（无附件 []）
5. issues_list（统一列表）端点：卡片含 attachments 字段
6. upload_issue_attachment：已建单路径触发同步评论；pending 路径不触发
"""
import asyncio
import io
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.session_manager import db, db_lock

REPO = "gitcode:own/repo648"
REPO_KEY_ALT = "gitcode:own/other648"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key IN (?,?)", (REPO, REPO_KEY_ALT))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key IN (?,?)", (REPO, REPO_KEY_ALT))
        db.commit()


# ===== 1. _attachments_markdown_block =====

class TestMarkdownBlock:
    def test_image_and_file(self):
        from src.api.issues_center import _attachments_markdown_block
        md = _attachments_markdown_block([
            {"filename": "a.png", "url": "http://u/a.png", "is_image": True},
            {"filename": "b.md", "url": "http://u/b.md", "is_image": False},
        ])
        assert md == "\n\n## 附件\n![a.png](http://u/a.png)\n[b.md](http://u/b.md)"

    def test_empty(self):
        from src.api.issues_center import _attachments_markdown_block
        assert _attachments_markdown_block([]) == ""
        assert _attachments_markdown_block(None) == ""

    def test_invalid_entries_skipped(self):
        from src.api.issues_center import _attachments_markdown_block
        md = _attachments_markdown_block([
            {"filename": "", "url": "http://u/x"},
            {"filename": "y", "url": ""},
            "not-a-dict",
            {"filename": "ok.txt", "url": "http://u/ok.txt"},
        ])
        assert md == "\n\n## 附件\n[ok.txt](http://u/ok.txt)"


# ===== 2. sync_issue_attachment_comment =====

class TestSyncComment:
    def _run(self, monkeypatch, provider="gitcode", token="tok", client_impl=None):
        import src.api.issues_center as ic
        import src.user.db as udb
        import src.vcs.api_client as vac
        calls = []
        if client_impl is None:
            client_impl = type("C", (), {
                "__init__": lambda self, *a, **k: None,
                "post_issue_comment": lambda self, n, body: calls.append((n, body)) or {"id": 1}
            })
        monkeypatch.setattr(vac, "VcsApiClient", client_impl)
        monkeypatch.setattr(udb, "get_gitcode_token", lambda repo_key="": token)
        ok = ic.sync_issue_attachment_comment(
            f"{provider}:own/repo648", 85,
            {"filename": "doc.md", "url": "http://u/doc.md", "is_image": False})
        return ok, calls

    def test_success(self, monkeypatch):
        ok, calls = self._run(monkeypatch)
        assert ok is True
        assert calls == [(85, "## 附件\n[doc.md](http://u/doc.md)")]

    def test_non_gitcode_skipped(self, monkeypatch):
        ok, calls = self._run(monkeypatch, provider="github")
        assert ok is False and calls == []

    def test_no_token(self, monkeypatch):
        ok, calls = self._run(monkeypatch, token="")
        assert ok is False and calls == []

    def test_vcs_exception_degrades(self, monkeypatch):
        def boom(self, n, body):
            raise RuntimeError("vcs down")
        ok, calls = self._run(monkeypatch, client_impl=type("C", (), {"post_issue_comment": boom}))
        assert ok is False

    def test_obs_url_resigned_long_lived(self, monkeypatch):
        # Issue #648 Required: 同步评论不得写 7 天签名 URL；OBS 启用时按对象键重签长效链接
        import src.api.issues_center as ic
        import src.storage as storage_mod
        import src.user.db as udb
        import src.vcs.api_client as vac
        resign_calls = []

        class FakeStorage:
            def temporary_url(self, key, expires=300):
                resign_calls.append((key, expires))
                return f"http://long/{key}"

        monkeypatch.setattr(storage_mod, "get_storage", lambda: FakeStorage())
        calls = []
        monkeypatch.setattr(vac, "VcsApiClient", type("C", (), {
            "__init__": lambda self, *a, **k: None,
            "post_issue_comment": lambda self, n, body: calls.append((n, body)) or {"id": 1}
        }))
        monkeypatch.setattr(udb, "get_gitcode_token", lambda repo_key="": "tok")
        ok = ic.sync_issue_attachment_comment(
            "gitcode:own/repo648", 85,
            {"filename": "doc.md", "url": "http://u/85/doc-uuid.md?X=1", "is_image": False})
        assert ok is True
        # 重签发生且时效为长效（10 年）
        assert len(resign_calls) == 1
        assert resign_calls[0][0].endswith("issue_attachments/own/repo648/85/doc-uuid.md")
        assert resign_calls[0][1] == ic._ATTACH_SYNC_URL_EXPIRES
        # 评论里是长效 URL，而非原 7 天签名 URL
        assert calls == [(85, "## 附件\n[doc.md](http://long/%s)" % resign_calls[0][0])]

    def test_obs_signature_with_slash_keeps_real_filename(self, monkeypatch):
        # 回归（2026-11-06 实障）：OBS 签名串可含 "/"，按整串 rsplit 会把签名碎片
        # 当文件名 → 重签发的是不存在的对象键，评论链接 404。
        import src.api.issues_center as ic
        import src.storage as storage_mod
        import src.user.db as udb
        import src.vcs.api_client as vac
        keys = []

        class FakeStorage:
            def temporary_url(self, key, expires=300):
                keys.append(key)
                return f"http://long/{key}"

        monkeypatch.setattr(storage_mod, "get_storage", lambda: FakeStorage())
        monkeypatch.setattr(vac, "VcsApiClient", type("C", (), {
            "__init__": lambda self, *a, **k: None,
            "post_issue_comment": lambda self, n, body: {"id": 1}
        }))
        monkeypatch.setattr(udb, "get_gitcode_token", lambda repo_key="": "tok")
        signed_url = ("https://bucket.obs.cn-north-4.myhuaweicloud.com/grape/"
                      "issue_attachments/own/repo648/1254/ea056975.md"
                      "?Expires=1794537983&AccessKeyId=HPUANG&Signature=7rDURFuPz0ri/uTx%2Fk")
        ok = ic.sync_issue_attachment_comment(
            "gitcode:own/repo648", 1254,
            {"filename": "im-wecom-adapter-requirements.md", "url": signed_url, "is_image": False})
        assert ok is True
        assert len(keys) == 1
        # 键必须以真实文件名结尾，且绝不能带签名碎片（uTx 来自 Signature 段）
        assert keys[0].endswith("/issue_attachments/own/repo648/1254/ea056975.md")
        assert "uTx" not in keys[0] and "Signature" not in keys[0]

    def test_resign_failure_falls_back_to_original_url(self, monkeypatch):
        # 重签异常降级：仍用原 URL 发评论（best-effort 不阻塞）
        import src.api.issues_center as ic
        import src.storage as storage_mod
        import src.user.db as udb
        import src.vcs.api_client as vac

        class BoomStorage:
            def temporary_url(self, key, expires=300):
                raise RuntimeError("sign down")

        monkeypatch.setattr(storage_mod, "get_storage", lambda: BoomStorage())
        calls = []
        monkeypatch.setattr(vac, "VcsApiClient", type("C", (), {
            "__init__": lambda self, *a, **k: None,
            "post_issue_comment": lambda self, n, body: calls.append((n, body)) or {"id": 1}
        }))
        monkeypatch.setattr(udb, "get_gitcode_token", lambda repo_key="": "tok")
        ok = ic.sync_issue_attachment_comment(
            "gitcode:own/repo648", 85,
            {"filename": "doc.md", "url": "http://u/85/doc.md", "is_image": False})
        assert ok is True
        assert calls == [(85, "## 附件\n[doc.md](http://u/85/doc.md)")]


# ===== 3. routes._issue_attachments =====

class TestIssueAttachmentsHelper:
    def test_aggregate(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        monkeypatch.setattr(ic, "_attach_list",
                            lambda o, r, n: [{"filename": f"{n}.md", "url": "http://u", "size": 5,
                                               "is_image": False}] if n == "85" else [])
        assert ri._issue_attachments([REPO], 85) == [
            {"filename": "85.md", "url": "http://u", "size": 5, "is_image": False}]

    def test_first_repo_key_hit(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        monkeypatch.setattr(ic, "_attach_list",
                            lambda o, r, n: [{"filename": "x", "url": "http://x", "size": 1,
                                               "is_image": True}] if r == "repo648" else [])
        assert ri._issue_attachments([REPO_KEY_ALT, REPO], 3)[0]["filename"] == "x"

    def test_exception_degrades_to_empty(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        def boom(o, r, n):
            raise RuntimeError("storage down")
        monkeypatch.setattr(ic, "_attach_list", boom)
        assert ri._issue_attachments([REPO], 85) == []

    def test_empty_number(self):
        import src.api.routes.issues as ri
        assert ri._issue_attachments([REPO], None) == []
        assert ri._issue_attachments([REPO], "") == []


# ===== 4. issues_detail 端点 =====

class TestDetailEndpoint:
    def test_detail_has_attachments(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        import src.api.pipeline_status as ps
        monkeypatch.setattr(ri, "_repo_keys_of", lambda o, r: [REPO])
        monkeypatch.setattr(ri, "_enforce_session_repo_scope", lambda u, ks: None)
        monkeypatch.setattr(ri, "_issue_stage_map", lambda ks, numbers=None: {"85": {"stage": "vod"}})
        monkeypatch.setattr(ri, "_task_status_map", lambda ks: {})
        monkeypatch.setattr(ri, "_group_events_by_issue", lambda ks, numbers=None: {"85": []})
        monkeypatch.setattr(ri, "_issue_title_map", lambda ks: {})
        monkeypatch.setattr(ri, "_pipeline_from_events",
                            lambda rk, n, evs: {"issue": {}, "summary": "", "status": "unknown",
                                                "segments": []})
        monkeypatch.setattr(ps, "load_issue_business_status", lambda ks, n: ("", ""))
        monkeypatch.setattr(ps, "load_segment_task_refs", lambda ks, seqs: {})
        monkeypatch.setattr(ps, "enrich_pipeline_business_status", lambda *a, **k: None)
        monkeypatch.setattr(ps, "_pipeline_segment_seqs", lambda p: [])
        monkeypatch.setattr(ic, "_attach_list",
                            lambda o, r, n: [{"filename": "spec.md", "url": "http://u/spec.md",
                                               "size": 9, "is_image": False}] if n == "85" else [])
        out = ri.get_issue_detail("own", "repo648", 85, user={"id": 1, "auth_type": "jwt"})
        assert out["attachments"] == [{"filename": "spec.md", "url": "http://u/spec.md",
                                        "size": 9, "is_image": False}]

    def test_detail_no_attachments(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        import src.api.pipeline_status as ps
        monkeypatch.setattr(ri, "_repo_keys_of", lambda o, r: [REPO])
        monkeypatch.setattr(ri, "_enforce_session_repo_scope", lambda u, ks: None)
        monkeypatch.setattr(ri, "_issue_stage_map", lambda ks, numbers=None: {"86": {"stage": "vod"}})
        monkeypatch.setattr(ri, "_task_status_map", lambda ks: {})
        monkeypatch.setattr(ri, "_group_events_by_issue", lambda ks, numbers=None: {"86": []})
        monkeypatch.setattr(ri, "_issue_title_map", lambda ks: {})
        monkeypatch.setattr(ri, "_pipeline_from_events",
                            lambda rk, n, evs: {"issue": {}, "summary": "", "status": "unknown",
                                                "segments": []})
        monkeypatch.setattr(ps, "load_issue_business_status", lambda ks, n: ("", ""))
        monkeypatch.setattr(ps, "load_segment_task_refs", lambda ks, seqs: {})
        monkeypatch.setattr(ps, "enrich_pipeline_business_status", lambda *a, **k: None)
        monkeypatch.setattr(ps, "_pipeline_segment_seqs", lambda p: [])
        monkeypatch.setattr(ic, "_attach_list", lambda o, r, n: [])
        out = ri.get_issue_detail("own", "repo648", 86, user={"id": 1, "auth_type": "jwt"})
        assert out["attachments"] == []


# ===== 5. issues_list（统一列表）端点 =====

class TestListEndpoint:
    def _patch(self, monkeypatch, items, atts):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        import src.user.org_db as orgdb
        monkeypatch.setattr(ri, "_repo_keys_of", lambda o, r: [REPO])
        monkeypatch.setattr(ri, "_group_events_by_issue", lambda ks, numbers=None: {})
        monkeypatch.setattr(ri, "_issue_stage_map", lambda ks, numbers=None: {})
        monkeypatch.setattr(ri, "_query_pr_subscription_events", lambda ks: [])
        monkeypatch.setattr(ic, "_query_tasks", lambda where, params, order_by,
                            include_user_id=False, limit=None, offset=None: list(items))
        monkeypatch.setattr(ic, "_build_assignee_filter", lambda a, clauses, params: None)
        monkeypatch.setattr(orgdb, "is_org_personal", lambda org_id: True)
        monkeypatch.setattr(ic, "_attach_list", lambda o, r, n: list(atts) if n == "85" else [])

    def test_list_cards_have_attachments(self, monkeypatch):
        import src.api.routes.issues as ri
        items = [{"id": 11, "repo_key": REPO, "issue_number": 85, "status": "active",
                  "created_at": "2026-11-02 00:00:00"},
                 {"id": 12, "repo_key": REPO, "issue_number": 90, "status": "active",
                  "created_at": "2026-11-02 00:00:00"}]
        atts = [{"filename": "img.png", "url": "http://u/img.png", "size": 3, "is_image": True}]
        self._patch(monkeypatch, items, atts)
        out = ri.list_repo_issues("own", "repo648", status="", sort_order="desc", mode="time",
                                  assignees="", page=1, per_page=0, recent_terminal=0,
                                  user={"id": 1, "org_id": 0})
        by_num = {it["issue_number"]: it for it in out["issues"]}
        assert by_num[85]["attachments"] == atts
        assert by_num[90]["attachments"] == []

    def test_list_no_attachments_empty(self, monkeypatch):
        import src.api.routes.issues as ri
        items = [{"id": 11, "repo_key": REPO, "issue_number": 85, "status": "active",
                  "created_at": "2026-11-02 00:00:00"}]
        self._patch(monkeypatch, items, [])
        out = ri.list_repo_issues("own", "repo648", status="", sort_order="desc", mode="time",
                                  assignees="", page=1, per_page=0, recent_terminal=0,
                                  user={"id": 1, "org_id": 0})
        assert out["issues"][0]["attachments"] == []

    def test_pr_cards_skip_attachments_injection(self, monkeypatch):
        # Issue #648 Required: PR 订阅伪卡片（is_pr_event）不得注入 issue 附件
        # （PR 号与同仓 issue 编号重合时会命中错误附件目录）
        import src.api.routes.issues as ri
        items = [{"id": 11, "repo_key": REPO, "issue_number": 85, "status": "active",
                  "created_at": "2026-11-02 00:00:00"},
                 {"id": 12, "repo_key": REPO, "issue_number": 85, "status": "active",
                  "created_at": "2026-11-02 00:01:00", "is_pr_event": True, "pr_seq": 9}]
        atts = [{"filename": "img.png", "url": "http://u/img.png", "size": 3, "is_image": True}]
        self._patch(monkeypatch, items, atts)
        out = ri.list_repo_issues("own", "repo648", status="", sort_order="desc", mode="time",
                                  assignees="", page=1, per_page=0, recent_terminal=0,
                                  user={"id": 1, "org_id": 0})
        by_id = {it["id"]: it for it in out["issues"]}
        assert by_id[11]["attachments"] == atts      # 真实 issue 卡片正常注入
        assert by_id[12]["attachments"] == []        # PR 伪卡片跳过注入

    def test_pr_subscription_cards_carry_empty_attachments(self, monkeypatch):
        # PR 订阅伪卡片混入列表时 attachments 兜底恒为 []（前端免回退请求、防错配）
        import src.api.routes.issues as ri
        items = [{"id": 11, "repo_key": REPO, "issue_number": 90, "status": "active",
                  "created_at": "2026-11-02 00:00:00"}]
        pr_card = {"id": -85, "issue_number": 85, "title": "PR #85", "status": "running",
                   "created_at": "2026-11-02 00:02:00", "repo_key": REPO,
                   "is_pr_event": True, "pr_seq": 5, "qa_history": [], "events_count": 1,
                   "latest_event": None, "assignee": "u"}
        self._patch(monkeypatch, items, [])
        monkeypatch.setattr(ri, "_query_pr_subscription_events", lambda ks: [dict(pr_card)])
        out = ri.list_repo_issues("own", "repo648", status="", sort_order="desc", mode="time",
                                  assignees="", page=1, per_page=0, recent_terminal=0,
                                  user={"id": 1, "org_id": 0})
        pr_items = [it for it in out["issues"] if it.get("is_pr_event")]
        assert pr_items and pr_items[0]["attachments"] == []


# ===== 6. upload_issue_attachment 同步评论触发 =====

class TestUploadSync:
    def _mkfile(self, name="doc.md", content=b"data"):
        from fastapi import UploadFile
        return UploadFile(file=io.BytesIO(content), filename=name)

    def _insert_issue(self, issue_number):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status)"
                      " VALUES (1,'',?,?,?, 'active')",
                      (REPO, issue_number, f"title-{issue_number}"))
            db.commit()
            return c.lastrowid

    def test_sync_called_for_created_issue(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        issue_id = self._insert_issue(85)
        monkeypatch.setattr(ic, "_attach_save", lambda o, r, d, f, content: "http://u/85/doc.md")
        sync_calls = []
        monkeypatch.setattr(ic, "sync_issue_attachment_comment",
                            lambda rk, n, att: sync_calls.append((rk, n, att)) or True)
        out = asyncio.run(ri.upload_issue_attachment(issue_id, self._mkfile("doc.md"),
                                                     user={"id": 1}))
        assert out["filename"] == "doc.md"
        assert sync_calls == [(REPO, 85, {"filename": "doc.md", "url": "http://u/85/doc.md",
                                          "is_image": False})]

    def test_pending_path_no_sync(self, monkeypatch):
        import src.api.issues_center as ic
        import src.api.routes.issues as ri
        issue_id = self._insert_issue(0)
        monkeypatch.setattr(ic, "_attach_save", lambda o, r, d, f, content: "http://u/p/doc.md")
        sync_calls = []
        monkeypatch.setattr(ic, "sync_issue_attachment_comment",
                            lambda rk, n, att: sync_calls.append((rk, n, att)) or True)
        asyncio.run(ri.upload_issue_attachment(issue_id, self._mkfile("doc.md"), user={"id": 1}))
        assert sync_calls == []
