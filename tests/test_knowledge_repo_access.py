"""项目知识库仓库访问权限：嵌套 namespace + 托管组织成员兜底 + provider 前缀漂移。

线上形态（2026-11-17）：用户打开「项目知识库」两个链接恒 403
（`/api/knowledge`、`/api/knowledge/stats`，repo=`biz/jmbos/devops/ops-inspection`）。
该仓库在平台上正常托管（Issue/流水线都能用），但知识接口只认 `user_repos` 这一份
**易失缓存**：`save_user_repos()` 每次同步整表 DELETE 重写，且只继承
`is_join=1 AND is_admin=1` 的行 —— 缓存一漏同步，合法成员就直接 403。

本文件钉住三件事：
1. 嵌套 namespace（4 段）本身不是问题（解析/查询全链路支持）；
2. 仓库已托管 + 用户是托管组织成员 → 放行（不再被缓存缺口卡死）；
3. 未托管/非组织成员仍必须 403；同一 owner/repo 在别的 provider 前缀下命中也不误杀。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_knowledge_repo_access.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.deps import get_current_user_or_api_key  # noqa: E402
from src.api.main import app  # noqa: E402
from src.api.db_setup import init_database  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

init_database()

NESTED = "biz/jmbos/devops/ops-inspection"
KEY = f"gitcode:{NESTED}"
UID = 99701
ORG = 997001

_client = TestClient(app)


def _seed(*, user_repos_row=True, managed_row=True, org_member=True, prefix="gitcode",
          knowledge=True):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        for p in ("gitcode", "github", "gitlab"):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (f"{p}:{NESTED}",))
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key LIKE ?", (f"%{NESTED}",))
        c.execute("DELETE FROM org_member WHERE org_id=?", (ORG,))
        c.execute("DELETE FROM organization WHERE id=?", (ORG,))
        c.execute("INSERT INTO organization (id, name, slug, owner_id, is_personal) "
                  "VALUES (?,?,?,?,0)", (ORG, "k-access-org", "k-access-org", 1))
        if managed_row:
            c.execute("INSERT INTO managed_repos "
                      "(repo_key, repo_owner, repo_name, full_name, org_id) VALUES (?,?,?,?,?)",
                      (KEY, "biz", "jmbos/devops/ops-inspection", NESTED, ORG))
        if user_repos_row:
            c.execute("INSERT OR REPLACE INTO user_repos "
                      "(user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,?,?)",
                      (UID, f"{prefix}:{NESTED}", "biz", "jmbos/devops/ops-inspection", 1, 1))
        if org_member:
            c.execute("INSERT OR REPLACE INTO org_member (org_id, user_id, role) VALUES (?,?,?)",
                      (ORG, UID, "member"))
        if knowledge:
            c.execute("INSERT INTO agent_shared_knowledge "
                      "(repo_key, source, scope, file_path, name, description, status, archived) "
                      "VALUES (?,?,?,?,?,?,?,0)",
                      (f"{prefix}:{NESTED}", "system", "project",
                       "knowledge/graph.md", "项目结构图谱", "基线分析", "accepted"))
        db.commit()


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "k-tester", "auth_type": "jwt"}
    yield
    app.dependency_overrides.pop(get_current_user_or_api_key, None)
    with db_lock:
        db.cursor().execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        for p in ("gitcode", "github", "gitlab"):
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (f"{p}:{NESTED}",))
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE repo_key LIKE ?", (f"%{NESTED}",))
        db.cursor().execute("DELETE FROM org_member WHERE org_id=?", (ORG,))
        db.cursor().execute("DELETE FROM organization WHERE id=?", (ORG,))
        db.commit()


def _list(repo=NESTED, **params):
    return _client.get("/api/knowledge", params={"repo": repo, **params})


def _stats(repo=NESTED):
    return _client.get("/api/knowledge/stats", params={"repo": repo})


# ---------------------------------------------------------------- ① 原行为不变

def test_user_repos_member_allowed_nested_namespace():
    """嵌套 namespace + user_repos 命中 → 放行（回归：4 段路径本身不是问题）。"""
    _seed()
    assert _list(page=1, page_size=20, source="system").status_code == 200
    assert _stats().status_code == 200


# ---------------------------------------------------------------- ② 托管组织成员兜底

def test_hosted_org_member_allowed_without_user_repos_row():
    """仓库已托管 + 组织成员 → 放行（修复线上 403：user_repos 缓存漏同步不再卡死）。"""
    _seed(user_repos_row=False, managed_row=True, org_member=True)
    r = _list()
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert _stats().status_code == 200


def test_hosted_non_member_still_forbidden():
    """仅托管但非组织成员 → 仍 403（不得因为"托管了"就放开给所有人）。"""
    _seed(user_repos_row=False, managed_row=True, org_member=False)
    assert _list().status_code == 403
    assert _stats().status_code == 403


def test_unhosted_repo_forbidden():
    """未托管且不在 user_repos → 403。"""
    _seed(user_repos_row=False, managed_row=False, org_member=False, knowledge=False)
    assert _list().status_code == 403


# ---------------------------------------------------------------- ③ provider 前缀漂移

def test_prefix_drift_tolerated():
    """同一 owner/repo 在 gitlab: 前缀下命中 user_repos → 放行（不因前缀不一致误杀）。"""
    _seed(user_repos_row=True, managed_row=False, org_member=False, prefix="gitlab")
    assert _list().status_code == 200


def test_resolve_repo_key_probes_all_three_providers():
    """repo_key 解析要探测 gitcode/github/gitlab 三种前缀（历史实现漏了 gitlab）。"""
    from src.api.routes.knowledge import _resolve_repo_key
    with db_lock:
        c = db.cursor()
        for p in ("gitcode", "github", "gitlab"):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (f"{p}:{NESTED}",))
        c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        c.execute("INSERT OR REPLACE INTO user_repos "
                  "(user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,?,?)",
                  (UID, f"gitlab:{NESTED}", "biz", "jmbos/devops/ops-inspection", 1, 1))
        db.commit()
    assert _resolve_repo_key(NESTED) == f"gitlab:{NESTED}"


def test_resolve_repo_key_prefixed_input_passthrough():
    """已带前缀的输入原样归一，不做二次拼接。"""
    from src.api.routes.knowledge import _resolve_repo_key
    assert _resolve_repo_key(KEY) == KEY


# ---------------------------------------------------------------- ④ 可诊断

def test_403_reason_logged(caplog):
    """403 必须打出可诊断原因（哪个用户、多少可见仓库、组织 id），便于线上定位。"""
    import logging
    caplog.set_level(logging.WARNING, logger="devops-bot")
    _seed(user_repos_row=False, managed_row=True, org_member=False)
    assert _list().status_code == 403
    msgs = [r.getMessage() for r in caplog.records]
    assert any("knowledge access denied (403)" in m and "not a member" in m for m in msgs), msgs
