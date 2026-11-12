"""S97 用例（D72，先测后改）：confirm-done 关单链多平台重构——仓库能力抽象。

行为约束：
- gitcode/github 两条既有路径逐字节保持（token 入口、降级链、返回值形态）；
- 结果字典 legacy 双 key（gitcode/github）冻结，新平台按实际候选增量出现；
- 镜像查找泛化：managed_repos 同 owner/repo 全平台行（repo_key_candidates
  卡派生），替代 github-only 单键查找；
- 降级不阻断语义、bot_token_failed 提示、权限校验（owner/admin 403）不动。
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """独立 sqlite 库，避免共享库行级污染。"""
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("CREATE TABLE managed_repos (repo_key TEXT PRIMARY KEY, full_name TEXT)")
    monkeypatch.setattr("src.kernel.session_manager.db", conn)
    monkeypatch.setattr("src.kernel.session_manager.db_lock", __import__("threading").Lock())
    yield conn
    conn.close()


def test_s97_guard_no_is_github_in_close_chain():
    """结构守卫：关单链零 is_github 分叉、零 github-only 镜像字面。"""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "src" / "domains" / "runtime" / "routes" / "issues.py").read_text()
    chain = src[src.index("def _close_vcs_issues"):src.index("def _try_close_issue_for_repo")]
    assert "is_github" not in chain
    fn = src[src.index("def _try_close_issue_for_repo"):]
    fn = fn[:fn.index("\n@router")]
    import re as _re
    assert not _re.search(r'\bis_github\b\s*(=|in\b|:)', fn) and "if is_github" not in fn \
        and "is_github=" not in fn, "函数体不得使用 is_github 分叉（docstring 说明除外）"
    assert "_find_github_mirror_repo_key" not in __import__("pathlib").Path(__file__).parent.parent.joinpath("src/api/issues_center.py").read_text(), \
        "github-only 镜像薄包装必须已删除（G3 收口：镜像语义全平台化）"


def test_s97_result_dict_legacy_keys_frozen():
    """legacy 双 key 冻结：无 issue_number 早退仍含 gitcode/github+message。"""
    from src.domains.runtime.routes.issues import _close_vcs_issues
    r = _close_vcs_issues("gitee:o/r", None, {"id": 1})
    assert r["gitcode"] == "skipped" and r["github"] == "skipped"
    assert "message" in r


@pytest.mark.xfail(reason="S97 全平台镜像外关口径被用户裁定否决（2026-11-10）：镜像外关仅「非 github 主仓→其 github 孪生」单向（#656 旧口径，见 _close_vcs_issues 内注释）；本测试编码的 gitcode 等非 github 镜像外关不再执行", strict=True)
def test_s97_primary_plus_mirror_all_platforms():
    """gitee 主仓 + github/gitcode 镜像：三平台逐一尝试，键按候选增量生成。"""
    import threading
    from src.domains.runtime.routes import issues as I
    conn = None
    # 造绑定：gitee 主仓 + github + gitcode 镜像
    import sqlite3
    # 通过 monkeypatch 后的 db 拿连接不便，这里直接 patch _mirror_repo_keys
    with patch.object(I, "_native_issue_number", side_effect=lambda n: int(n)), \
         patch("src.api.issues_center._mirror_repo_keys",
               return_value=["github:o/r", "gitcode:o/r"]), \
         patch.object(I, "_try_close_issue_for_repo",
                      side_effect=lambda rk, n, u: f"closed:{rk}") as m:
        r = I._close_vcs_issues("gitee:o/r", 42, {"id": 7})
    assert r["gitee"] == "closed:gitee:o/r"
    assert r["github"] == "closed:github:o/r"
    assert r["gitcode"] == "closed:gitcode:o/r"
    # legacy key 也被镜像/主平台真实结果覆盖（gitcode/github 有绑定即尝试）
    assert set(m.call_args_list[0][0][:1]) == {"gitee:o/r"}


def test_s97_mirror_finder_generalized():
    """镜像查找：同 owner/repo 的全部平台行（除自身），github 语义保持薄包装。"""
    import threading
    import src.kernel.session_manager as SM
    import src.api.issues_center as IC
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE managed_repos (repo_key TEXT PRIMARY KEY)")
    for rk in ("gitcode:o/r", "github:o/r", "gitee:o/r", "gitlab:o/r", "gitcode:other/x"):
        conn.execute("INSERT INTO managed_repos VALUES (?)", (rk,))
    monkey_db = conn
    with patch.object(SM, "db", monkey_db), patch.object(SM, "db_lock", threading.Lock()):
        mirrors = IC._mirror_repo_keys("gitcode:o/r")
    assert set(mirrors) == {"github:o/r", "gitee:o/r", "gitlab:o/r"}
    assert "gitcode:other/x" not in mirrors and "gitcode:o/r" not in mirrors
    # S108：github-only 薄包装已删除——github 镜像语义由 _mirror_repo_keys 全集覆盖
    with patch.object(SM, "db", monkey_db), patch.object(SM, "db_lock", threading.Lock()):
        assert "github:o/r" in IC._mirror_repo_keys("gitcode:o/r")


def test_s97_bot_token_dispatch_per_provider():
    """token 分派：github 走 get_github_token（D32 兜底保持），gitee 走 get_repo_bot_token。"""
    from src.domains.runtime.routes.issues import _try_close_issue_for_repo
    with patch("src.domains.mgmt.user.db.get_github_token", return_value="GH_TOK") as gh, \
         patch("src.domains.mgmt.user.db.get_repo_bot_token", return_value="") as rbt:
        # github：命中 get_github_token，VcsApiClient 成功关闭
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as VC:
            inst = VC.return_value
            inst.get_issue_state.return_value = "open"
            inst.close_issue.return_value = {"number": 5}
            r = _try_close_issue_for_repo("github:o/r", 5, {"id": 1})
        assert r == "success"
        gh.assert_called_once()
        rbt.assert_not_called()
    with patch("src.domains.mgmt.user.db.get_github_token", return_value="") as gh, \
         patch("src.domains.mgmt.user.db.get_repo_bot_token", return_value="GT_TOK") as rbt:
        with patch("src.domains.runtime.vcs.api_client.VcsApiClient") as VC:
            inst = VC.return_value
            inst.get_issue_state.return_value = "open"
            inst.close_issue.return_value = {"number": 5}
            r = _try_close_issue_for_repo("gitee:o/r", 5, {"id": 1})
        assert r == "success"
        rbt.assert_called_once()
        gh.assert_not_called()
