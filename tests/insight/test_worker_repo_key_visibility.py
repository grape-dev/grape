"""看板 Worker 卡片：仓库级（scope=repo）设备按候选 repo_key 可见。

线上 repo_key 存储形态是带平台前缀的 ``gitcode:owner/repo``，而 UI/URL 传给
``/ops/overview`` 的是裸 ``owner/repo``。旧实现直接拿裸键做等值比较，
导致所有仓库级 worker 在 Worker 卡片里永远不可见（设备管理页做了去前缀，
所以那边能看到同一台设备）。本文件锁住三条口径：

1. 选中该仓库时，仓库级设备出现在 worker 列表；
2. 选别的仓库时不出现（不越权串仓）；
3. 无仓库上下文（个人空间全局）时不出现——这是既有可见性口径，不是回退。
"""
import os
import sys

from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

CRED_ID = 9761
REPO_BARE = "selftest/dash-visibility"
REPO_KEY = "gitcode:" + REPO_BARE


def _user():
    return {"id": 1, "username": "admin", "org_id": 0, "role": "admin"}


def _clean():
    with db_lock:
        try:
            db.cursor().execute("DELETE FROM runtime_credentials WHERE id=?", (CRED_ID,))
            db.commit()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


def _seed_repo_worker():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (id, token_hash, name, device_name, scope, status, "
            "last_seen_at, last_ip, owner_name, repo_key, org_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (CRED_ID, "hash-visibility-test", "repo-visibility-worker", "repo-visibility-host",
             "repo", "active", datetime.now(timezone.utc).isoformat(), "10.0.0.7",
             "someone-else", REPO_KEY, ""))
        db.commit()


def _worker_ids(repo):
    from src.domains.insight.routes.dashboard import get_ops_overview
    resp = get_ops_overview(repo=repo, user=_user())
    return [w.get("id") for w in resp["workers"]["list"]]


def test_repo_scoped_worker_visible_when_repo_selected():
    _seed_repo_worker()
    assert CRED_ID in _worker_ids(REPO_BARE), \
        "仓库级设备（repo_key 带平台前缀）在选中该仓库时必须出现在 Worker 卡片"


def test_repo_scoped_worker_not_visible_for_other_repo():
    _seed_repo_worker()
    assert CRED_ID not in _worker_ids("selftest/some-other-repo")


def test_repo_scoped_worker_not_visible_without_repo_context():
    _seed_repo_worker()
    assert CRED_ID not in _worker_ids("")