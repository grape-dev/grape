# -*- coding: utf-8 -*-
"""「我的需求」接口服务端分页回归（需求量大账号 4MB/3.6s）。

契约：
1. my_issues 接受 per_page/page，分页模式下响应带 total/page/per_page/has_more；
2. per_page=0（默认）保持全量（旧调用方零破坏）；
3. 前端 IssuesView 的 mine 分支带分页参数且 usePaging 不再排除 mine。
"""
import inspect


def test_mine_endpoint_accepts_paging():
    from src.domains.runtime.routes import issues as rt

    sig = inspect.signature(rt.my_issues)
    assert "per_page" in sig.parameters, "my_issues 缺 per_page"
    assert "page" in sig.parameters, "my_issues 缺 page"


def test_mine_paging_metadata_in_response():
    from src.domains.runtime.routes import issues as rt

    src = inspect.getsource(rt.my_issues)
    assert '"has_more"' in src, "分页模式未回传 has_more"
    assert '"total"' in src, "分页模式未回传 total"


def test_frontend_mine_branch_is_paged():
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "'/issues/mine?status=&repo_key='" in vue
    # mine URL 必须带 per_page + page
    assert "'&mode=time&per_page='" in vue, "mine 分支未带 per_page"
    # 分页处理对两种视图都启用；URL 按 viewMode 分支（repo_all → 仓库接口，否则 mine）
    assert "const usePaging = true" in vue, "usePaging 未对 mine 启用（全量拉取 → 4MB/3.6s）"
    assert "const repoAll = viewMode.value === 'repo_all' && !!repo" in vue, (
        "URL 分支未按 viewMode 区分（mine 会被打到仓库列表接口）")
