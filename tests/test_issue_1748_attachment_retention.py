"""Issue #1748 补充：自动转存附件的回收判定 + #315 注入缓存按评论隔离。

背景（主人验收 #1748 时提出的疑虑，实测确认两条）：
1. 清理链条是死代码：_cleanup_issue_attachments 全仓无调用点，附件只增不减。
2. #315 的注入缓存键是 (repo_key, issue_num)，同 issue 第二条评论会命中上一条缓存
   → 新评论的图被静默漏采。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import src.api.issues_center as ic
import src.prompt_builder as pb

REPO_KEY = "gitcode:acme-org/demo"


def test_auto_transferred_name_matrix():
    """只认平台自动转存的文件名形态；用户手工命名不误伤。"""
    assert ic._is_auto_transferred_attachment("user-image-1.png")
    assert ic._is_auto_transferred_attachment("user-attachment-3.mp4")
    # #1748：固定前缀 + UUID（MR !1145 Informational-2：不再靠 32hex 形态启发式）
    assert ic._is_auto_transferred_attachment(
        "user-attachment-c311ec5f5d4744448b0fc6bc77a59559.mp4")
    # 用户手工上传：没有平台前缀一律保留（含恰好 32 位十六进制 + 媒体扩展名的名字）
    assert not ic._is_auto_transferred_attachment("设计评审截图.png")
    assert not ic._is_auto_transferred_attachment("report.pdf")
    assert not ic._is_auto_transferred_attachment("c311ec5f5d4744448b0fc6bc77a59559.mp4")
    assert not ic._is_auto_transferred_attachment("c311ec5f5d4744448b0fc6bc77a59559.zip")
    assert not ic._is_auto_transferred_attachment("")
    assert not ic._is_auto_transferred_attachment("abc123.mp4")


def test_cleanup_removes_only_auto_transferred_local(tmp_path, monkeypatch):
    """本地存储：只删自动转存副本，用户手工附件留存。"""
    issue_dir = tmp_path / "issue_attachments" / "acme-org" / "demo" / "7"
    issue_dir.mkdir(parents=True)
    for name in ("user-image-1.png", "user-image-2.jpg",
                 "user-attachment-c311ec5f5d4744448b0fc6bc77a59559.mp4",
                 "设计评审截图.png", "requirements.docx"):
        (issue_dir / name).write_bytes(b"x")

    monkeypatch.setattr(ic, "_attach_base_dir", lambda o, r, n: str(issue_dir))
    monkeypatch.setattr("src.storage.get_storage", lambda: None)

    removed = ic._cleanup_auto_transferred_attachments(REPO_KEY, 7)

    assert removed == 3
    left = sorted(os.listdir(issue_dir))
    assert left == ["requirements.docx", "设计评审截图.png"]


def test_cleanup_swallows_errors(monkeypatch):
    """清理失败不能影响关闭流程。"""
    def boom(*_a, **_k):
        raise RuntimeError("obs down")
    monkeypatch.setattr("src.storage.get_storage", boom)
    assert ic._cleanup_auto_transferred_attachments(REPO_KEY, 7) == 0


def test_confirm_done_invokes_cleanup(monkeypatch):
    """issue 确认完成 → 触达回收（而不是像旧死代码那样无人调用）。"""
    calls = []
    monkeypatch.setattr(ic, "_cleanup_auto_transferred_attachments",
                        lambda repo_key, num: calls.append((repo_key, num)) or 0)
    ic.confirm_issue_done(REPO_KEY, 7)
    ic.cancel_issue_task(REPO_KEY, 7)
    assert calls == [(REPO_KEY, 7), (REPO_KEY, 7)]


def test_315_cache_isolated_per_comment(monkeypatch):
    """同一 issue 的两条不同评论各采一次；同一条评论重复调用仍只采一次。"""
    seen = []

    def fake_collect(owner, repo, issue_number, texts, repo_key=""):
        seen.append(list(texts))
        idx = len(seen)
        return {"ok": [{"filename": f"user-image-{idx}.png",
                        "url": f"/static/x/user-image-{idx}.png", "size": 1}],
                "failed": []}

    monkeypatch.setattr(ic, "_collect_issue_user_images", fake_collect)
    pb._USER_IMAGE_CACHE.clear()

    def evt(note):
        return {"type": "issue_comment",
                "payload": {"object_attributes": {"note": note}}}

    a = pb._collect_user_images_for_injection(REPO_KEY, "acme-org", "demo", "7", "coding",
                                              evt("第一张 https://raw.gitcode.com/user-images/a.png"))
    b = pb._collect_user_images_for_injection(REPO_KEY, "acme-org", "demo", "7", "coding",
                                              evt("第二张 https://raw.gitcode.com/user-images/b.png"))
    c = pb._collect_user_images_for_injection(REPO_KEY, "acme-org", "demo", "7", "coding",
                                              evt("第二张 https://raw.gitcode.com/user-images/b.png"))

    assert len(seen) == 2, "第二条评论必须重新采集，不能命中上一条缓存"
    assert "user-image-1.png" in a
    assert "user-image-2.png" in b
    assert "user-image-1.png" not in b, "第二次注入不能是上一条评论的旧图"
    assert c == b, "同一条评论重复调用应命中缓存"