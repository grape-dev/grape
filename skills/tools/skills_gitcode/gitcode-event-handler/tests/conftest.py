"""pytest 共享 fixture: 加载 gitcode_event_handler 模块, 隔离外部依赖。

所有测试使用 mock 隔离 GitCode API / Multica CLI / DB, 不产生真实副作用。
"""
import sys
import os
from unittest import mock

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SRC))

import gitcode_event_handler as h  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_external():
    """自动隔离所有外部依赖(GitCode API / Multica / DB / 缓存)。"""
    with mock.patch.object(h, "gitcode_get", return_value=[]), \
         mock.patch.object(h, "gitcode_post", return_value={"id": 1}), \
         mock.patch.object(h, "gitcode_patch", return_value={"state": "x"}), \
         mock.patch.object(h, "gitcode_delete", return_value={}), \
         mock.patch.object(h, "_multica_cli", return_value=(1, "", "mock")), \
         mock.patch.object(h, "multica_issue_create", return_value=("m-1", "SKI-1")), \
         mock.patch.object(h, "multica_issue_comment", return_value=1), \
         mock.patch.object(h, "multica_issue_assign", return_value=True), \
         mock.patch.object(h, "multica_issue_get", return_value=(False, {})), \
         mock.patch.object(h, "db_connect"), \
         mock.patch.object(h, "db_insert_requirement", return_value=999), \
         mock.patch.object(h, "db_insert_flow", return_value=None), \
         mock.patch.object(h, "db_update_requirement", return_value=None), \
         mock.patch.object(h, "db_update_flow", return_value=None), \
         mock.patch.object(h, "db_find_requirement_by_issue_id", return_value=None), \
         mock.patch.object(h, "db_find_requirement_by_pr_id", return_value=None), \
         mock.patch.object(h, "db_find_requirement_by_issue_number", return_value=None):
        h._COMMENTS_CACHE.clear()
        h._REPO_PRODUCTS_CACHE.clear()
        yield
        h._COMMENTS_CACHE.clear()
        h._REPO_PRODUCTS_CACHE.clear()


def make_issue_payload(repo, number, title, body, issue_id=None, author="lccode",
                       state="open", created="2026-08-09T10:00:00+08:00"):
    """构造 GitLab 风格 issue payload。"""
    return {
        "object_kind": "issue",
        "object_attributes": {
            "id": issue_id or (10000 + number),
            "iid": number,
            "title": title,
            "description": body,
            "state": state,
            "url": f"https://gitcode.com/{repo}/issues/{number}",
            "author": {"username": author, "name": author},
            "created_at": created,
            "updated_at": created,
        },
        "project": {"path_with_namespace": repo},
    }
