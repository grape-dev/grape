# -*- coding: utf-8 -*-
"""建单路径不得在事件循环上做同步出站调用（压测下 UI 卡顿根因）。

现象：压测并发建单时，py-spy 抓到事件循环线程停在
`VcsApiClient._do_request -> urllib.request.urlopen -> find_open_issue_by_title`，
单次 15~20s；期间所有 UI 请求（含带登录态的 /api/health）一起排队。

契约：`create_task_via_issue`（async）里的 GitCode 出站调用必须走
`asyncio.to_thread(...)`，不得直接同步调用。
"""
import inspect


def _src() -> str:
    from src.domains.runtime import issues_center as ic

    return inspect.getsource(ic.create_task_via_issue)


def test_dedup_lookup_is_offloaded():
    src = _src()
    # 允许出现在 to_thread 实参里，但不允许作为语句直接调用
    lines = [ln.strip() for ln in src.splitlines()]
    direct = [ln for ln in lines
              if ln.startswith("issue_num = _find_open_issue_by_title(")]
    assert not direct, f"查重调用仍是同步直调: {direct}"
    assert "asyncio.to_thread(" in src
    assert "_find_open_issue_by_title" in src


def test_create_issue_is_offloaded():
    src = _src()
    lines = [ln.strip() for ln in src.splitlines()]
    direct = [ln for ln in lines if ln.startswith("resp = _create_issue_unified(")]
    assert not direct, f"建单调用仍是同步直调: {direct}"
    assert "_create_issue_unified" in src


def test_no_bare_urlopen_in_async_issue_path():
    from src.domains.runtime import issues_center as ic

    src = inspect.getsource(ic)
    assert "urllib.request.urlopen(" not in src, (
        "issues_center 内不应再有同步 urlopen（出站应经 VcsApiClient 且在线程池执行）")


def test_clarify_issue_offloads_outbound():
    """clarify_issue 的两路同步出站（GitHub / GitCode）必须走 to_thread。"""
    import inspect

    from src.domains.runtime.routes import issues as rt

    src = inspect.getsource(rt.clarify_issue)
    direct_gh = [ln.strip() for ln in src.splitlines()
                 if ln.strip().startswith("_gh_resp = _github_req(")]
    assert not direct_gh, f"GitHub 出站仍直调: {direct_gh}"
    direct_gc = [ln.strip() for ln in src.splitlines()
                 if ln.strip().startswith("_resp = _client.post_issue_comment(")]
    assert not direct_gc, f"GitCode 出站仍直调: {direct_gc}"
    assert src.count("asyncio.to_thread(") >= 2
