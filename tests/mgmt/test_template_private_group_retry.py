"""一键创建：私有组不许建公开项目 → 私有仓库重试（2026-11-22）

真机缺陷：选到私有组织/组时，GitCode 直接拒绝
`Cannot create public project under private group`（一键创建整体 502）。
外包一层：仅在命中该语义时以 private=True 重试一次；其它错误照旧抛出。
"""
import pytest

from src.domains.mgmt.routes import templates as t


class _PrivateGroupAdapter:
    def __init__(self):
        self.calls = []

    def create_repo(self, token, name, description="", private=False, owner="", **kw):
        self.calls.append({"owner": owner, "private": private, "kw": kw})
        if not private:
            raise RuntimeError(
                'GitCode API POST failed (400): {"error_message":"Cannot create public project under private group"}')
        return {"full_name": f"{owner}/{name}"}


def test_private_group_error_retries_with_private():
    a = _PrivateGroupAdapter()
    t._create_repo_with_visibility_retry(a, "tok", "proj", {"description": "d"}, "org-a")
    assert len(a.calls) == 2, a.calls
    assert a.calls[0]["private"] is False and a.calls[0]["owner"] == "org-a"
    assert a.calls[1]["private"] is True and a.calls[1]["owner"] == "org-a"


def test_other_errors_are_not_retried():
    class _Boom:
        calls = 0

        def create_repo(self, *a, **k):
            _Boom.calls += 1
            raise RuntimeError("GitCode API POST failed (500): server error")

    with pytest.raises(RuntimeError):
        t._create_repo_with_visibility_retry(_Boom(), "tok", "proj", {}, "grp")
    assert _Boom.calls == 1, "非私有组错误不应重试"


def test_personal_space_never_retried():
    class _Boom:
        calls = 0

        def create_repo(self, *a, **k):
            _Boom.calls += 1
            raise RuntimeError("Cannot create public project under private group")

    with pytest.raises(RuntimeError):
        t._create_repo_with_visibility_retry(_Boom(), "tok", "proj", {}, "")
    assert _Boom.calls == 1, "个人空间不存在私有组语义，不应重试"

class _VisAdapter:
    def __init__(self):
        self.calls = []

    def create_repo(self, token, name, description="", private=False, owner="", **kw):
        self.calls.append(private)
        return {"full_name": f"{owner}/{name}"}


def test_private_flag_passes_through():
    """公开/私有可见性必须原样透传到平台建仓。"""
    a = _VisAdapter()
    t._create_repo_with_visibility_retry(a, "tok", "proj", {}, "grp", private=True)
    assert a.calls == [True]
    b = _VisAdapter()
    t._create_repo_with_visibility_retry(b, "tok", "proj", {}, "", private=False)
    assert b.calls == [False]
