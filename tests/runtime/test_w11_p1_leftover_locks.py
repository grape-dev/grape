"""批次 4 锁：剩余 P1 平台硬编码四处（issues/mine 候选、通用 webhook 删除级联、
IM issue 链接、字符串 ident 兜底建档）。

背景（实查）：
- `/issues/mine` 只生成 gitcode/github 两候选 → gitee 仓 issue 列表恒空；
- `webhook/{provider}` 通用端点没有仓库删除级联（专用端点才有）→ 上游删仓后托管记录残留；
- IM 卡片 issue 链接按 github/gitlab/其余 gitcode 三分 → gitee 落 gitcode.com；
- `ensure_issue_task(rk, int(num))` 对 gitee 原生 ident（IK*）会 ValueError。
"""
import inspect
import os

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC_DIR, "data", "test_sessions.db"))
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from unittest.mock import patch


def _src(fn) -> str:
    return inspect.getsource(fn)


# ── ① /issues/mine：候选集卡目录派生 ──
def test_my_issues_uses_repo_key_candidates():
    from src.domains.runtime.routes import issues as issues_routes
    src = _src(issues_routes.my_issues)
    assert "repo_key_candidates" in src
    assert '"github:" + repo_key' not in src


def test_repo_key_candidates_covers_gitee():
    from src.kernel.repo_key import repo_key_candidates
    cands = repo_key_candidates("owner/repo")
    assert "gitee:owner/repo" in cands and "gitcode:owner/repo" in cands
    # 已带前缀输入同样能覆盖其他在册平台
    assert "gitee:owner/repo" in repo_key_candidates("gitcode:owner/repo")


# ── ② 通用 webhook 端点：仓库删除级联 ──
def test_generic_webhook_handles_repo_delete():
    from src.domains.runtime.routes import webhook as wh
    src = _src(wh.generic_vcs_webhook)
    assert "_is_repo_delete_event" in src
    assert "_handle_repo_deleted" in src


def test_repo_delete_event_detection():
    from src.domains.runtime.routes.webhook import _is_repo_delete_event
    assert _is_repo_delete_event("repo.destroyed", {})
    assert _is_repo_delete_event("repository", {"action": "deleted"})
    assert not _is_repo_delete_event("push", {"action": "opened"})


# ── ③ IM 卡片 issue 链接：平台卡派生 ──
def test_im_issue_url_follows_card_host():
    from src.domains.runtime.im.notify import issue_url
    assert issue_url("gitee:o/r", 7) == "https://gitee.com/o/r/issues/7"
    assert issue_url("gitcode:o/r", 7) == "https://gitcode.com/o/r/issues/7"
    assert issue_url("github:o/r", 7) == "https://github.com/o/r/issues/7"
    assert issue_url("gitlab:o/r", 7) == "https://gitlab.com/o/r/-/issues/7"
    assert issue_url("", 7) == ""
    assert issue_url("gitee:o/r", 0) == ""


# ── ④ 字符串 ident 兜底建档：不再 int() 强转 ──
def test_issue_vcs_fallback_keeps_string_ident():
    from src.domains.runtime.routes import issues as issues_routes
    src = _src(issues_routes)
    assert "ensure_issue_task(rk, int(num)" not in src, "兜底建档仍把 ident 强转 int（gitee IK* 会崩）"
    assert "ensure_issue_task(rk, num" in src