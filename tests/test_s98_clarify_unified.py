"""S98 用例（D73，先测后改）：澄清评论发送统一——关单链同族收口（仓库能力抽象）。

原状：clarify_issue 内 is_github 二元分叉——github 走 _github_req 裸调
+ 手工台账，其余平台走 VcsApiClient（S75 已统一）。本批收口为全平台
统一卡客户端路径。行为约束：token 入口按 provider（github=get_github_token
含 D32 兜底；其余=get_repo_bot_token rc 权威）；#453 台账、#345 401 提示、
TOKEN_NOT_BOUND error_code、会话写入/awaiting_input 恢复不动。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _src():
    from pathlib import Path
    return (Path(__file__).parent.parent / "src" / "domains" / "runtime" / "routes" / "issues.py").read_text()


def test_s98_guard_clarify_no_dual_branch():
    """结构守卫：clarify 链零 is_github 分叉、零 _github_req 裸调、零硬编码 URL。"""
    src = _src()
    start = src.index("async def clarify_issue")
    end = src.index("\n@router", start)
    fn = src[start:end]
    assert "_is_github" not in fn
    assert "_github_req" not in fn
    assert "api.github.com" not in fn
    assert "api.github.com" not in fn
    # TOKEN_NOT_BOUND（#372 T2-16 审计锚）保持存在
    assert "TOKEN_NOT_BOUND" in fn


def test_s98_guard_repo_routes_github_req_gone():
    """issues.py 全文件零 _github_req 依赖（裸调族清零）。"""
    src = _src()
    assert "_github_req" not in src


def test_s98_clarify_posts_via_card_client():
    """行为：gitee/gitcode/github 全部经 VcsApiClient.post_issue_comment 发送。"""
    from unittest.mock import patch, MagicMock
    from src.domains.runtime.routes.issues import clarify_issue
    import asyncio

    async def _run(repo_key):
        with patch("src.api.issues_center._get_task_issue_info",
                   return_value=(repo_key, 5, "conv1")), \
             patch("src.domains.mgmt.user.db.get_github_token", return_value="GH_TOK"), \
             patch("src.domains.mgmt.user.db.get_repo_bot_token", return_value="RBT_TOK"), \
             patch("src.domains.runtime.vcs.api_client.VcsApiClient") as VC, \
             patch("src.kernel.session_manager.add_message"), \
             patch("src.api.issues_center._resume_issue_waiting", return_value=False):
            VC.return_value.post_issue_comment.return_value = {"id": 1}
            r = await clarify_issue(9, type("B", (), {"message": "hello"})(),
                                    user={"id": 1, "username": "u"})
            return r, VC

    r, VC = asyncio.run(_run("gitee:o/r"))
    assert r["ok"]
    VC.assert_called_once_with("gitee", "RBT_TOK", "o", "r")
    VC.return_value.post_issue_comment.assert_called_once()

    r, VC = asyncio.run(_run("github:o/r"))
    assert r["ok"]
    VC.assert_called_once_with("github", "GH_TOK", "o", "r")
    VC.return_value.post_issue_comment.assert_called_once()


def test_s98_clarify_no_token_bound_error():
    """无 token：400 + error_code=TOKEN_NOT_BOUND（#372 T2-16 语义全平台保持）。"""
    from unittest.mock import patch
    from src.domains.runtime.routes.issues import clarify_issue
    from fastapi import HTTPException
    import asyncio

    async def _run(repo_key):
        with patch("src.api.issues_center._get_task_issue_info",
                   return_value=(repo_key, 5, "")), \
             patch("src.domains.mgmt.user.db.get_github_token", return_value=""), \
             patch("src.domains.mgmt.user.db.get_repo_bot_token", return_value=""):
            return await clarify_issue(9, type("B", (), {"message": "hi"})(),
                                       user={"id": 1, "username": "u"})

    for rk in ("gitee:o/r", "github:o/r", "gitcode:o/r"):
        try:
            asyncio.run(_run(rk))
            raise AssertionError(f"{rk} 应抛 TOKEN_NOT_BOUND")
        except HTTPException as e:
            assert e.status_code == 400
            assert e.detail.get("error_code") == "TOKEN_NOT_BOUND"
