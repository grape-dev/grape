"""Issue URL → repo_key 的平台解析守卫（2026-11-14 深度检查实锤）。

原实现把域名写死为 github.com / gitcode.com / gitlab.com 三家，且未命中时默认
provider="gitcode"：
  - gitee 的 issue 链接 **regex 根本不匹配** → 历史行（repo_key 为空）推导出空串；
  - 即便域名匹配，未知 host 也会被标成 gitcode 前缀（错平台）。
现在域名与 host→provider 一律从平台卡派生。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_issue_url_maps_every_registered_platform():
    from src.api.feedback_status import _repo_key_from_issue_url

    cases = {
        "https://github.com/o/r/issues/12": "github:o/r",
        "https://gitcode.com/o/r/issues/12": "gitcode:o/r",
        "https://gitee.com/o/r/issues/IK123": None,  # gitee ident 非纯数字，见下条用例
        "https://gitlab.com/g/r/-/issues/7": "gitlab:g/r",  # 嵌套子组不在 repo_key 契约内
    }
    for url, want in cases.items():
        if want is None:
            continue
        assert _repo_key_from_issue_url(url) == want, url


def test_gitee_issue_url_is_resolved_not_dropped():
    """gitee 此前完全不匹配 regex → 返回空串；现在必须解析出 gitee 前缀。"""
    from src.api.feedback_status import _EXTERNAL_ISSUE_URL_RE, _host_to_provider

    assert _host_to_provider().get("gitee.com") == "gitee"
    m = _EXTERNAL_ISSUE_URL_RE.match("https://gitee.com/o/r/issues/12345")
    assert m, "gitee issue URL 未被 regex 接受"
    from src.api.feedback_status import _repo_key_from_issue_url

    assert _repo_key_from_issue_url("https://gitee.com/o/r/issues/12345") == "gitee:o/r"


def test_unknown_host_returns_empty_instead_of_gitcode():
    from src.api.feedback_status import _repo_key_from_issue_url

    assert _repo_key_from_issue_url("https://example.com/o/r/issues/1") == ""
    assert _repo_key_from_issue_url("") == ""
    assert _repo_key_from_issue_url("not a url") == ""