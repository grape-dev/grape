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


def test_mine_supports_multi_status_filter():
    from src.domains.runtime.routes import issues as rt

    sig = inspect.signature(rt.my_issues)
    assert "statuses" in sig.parameters, "my_issues 缺多状态 statuses"
    src = inspect.getsource(rt.my_issues)
    assert "status IN (" in src, "多状态未下推为 IN 子句"


def test_frontend_mine_pushes_status_filter():
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "mineStatuses" in vue, "mine 分支未把状态过滤下推服务端"
    assert "'&statuses=' + encodeURIComponent(mineStatuses)" in vue
    # 状态过滤切换时 mine 也要重拉（不能只依赖本地过滤）
    assert "if (repoOf()) resetAndLoad()" in vue, (
        "toggleFilterStatus 仍只对 repo_all 重拉 → mine 分页后其余状态列显示为空")


def test_board_lazy_columns_cover_both_views():
    """看板按列懒加载：mine 与 repo_all 一视同仁（列游标 + 每列一页）。"""
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "taskView.value === 'board' && !!repoOf()" in vue, (
        "boardPagedView 仍只对 repo_all 生效 → mine 看板没有列级懒加载")
    # 列游标请求按 viewMode 分支（mine 打 /issues/mine）
    assert "'/issues/mine?status=&repo_key='" in vue
    # 每列一页 20 条
    assert "const SERVER_PAGE = 20" in vue


def test_board_first_render_is_progressive():
    """首屏渐进渲染：先到的列先上屏，不等全部列收敛（总数随各列响应自带）。"""
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "round.progressive = !tasks.value.length" in vue, (
        "回合创建时未标记首屏渐进 → 无法区分首屏/刷新")
    assert "if (round.progressive && items.length)" in vue, (
        "整刷回合没有首屏例外 → 最慢的列阻塞整个看板渲染（且首列之后到达的列仍被阻塞）")
