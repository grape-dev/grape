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


def test_board_scroll_advances_cursor_page():
    """滚到底必须拉下一页：游标推进覆盖整刷/渐进（只认 merge=false 会漏 round 分支）。"""
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "if (!merge || round) cur.page = page + 1" in vue, (
        "游标推进漏了 round 分支 → 渐进首刷后滚到底永远重拉 page=1，列内翻页失效")


def test_board_round_must_be_shallow_ref():
    """boardRound 必须 shallowRef：ref 深度 reactive 化会让 round !== boardRound.value
    恒成立 → commitBoardRound 永早退 → boardRound 永不清空 → 列内翻页(滚动/点击)全被拦。
    实测（Vue vendor 全局验证）：ref.value = raw 后 identity=false、isReactive=true。"""
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "const boardRound = shallowRef(null)" in vue, (
        "boardRound 仍用 ref() → #2649 粘合层身份比较恒 false → 看板列翻页死锁")
    assert "shallowRef" in vue.split("from 'vue'")[0], (
        "import 未引入 shallowRef（M2：此前是恒真断言，无守卫力）")


def test_board_foot_loadmore_is_clickable():
    """列底「加载更多」span 必须可点击（此前无 @click，用户点击无反应）。"""
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert 'class="isk-loadmore-btn"' in vue, "列底加载更多缺点击处理器"
    assert '@click="boardFetch(BOARD_CURSOR_OF_COL[col.k])"' in vue
    # D1 回归：title 必须是绑定（:title），不得是 mustache 字面量
    assert ":title=\"t('load_more')\"" in vue, (
        "title 写成 mustache 插值 → 编译为字面量 '{{ t(...) }}'")
    assert "title=\"{{ t('load_more') }}\"" not in vue


def test_create_task_precheck_keeps_explicit_semantics():
    """F1/A14 回归：建单预检必须保留 explicit 语义（回落场景只告警不拦）。"""
    from pathlib import Path

    src = Path("src/domains/runtime/issues_center.py").read_text(encoding="utf-8")
    assert "_scene_is_explicit = bool(_scene_for_precheck)" in src, (
        "explicit 语义被删 → 回落场景(仓库 dev_scene/内置 default)从告警放行变 400 拦单，"
        "用户无修复入口")
    assert "explicit=_scene_is_explicit" in src


def test_routes_keep_attach_and_repo_list_caches():
    """G1 回归：附件批量缓存与列表 TTL 缓存不得再被删。"""
    from pathlib import Path

    src = Path("src/domains/runtime/routes/issues.py").read_text(encoding="utf-8")
    assert "_ATTACH_CACHE" in src, "附件批量缓存被删 → 看板 6 并发各自 OBS 枚举 3.2s"
    assert "_cached_repo_list" in src, "列表 TTL 缓存被删 → db_lock 串行叠加顶穿 8s 看门狗"
    assert "@_cached_repo_list(5.0)" in src


def test_watchdog_survives_board_changes():
    """N3 回归：静默 abort 看门狗不得被看板改造挤掉。"""
    from pathlib import Path

    vue = Path("src/api/frontend/app/src/views/IssuesView.vue").read_text(
        encoding="utf-8")
    assert "function armLoadWatchdog()" in vue, "armLoadWatchdog 被删 → 静默 abort 永久加载态"
    assert "function disarmLoadWatchdog()" in vue
    assert "_silentAbortRetried" in vue


def test_mine_issues_accepts_creators_filter():
    """N2 回归：my_issues 必须消费 creators（此前静默丢弃）。"""
    from pathlib import Path

    src = Path("src/domains/runtime/routes/issues.py").read_text(encoding="utf-8")
    assert 'creators: str = Query("", description="按创建人过滤' in src, (
        "my_issues 缺 creators 参数 → 前端 mine 分支的创建人过滤被服务端丢弃")
    assert "_build_creator_filter(creators, clauses, params)" in src
