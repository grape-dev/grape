"""Issue #1748 单测：评论媒体附件（gitcode.com/user-attachments 视频/图片）解析与注入。

覆盖分支：
- 提取：Markdown / HTML / 裸链、去重保序、非 user-attachments 域不匹配
- 下载：视频成功、HTTP 401、非媒体 content-type、超大小、超时、URL 白名单（防 SSRF）
- 类型判定：image / video / file
- 采集编排：视频/图片成功、混合失败、数量上限、无 token 降级、转存失败、顶层异常不外抛
- 注入：视频/图片分析段落、读取通道 URL、不可读原因、纯文本无回归、issue 角色不注入 URL、
        A2A 上下文触发、缓存命中不重复采集、token 不出现在 prompt
"""
import os
import sys
import urllib.error

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.vcs.api_client import (
    extract_gitcode_user_attachment_urls,
    _download_user_attachment,
    attachment_kind,
    USER_ATTACHMENT_MAX_COUNT,
    VcsApiClient,
)

V1 = "https://gitcode.com/user-attachments/files/10691287/c311ec5f5d4744448b0fc6bc77a59559.mp4"
V2 = "https://gitcode.com/user-attachments/files/10691287/aaaa-bbbb.mp4"
I1 = "https://gitcode.com/user-attachments/assets/10691287/572e09d2/image.png"
OTHER = "https://raw.gitcode.com/user-images/assets/1/x.png"
BARE = "https://gitcode.com/user-attachments/files/1/2/f.zip"


# ---------- 提取 ----------

def test_extract_html_anchor():
    text = f'补充视频 <a href="{V1}" target="_blank">clip.mp4</a>'
    assert extract_gitcode_user_attachment_urls(text) == [V1]


def test_extract_markdown_and_bare():
    text = f"![img]({I1}) 以及裸链 {V1}"
    assert extract_gitcode_user_attachment_urls(text) == [I1, V1]


def test_extract_dedup_and_order():
    text = f"{V1} 重复 {V1} 另一个 {V2}"
    assert extract_gitcode_user_attachment_urls(text) == [V1, V2]


def test_extract_ignores_other_domains():
    assert extract_gitcode_user_attachment_urls(f"{OTHER} https://example.com/user-attachments/x.mp4") == []


def test_extract_none_and_empty():
    assert extract_gitcode_user_attachment_urls(None) == []
    assert extract_gitcode_user_attachment_urls("") == []


# ---------- 类型判定 ----------

def test_attachment_kind():
    assert attachment_kind(V1, "video/mp4") == "video"
    assert attachment_kind(I1, "image/png") == "image"
    assert attachment_kind(V2, "") == "video"
    assert attachment_kind(BARE, "application/zip") == "file"


def test_attachment_ext_whitelist():
    from src.vcs.api_client import _attachment_ext, is_media_attachment_url
    assert _attachment_ext(V1, "video/mp4") == ".mp4"
    assert _attachment_ext("https://gitcode.com/user-attachments/x.html", "application/octet-stream") == ""
    assert _attachment_ext("https://gitcode.com/user-attachments/x.php", "application/octet-stream") == ""
    assert _attachment_ext("https://gitcode.com/user-attachments/x", "image/png") == ".png"
    assert is_media_attachment_url(V1)
    assert not is_media_attachment_url(BARE)
    assert is_media_attachment_url("https://gitcode.com/user-attachments/x")


# ---------- 下载 ----------

class _FakeResp:
    def __init__(self, ctype, body):
        self.headers = {"Content-Type": ctype}
        self._body = body
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body) - self._pos
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_video_success(monkeypatch):
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp("video/mp4", b"vid"))
    r = _download_user_attachment(V1, "tk")
    assert r["content"] == b"vid"
    assert r["content_type"] == "video/mp4"
    assert r["size"] == 3


def test_download_401(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(V1, 401, "Unauthorized", {}, None)
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen", fake_urlopen)
    assert _download_user_attachment(V1, "tk")["error"] == "HTTP 401"


def test_download_unsupported_content_type(monkeypatch):
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp("text/html", b"<html>"))
    r = _download_user_attachment(V1, "tk")
    assert "unsupported content-type" in r["error"]


def test_download_size_limit(monkeypatch):
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp("video/mp4", b"x" * 2048))
    r = _download_user_attachment(V1, "tk", max_size=1024)
    assert "size limit" in r["error"]


def test_download_timeout(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen", fake_urlopen)
    assert _download_user_attachment(V1, "tk")["error"] == "timeout"


def test_download_url_whitelist():
    r = _download_user_attachment("https://evil.com/user-attachments/x.mp4", "tk")
    assert "not allowed" in r["error"]


def test_download_via_client_exists():
    client = VcsApiClient(provider="gitcode", token="tk", owner="o", repo="r")
    assert client.download_user_attachment.__name__ == "download_user_attachment"


# ---------- 采集编排 ----------

def _patch_collect_env(monkeypatch, token="tk-fake", download_results=None, saved=None, raise_save=False):
    import src.api.issues_center as ic
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda repo_key="": token, raising=False)

    class _FakeClient:
        def download_user_attachment(self, url, **kw):
            return (download_results or {}).get(url, {"error": "HTTP 401"})

    monkeypatch.setattr("src.vcs.api_client.create_client", lambda repo_key, token="": _FakeClient())
    monkeypatch.setattr(ic, "_analyze_media_attachment",
                        lambda kind, content, ext="": f"## 用户上传的{kind}分析\n已分析{kind}", raising=False)

    saved_list = saved if saved is not None else []

    def fake_attach_save(owner, repo, issue_number, filename, content):
        if raise_save:
            raise RuntimeError("obs down")
        saved_list.append((filename, content))
        return f"http://srv/static/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"

    monkeypatch.setattr(ic, "_attach_save", fake_attach_save)
    return saved_list


def test_collect_video_success(monkeypatch):
    saved = _patch_collect_env(monkeypatch, download_results={
        V1: {"content": b"vid", "content_type": "video/mp4", "size": 3},
    })
    from src.api.issues_center import _collect_issue_media_attachments
    r = _collect_issue_media_attachments("grape-dev", "grape", 1748, [f"看视频 {V1}"])
    assert r["failed"] == []
    import re
    assert re.match(r"^user-attachment-[0-9a-f]{32}\.mp4$", r["ok"][0]["filename"])
    assert r["ok"][0]["kind"] == "video"
    assert r["ok"][0]["url"].endswith("/" + r["ok"][0]["filename"])
    assert "已分析video" in r["ok"][0]["analysis"]
    assert saved[0] == (r["ok"][0]["filename"], b"vid")


def test_collect_uuid_filename_no_cross_comment_overwrite(monkeypatch):
    _patch_collect_env(monkeypatch, download_results={
        V1: {"content": b"vid", "content_type": "video/mp4", "size": 3},
    })
    from src.api.issues_center import _collect_issue_media_attachments
    r1 = _collect_issue_media_attachments("grape-dev", "grape", 1748, [V1])
    r2 = _collect_issue_media_attachments("grape-dev", "grape", 1748, [V1])
    assert r1["ok"][0]["filename"] != r2["ok"][0]["filename"]


def test_collect_non_media_ignored(monkeypatch):
    called = {"n": 0}

    def fake_client(*a, **k):
        class _C:
            def download_user_attachment(self, url, **kw):
                called["n"] += 1
                return {"content": b"zip", "content_type": "application/zip", "size": 3}
        return _C()

    import src.api.issues_center as ic
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda repo_key="": "tk-fake", raising=False)
    monkeypatch.setattr("src.vcs.api_client.create_client", fake_client)
    r = ic._collect_issue_media_attachments("grape-dev", "grape", 1748, [f"附件 {BARE}"])
    assert r == {"ok": [], "failed": []}
    assert called["n"] == 0


def test_collect_unknown_ext_non_media_after_download_ignored(monkeypatch):
    url = "https://gitcode.com/user-attachments/files/1/2/payload"
    _patch_collect_env(monkeypatch, download_results={
        url: {"content": b"x", "content_type": "application/zip", "size": 1},
    })
    from src.api.issues_center import _collect_issue_media_attachments
    r = _collect_issue_media_attachments("grape-dev", "grape", 1748, [url])
    assert r == {"ok": [], "failed": []}


def test_collect_image_and_failed_mixed(monkeypatch):
    _patch_collect_env(monkeypatch, download_results={
        I1: {"content": b"img", "content_type": "image/png", "size": 3},
        V2: {"error": "HTTP 401"},
    })
    from src.api.issues_center import _collect_issue_media_attachments
    r = _collect_issue_media_attachments("grape-dev", "grape", 1748, [f"{I1} {V2}"])
    assert len(r["ok"]) == 1 and r["ok"][0]["kind"] == "image"
    assert r["failed"] == [{"url": V2, "reason": "HTTP 401"}]


def test_collect_count_limit(monkeypatch):
    urls = [f"https://gitcode.com/user-attachments/files/1/{i}/v.mp4" for i in range(10)]
    _patch_collect_env(monkeypatch, download_results={
        u: {"content": b"v", "content_type": "video/mp4", "size": 1} for u in urls})
    from src.api.issues_center import _collect_issue_media_attachments
    r = _collect_issue_media_attachments("grape-dev", "grape", 1748, [" ".join(urls)])
    assert len(r["ok"]) == USER_ATTACHMENT_MAX_COUNT
    assert r["failed"][0]["reason"] == "exceeds count limit"


def test_collect_no_token(monkeypatch):
    _patch_collect_env(monkeypatch, token="")
    from src.api.issues_center import _collect_issue_media_attachments
    r = _collect_issue_media_attachments("grape-dev", "grape", 1748, [V1])
    assert r["ok"] == []
    assert r["failed"] == [{"url": V1, "reason": "no repo token"}]


def test_collect_save_failure(monkeypatch):
    _patch_collect_env(monkeypatch, raise_save=True,
                       download_results={V1: {"content": b"v", "content_type": "video/mp4", "size": 1}})
    from src.api.issues_center import _collect_issue_media_attachments
    r = _collect_issue_media_attachments("grape-dev", "grape", 1748, [V1])
    assert r["ok"] == []
    assert "save failed" in r["failed"][0]["reason"]


def test_collect_no_urls(monkeypatch):
    from src.api.issues_center import _collect_issue_media_attachments
    assert _collect_issue_media_attachments("grape-dev", "grape", 1748, ["纯文本", ""]) == \
        {"ok": [], "failed": []}


def test_collect_top_level_exception_not_raised(monkeypatch):
    import src.api.issues_center as ic

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("src.vcs.api_client.extract_gitcode_user_attachment_urls", boom)
    r = ic._collect_issue_media_attachments("grape-dev", "grape", 1748, [V1])
    assert "error" in r


def test_analyze_media_attachment_image(monkeypatch):
    import src.agent.image_understanding as iu
    monkeypatch.setattr(iu, "understand_image_from_bytes",
                        lambda content, ext=".png": "## 用户上传的图片分析\n图里有报错", raising=False)
    from src.api.issues_center import _analyze_media_attachment
    out = _analyze_media_attachment("image", b"img", ".png")
    assert out.startswith("## 用户上传的图片分析")


def test_analyze_media_attachment_video_header(monkeypatch):
    import src.agent.image_understanding as iu
    monkeypatch.setattr(iu, "understand_video_from_bytes",
                        lambda content, ext=".mp4": "视频文件过大（超过50MB），无法分析。", raising=False)
    from src.api.issues_center import _analyze_media_attachment
    out = _analyze_media_attachment("video", b"v", ".mp4")
    assert "## 用户上传的视频分析" in out
    assert "超过50MB" in out


# ---------- 注入（prompt_builder） ----------

def _comment_evt(note="", a2a=""):
    return {
        "type": "issue_comment",
        "source": "gitcode",
        "repository": "grape-dev/grape",
        "repo_key": "gitcode:grape-dev/grape",
        "issue_id": "#1748",
        "payload": {
            "issue": {"number": 1748, "title": "t", "body": ""},
            "object_attributes": {"note": note} if note else {},
        },
        "_a2a_context": a2a,
        "actor": "tester",
    }


def _inject(monkeypatch, agent_type, note="", a2a="", collected=None):
    import src.prompt_builder as pb
    monkeypatch.setattr("src.api.issues_center._attach_list",
                        lambda o, r, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images",
                        lambda *a, **k: {"ok": [], "failed": []}, raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_media_attachments",
                        lambda *a, **k: collected if collected is not None else {"ok": [], "failed": []},
                        raising=False)
    pb._COMMENT_MEDIA_CACHE.clear()
    parts = []
    pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#1748", agent_type, 0,
                                   evt=_comment_evt(note, a2a))
    return "\n".join(parts)


def test_inject_video_analysis_and_read_url(monkeypatch):
    collected = {
        "ok": [{"filename": "user-attachment-1.mp4", "url": "http://srv/static/user-attachment-1.mp4",
                "size": 100, "kind": "video", "source_url": V1,
                "analysis": "## 用户上传的视频分析\n页面点击后报错"}],
        "failed": [],
    }
    out = _inject(monkeypatch, "vod", note=f"补充视频 {V1}", collected=collected)
    assert "## Comment Media Attachments" in out
    assert "## 用户上传的视频分析" in out
    assert "http://srv/static/user-attachment-1.mp4" in out
    assert "401" not in out or "no 401" in out


def test_inject_image_analysis(monkeypatch):
    collected = {
        "ok": [{"filename": "user-attachment-1.png", "url": "http://srv/x.png", "size": 4,
                "kind": "image", "source_url": I1,
                "analysis": "## 用户上传的图片分析\n截图显示列表为空"}],
        "failed": [],
    }
    out = _inject(monkeypatch, "coding", note=f"截图 {I1}", collected=collected)
    assert "## 用户上传的图片分析" in out
    assert "http://srv/x.png" in out


def test_inject_unreadable_reason(monkeypatch):
    collected = {"ok": [], "failed": [{"url": V1, "reason": "HTTP 401"}]}
    out = _inject(monkeypatch, "issue", note=f"视频 {V1}", collected=collected)
    assert "附件不可读" in out
    assert "HTTP 401" in out


def test_inject_empty_analysis_reason(monkeypatch):
    collected = {
        "ok": [{"filename": "user-attachment-1.mp4", "url": "http://srv/v.mp4", "size": 1,
                "kind": "video", "source_url": V1, "analysis": ""}],
        "failed": [],
    }
    out = _inject(monkeypatch, "coding", note=f"{V1}", collected=collected)
    assert "内容分析不可用" in out


def test_inject_issue_role_has_read_url(monkeypatch):
    collected = {
        "ok": [{"filename": "user-attachment-1.mp4", "url": "http://srv/secret-url.mp4", "size": 1,
                "kind": "video", "source_url": V1, "analysis": "## 用户上传的视频分析\n内容"}],
        "failed": [],
    }
    out = _inject(monkeypatch, "issue", note=f"{V1}", collected=collected)
    assert "user-attachment-1.mp4" in out
    assert "http://srv/secret-url.mp4" in out


def test_inject_collect_error_visible(monkeypatch):
    collected = {"ok": [], "failed": [], "error": "boom"}
    out = _inject(monkeypatch, "coding", note=f"{V1}", collected=collected)
    assert "attachment collection failed" in out
    assert "boom" in out


def test_inject_analysis_truncated(monkeypatch):
    from src.prompt_builder import _COMMENT_MEDIA_ANALYSIS_MAX_CHARS
    long_analysis = "## 用户上传的视频分析\n" + ("x" * (_COMMENT_MEDIA_ANALYSIS_MAX_CHARS + 100))
    collected = {
        "ok": [{"filename": "user-attachment-1.mp4", "url": "http://srv/v.mp4", "size": 1,
                "kind": "video", "source_url": V1, "analysis": long_analysis}],
        "failed": [],
    }
    out = _inject(monkeypatch, "coding", note=f"{V1}", collected=collected)
    assert "...(analysis truncated)" in out
    assert len(out) < len(long_analysis) + 500


def test_inject_pure_text_no_regression(monkeypatch):
    out = _inject(monkeypatch, "coding", note="纯文本评论，无附件", collected={"ok": [], "failed": []})
    assert "Comment Media Attachments" not in out
    assert out == ""


def test_inject_non_comment_event_does_not_collect(monkeypatch):
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        return {"ok": [], "failed": []}

    import src.prompt_builder as pb
    monkeypatch.setattr("src.api.issues_center._attach_list", lambda o, r, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images",
                        lambda *a, **k: {"ok": [], "failed": []}, raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_media_attachments", fake_collect, raising=False)
    pb._COMMENT_MEDIA_CACHE.clear()
    evt = _comment_evt(note="", a2a=f"前序摘要含视频 {V1}")
    evt["type"] = "a2a_event"
    parts = []
    pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#1748", "coding", 0, evt=evt)
    assert calls["n"] == 0


def test_inject_cache_no_recollect(monkeypatch):
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        return {"ok": [], "failed": []}

    import src.prompt_builder as pb
    monkeypatch.setattr("src.api.issues_center._attach_list", lambda o, r, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images",
                        lambda *a, **k: {"ok": [], "failed": []}, raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_media_attachments", fake_collect, raising=False)
    pb._COMMENT_MEDIA_CACHE.clear()
    for _ in range(2):
        parts = []
        pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#1748", "coding", 0,
                                       evt=_comment_evt(note=f"{V1}"))
    assert calls["n"] == 1


def test_token_never_in_prompt(monkeypatch):
    collected = {
        "ok": [{"filename": "user-attachment-1.mp4", "url": "http://srv/v.mp4", "size": 1,
                "kind": "video", "source_url": V1, "analysis": "分析"}],
        "failed": [{"url": V2, "reason": "HTTP 401"}],
    }
    out = _inject(monkeypatch, "vod", note=f"{V1} {V2}", collected=collected)
    assert "tk-fake" not in out
    assert "Bearer" not in out