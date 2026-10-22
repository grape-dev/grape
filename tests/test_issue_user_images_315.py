"""Issue #315 单测：GitCode 编辑器上传图片（raw.gitcode.com/user-images）识别 / 鉴权下载 / 附件注入。

覆盖分支：
- 提取：Markdown 图、HTML img、裸链、混合去重保序、非 user-images 域不匹配、边界字符截断
- 下载：成功（image/png）、HTTP 403、非图片 content-type、超大小、超时、URL 白名单（防 SSRF）
- 编排（_collect_issue_user_images）：成功清单、失败清单、数量上限、无 token 降级、顶层异常不外抛
- 注入（prompt_builder）：vod/coding 成功注入 URL、issue 只注入文件名、失败降级说明、qa/sre 不注入、
  token 不出现在 prompt、缓存命中不重复采集
"""
import io
import os
import sys
import urllib.error
import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.vcs.api_client import (
    extract_gitcode_user_image_urls,
    _download_user_image,
    USER_IMAGE_MAX_COUNT,
    VcsApiClient,
)

U1 = "https://raw.gitcode.com/user-images/assets/10691287/572e09d2-1f2c-45ea-b83d-024b569bca4e/image.png"
U2 = "https://raw.gitcode.com/user-images/assets/10691287/aaaa-bbbb/image.jpg"
OTHER = "https://gitcode.com/grape-dev/grape/raw/main/docs/a.png"
RAW_OTHER = "https://raw.githubusercontent.com/user-images/x.png"


# ---------- 提取 ----------

def test_extract_markdown_image():
    text = f"看截图 ![img]({U1}) 定位问题"
    assert extract_gitcode_user_image_urls(text) == [U1]


def test_extract_html_img():
    text = f'<img src="{U2}" alt="screenshot">'
    assert extract_gitcode_user_image_urls(text) == [U2]


def test_extract_bare_url():
    assert extract_gitcode_user_image_urls(f"链接：{U1}") == [U1]


def test_extract_dedup_and_order():
    text = f"{U1} 与 ![a]({U2}) 与重复 {U1}"
    urls = extract_gitcode_user_image_urls(text)
    assert urls == [U1, U2]


def test_extract_ignores_non_user_images():
    text = f"{OTHER} {RAW_OTHER} https://raw.gitcode.com/other/path.png"
    assert extract_gitcode_user_image_urls(text) == []


def test_extract_right_boundary():
    # 右侧括号/引号/空白不应进入 URL
    text = f"![a]({U1}) 和 \"{U2}\""
    urls = extract_gitcode_user_image_urls(text)
    assert urls == [U1, U2]


def test_extract_empty():
    assert extract_gitcode_user_image_urls("") == []
    assert extract_gitcode_user_image_urls(None) == []


# ---------- 下载（mock urlopen） ----------

class _FakeResp:
    def __init__(self, ctype, data):
        self.headers = {"Content-Type": ctype}
        self._data = data

    def read(self, n=-1):
        if not self._data:
            return b""
        chunk = self._data[:n if n and n > 0 else len(self._data)]
        self._data = self._data[len(chunk):]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_success(monkeypatch):
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp("image/png", b"\x89PNG-fake"))
    r = _download_user_image(U1, "fake-token")
    assert "error" not in r
    assert r["content"] == b"\x89PNG-fake"
    assert r["content_type"] == "image/png"
    assert r["size"] == 9


def test_download_authorization_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp("image/png", b"abc")

    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen", fake_urlopen)
    _download_user_image(U1, "fake-token")
    assert captured["auth"] == "Bearer fake-token"


def test_download_403(monkeypatch):
    import email.message

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(U1, 403, "Forbidden", email.message.Message(), io.BytesIO(b"no access"))

    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen", fake_urlopen)
    r = _download_user_image(U1, "fake-token")
    assert r["error"] == "HTTP 403"


def test_download_non_image(monkeypatch):
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp("text/html", b"<html>"))
    r = _download_user_image(U1, "fake-token")
    assert "non-image content-type" in r["error"]


def test_download_size_limit(monkeypatch):
    big = b"x" * (1024 * 1024)
    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp("image/png", big))
    r = _download_user_image(U1, "fake-token", max_size=1024)
    assert "size limit" in r["error"]


def test_download_timeout(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(urllib.error.URLError("timed out"))

    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen", fake_urlopen)
    r = _download_user_image(U1, "fake-token")
    assert r["error"] == "timeout"


def test_download_url_whitelist():
    # 防 SSRF：非 user-images 前缀直接拒绝，不发网络请求
    r = _download_user_image("https://raw.gitcode.com/api/v5/repos/x", "fake-token")
    assert "not allowed" in r["error"]
    r2 = _download_user_image("http://evil.com/user-images/x.png", "fake-token")
    assert "not allowed" in r2["error"]


def test_download_via_client():
    client = VcsApiClient(provider="gitcode", token="tk-fake", owner="o", repo="r")
    # 仅验证方法存在且复用内部实现（网络分支已在上游覆盖）
    assert client.download_user_image.__name__ == "download_user_image"


# ---------- 编排（_collect_issue_user_images） ----------

def _patch_collect_env(monkeypatch, token="tk-fake", download_results=None, saved=None, raise_save=False):
    """给 _collect_issue_user_images 打桩：token、下载结果、_attach_save。"""
    import src.api.issues_center as ic

    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda repo_key="": token, raising=False)

    class _FakeClient:
        def download_user_image(self, url, **kw):
            return (download_results or {}).get(url, {"error": "HTTP 403"})

    monkeypatch.setattr("src.vcs.api_client.create_client", lambda repo_key, token="": _FakeClient())

    saved_list = saved if saved is not None else []

    def fake_attach_save(owner, repo, issue_number, filename, content):
        if raise_save:
            raise RuntimeError("obs down")
        url = f"http://srv/static/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"
        saved_list.append((filename, content))
        return url

    monkeypatch.setattr(ic, "_attach_save", fake_attach_save)
    return saved_list


def test_collect_success(monkeypatch):
    saved = _patch_collect_env(monkeypatch, download_results={
        U1: {"content": b"img1", "content_type": "image/png", "size": 4},
    })
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, [f"正文 ![a]({U1})"])
    assert r["ok"] == [{"filename": "user-image-1.png", "url": saved and saved[0] and f"http://srv/static/issue_attachments/grape-dev/grape/315/user-image-1.png", "size": 4}]
    assert r["failed"] == []
    assert saved[0] == ("user-image-1.png", b"img1")


def test_collect_mixed_ok_failed(monkeypatch):
    _patch_collect_env(monkeypatch, download_results={
        U1: {"content": b"img1", "content_type": "image/png", "size": 4},
        U2: {"error": "HTTP 403"},
    })
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, [f"{U1} {U2}"])
    assert len(r["ok"]) == 1
    assert r["failed"] == [{"url": U2, "reason": "HTTP 403"}]


def test_collect_count_limit(monkeypatch):
    urls = [f"https://raw.gitcode.com/user-images/assets/1/{i}/image.png" for i in range(12)]
    _patch_collect_env(monkeypatch, download_results={
        u: {"content": b"x", "content_type": "image/png", "size": 1} for u in urls})
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, [" ".join(urls)])
    assert len(r["ok"]) == USER_IMAGE_MAX_COUNT
    assert len(r["failed"]) == 12 - USER_IMAGE_MAX_COUNT
    assert r["failed"][0]["reason"] == "exceeds count limit"


def test_collect_no_token(monkeypatch):
    _patch_collect_env(monkeypatch, token="")
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, [U1])
    assert r["ok"] == []
    assert r["failed"] == [{"url": U1, "reason": "no repo token"}]


def test_collect_save_failure_degrades(monkeypatch):
    _patch_collect_env(monkeypatch, raise_save=True,
                       download_results={U1: {"content": b"img1", "content_type": "image/png", "size": 4}})
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, [U1])
    assert r["ok"] == []
    assert len(r["failed"]) == 1
    assert "save failed" in r["failed"][0]["reason"]


def test_collect_no_urls(monkeypatch):
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, ["没有图片的正文", ""])
    assert r == {"ok": [], "failed": []}


def test_collect_top_level_exception_not_raised(monkeypatch):
    import src.api.issues_center as ic

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("src.vcs.api_client.extract_gitcode_user_image_urls", boom)
    from src.api.issues_center import _collect_issue_user_images
    r = _collect_issue_user_images("grape-dev", "grape", 315, [U1])  # 不应抛出
    assert "error" in r


# ---------- 注入（prompt_builder） ----------

def _base_evt(body="", note="", a2a=""):
    return {
        "type": "issue_opened",
        "source": "gitcode",
        "repository": "grape-dev/grape",
        "repo_key": "gitcode:grape-dev/grape",
        "issue_id": "#315",
        "payload": {
            "issue": {"number": 315, "title": "t", "body": body},
            "object_attributes": {"note": note} if note else {},
        },
        "_a2a_context": a2a,
        "actor": "tester",
    }


def _inject(monkeypatch, agent_type, body="", note="", a2a="", collected=None):
    import src.prompt_builder as pb
    monkeypatch.setattr("src.api.issues_center._attach_list",
                        lambda owner, repo, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images",
                        lambda owner, repo, n, texts, repo_key="": collected if collected is not None else
                        {"ok": [], "failed": []}, raising=False)
    pb._USER_IMAGE_CACHE.clear()
    parts = []
    pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#315", agent_type, 0,
                                    evt=_base_evt(body, note, a2a))
    return "\n".join(parts)


def test_inject_vod_ok_and_failed(monkeypatch):
    collected = {
        "ok": [{"filename": "user-image-1.png", "url": "http://srv/static/x/user-image-1.png", "size": 4}],
        "failed": [{"url": U2, "reason": "HTTP 403"}],
    }
    out = _inject(monkeypatch, "vod", body=f"![a]({U1}) {U2}", collected=collected)
    assert "## User Attachments (GitCode editor-uploaded images)" in out
    assert "user-image-1.png: http://srv/static/x/user-image-1.png" in out
    assert "user image fetch failed" in out
    assert "HTTP 403" in out


def test_inject_coding(monkeypatch):
    collected = {"ok": [{"filename": "user-image-1.png", "url": "http://srv/x.png", "size": 4}], "failed": []}
    out = _inject(monkeypatch, "coding", body=f"![a]({U1})", collected=collected)
    assert "user-image-1.png: http://srv/x.png" in out
    assert "download them into the current working directory" in out


def test_inject_issue_filenames_only(monkeypatch):
    collected = {"ok": [{"filename": "user-image-1.png", "url": "http://srv/x.png", "size": 4}], "failed": []}
    out = _inject(monkeypatch, "issue", body=f"![a]({U1})", collected=collected)
    assert "user-image-1.png" in out
    assert "http://srv/x.png" not in out  # issue 角色不注入 URL


def test_inject_qa_not_injected(monkeypatch):
    # qa/sre 不走附件注入（角色分级），_inject_attachments_by_role 直接 return
    import src.prompt_builder as pb
    pb._USER_IMAGE_CACHE.clear()
    parts = []
    pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#315", "qa", 0, evt=_base_evt())
    assert parts == []


def test_inject_no_images_no_section(monkeypatch):
    out = _inject(monkeypatch, "vod", body="纯文本正文", collected={"ok": [], "failed": []})
    assert "GitCode editor-uploaded images" not in out


def test_inject_collect_called_with_event_texts(monkeypatch):
    captured = {}

    def fake_collect(owner, repo, n, texts, repo_key=""):
        captured["texts"] = list(texts)
        return {"ok": [], "failed": []}

    monkeypatch.setattr("src.api.issues_center._attach_list", lambda o, r, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images", fake_collect, raising=False)
    import src.prompt_builder as pb
    pb._USER_IMAGE_CACHE.clear()
    parts = []
    pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#315", "vod", 0,
                                    evt=_base_evt(body=f"正文图 {U1}", note="评论图", a2a="前序摘要"))
    assert any(U1 in t for t in captured["texts"])
    assert any("评论图" in t for t in captured["texts"])
    assert any("前序摘要" in t for t in captured["texts"])


def test_inject_cache_second_call_no_recollect(monkeypatch):
    calls = {"n": 0}

    def fake_collect(owner, repo, n, texts, repo_key=""):
        calls["n"] += 1
        return {"ok": [], "failed": []}

    monkeypatch.setattr("src.api.issues_center._attach_list", lambda o, r, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images", fake_collect, raising=False)
    import src.prompt_builder as pb
    pb._USER_IMAGE_CACHE.clear()
    for _ in range(2):
        parts = []
        pb._inject_attachments_by_role(parts, "gitcode:grape-dev/grape", "#315", "vod", 0,
                                        evt=_base_evt(body=f"![a]({U1})"))
    assert calls["n"] == 1  # 第二次命中缓存


def test_token_never_in_prompt(monkeypatch):
    # token 只用于请求头，绝不进入注入文本
    collected = {"ok": [{"filename": "user-image-1.png", "url": "http://srv/x.png", "size": 4}],
                 "failed": [{"url": U2, "reason": "HTTP 403"}]}
    out = _inject(monkeypatch, "vod", collected=collected)
    assert "tk-fake" not in out
    assert "Bearer" not in out


def test_build_prompt_end_to_end(monkeypatch):
    # build_devops_agent_prompt 全链路：evt 携带 user-images 图 → 注入段出现
    monkeypatch.setattr("src.api.issues_center._attach_list", lambda o, r, n: [], raising=False)
    monkeypatch.setattr("src.api.issues_center._collect_issue_user_images",
                        lambda o, r, n, texts, repo_key="": {"ok": [{"filename": "user-image-1.png",
                                                        "url": "http://srv/x.png", "size": 4}],
                                                "failed": []}, raising=False)
    import src.prompt_builder as pb
    pb._USER_IMAGE_CACHE.clear()
    evt = _base_evt(body=f"在 issue 314 中附带的截图 ![img]({U1})")
    evt["payload"]["issue"]["title"] = "Agent 访问不了截图"
    prompt = pb.build_devops_agent_prompt(evt, [], "", ["vod"], agent_type="vod")
    assert "user-image-1.png: http://srv/x.png" in prompt
    assert "GitCode editor-uploaded images" in prompt
    assert "tk-fake" not in prompt
