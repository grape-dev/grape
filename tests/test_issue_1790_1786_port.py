"""Issue #1790/#1786（#1790/#1786 移植）测试。

#1790：skills list 三态 repo 参数校验（bare → 400 格式提示）。
#1786：pulls files 顶层 diff 别名投影 + diff_unavailable_reason 枚举。
Port from weekly-1102-zrr tests/test_issue1790_skills_repo_param.py 与
tests/test_git_repo_api.py 的 #1786 系函数，适配 arch11 domains 路径。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.kernel.session_manager import db, db_lock

UID = "u1790"
OWNER = "o1790"
REPO = "r1790"


BOT = "bot_tok_1790"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_github_token", lambda *a, **k: BOT)


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "git_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    yield TestClient(app)
    app.dependency_overrides.clear()


def _get(_client, url):
    """GET 帮助函数（weekly 测试基建同款：返回 Response；断言经 r.json() 访问）。"""
    resp = _client.get(url)
    assert resp.status_code == 200, resp.text
    return resp


def test_pulls_files_top_level_diff_alias(monkeypatch, _client):
    """Issue #1786：顶层 diff 别名与 .patch.diff 严格一致（普通修改/新增文件）。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    upstream = [
        # 普通修改：patch 对象已就位
        {"filename": "src/a.py", "status": "modified", "additions": 1, "deletions": 1,
         "patch": {"diff": "@@ -1 +1 @@\n+a", "added_lines": 1, "removed_lines": 1}},
        # 新增文件：平台差异形态——顶层 .diff（#731 归一化并入 .patch.diff 后别名同步投影）
        {"filename": "new_file.py", "status": "added", "additions": 2, "deletions": 0,
         "diff": "@@ -0,0 +1,2 @@\n+b1\n+b2"},
    ]

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req",
                        lambda m, u, token="", body=None: [dict(f) for f in upstream])
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200
    # Issue #1941 信封化：文件数组在 files 字段下（与 test_pulls_files_diff_normalization 同口径）
    _rj = r.json()
    files = _rj["files"] if isinstance(_rj, dict) else _rj  # arch11 裸数组兼容

    # 顶层 diff 与 .patch.diff 严格相等（消费者按 .diff 或 .patch.diff 取值均可）
    assert files[0]["diff"] == "@@ -1 +1 @@\n+a"
    assert files[0]["diff"] == files[0]["patch"]["diff"]
    assert files[1]["diff"] == files[1]["patch"]["diff"] == "@@ -0,0 +1,2 @@\n+b1\n+b2"

    # .patch 对象形态/元信息不变（.patch.diff 取值方零回归）
    assert files[0]["patch"]["added_lines"] == 1
    assert files[0]["patch"]["removed_lines"] == 1

    # 有非空 diff ⇒ 不出现 diff_unavailable_reason（互斥）
    for f in files:
        assert "diff_unavailable_reason" not in f


def test_pulls_files_diff_unavailable_reason(monkeypatch, _client):
    """Issue #1786：补丁不可用时 diff_unavailable_reason 枚举化（二进制/超大/空文件/上游缺失）。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    upstream = [
        # 超大文件：上游标记 too_large（无 diff 正文）
        {"filename": "big.txt", "status": "modified", "additions": 0, "deletions": 0,
         "patch": {"too_large": True}},
        # 二进制：无 patch + 二进制扩展名
        {"filename": "assets/logo.png", "status": "modified", "additions": 0, "deletions": 0},
        # 空文件：新增零行且无补丁（零字节）
        {"filename": "empty.txt", "status": "added", "additions": 0, "deletions": 0},
        # 上游 patch 缺失：文本文件、无任何标记
        {"filename": "notes.txt", "status": "modified", "additions": 3, "deletions": 1},
        # 对照组：正常文本修改（有 diff）
        {"filename": "src/a.py", "status": "modified",
         "patch": {"diff": "@@ -1 +1 @@\n+a"}},
    ]

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req",
                        lambda m, u, token="", body=None: [dict(f) for f in upstream])
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200
    _rj = r.json()
    files = _rj["files"] if isinstance(_rj, dict) else _rj  # arch11 裸数组兼容

    reasons = {f["filename"]: f.get("diff_unavailable_reason") for f in files}
    assert reasons["big.txt"] == "too_large"
    assert reasons["assets/logo.png"] == "binary"
    assert reasons["empty.txt"] == "empty_file"
    assert reasons["notes.txt"] == "upstream_missing"
    assert reasons["src/a.py"] is None

    # 互斥不变量：有非空 diff ⇒ 无 reason；无 diff ⇒ 必有 reason 且落枚举（绝不静默空串）
    enum_ok = {"too_large", "binary", "empty_file", "upstream_missing"}
    for f in files:
        if f["diff"]:
            assert "diff_unavailable_reason" not in f
        else:
            assert f["diff"] == ""
            assert f["diff_unavailable_reason"] in enum_ok


def test_pulls_files_empty_file_requires_present_zero_counts(monkeypatch, _client):
    """Issue #1786 QA 收敛：行数字段缺失 ≠ 零行——empty_file 仅在 additions/deletions 均在位且为零时判定。

    Spec 风险缓解「未知情况宁可不给具体 reason 也不误标」：status=added 但行数字段缺省
    （上游未返回该字段）时不得推断为空文件，回落事实枚举 upstream_missing（上游未返回可用补丁）。
    """
    base = f"/api/git-repo/{OWNER}/{REPO}"
    upstream = [
        # 边界：status=added 但行数字段缺失（≠ 零行，长度未知）——不误标 empty_file
        {"filename": "unknown_counts.txt", "status": "added"},
        # 边界：仅 additions 在位为零、deletions 缺失——仍有未知项，不误标
        {"filename": "half_known.txt", "status": "added", "additions": 0},
        # 对照组：行数字段均在位且为零——真零字节/零行变更，仍判 empty_file
        {"filename": "really_empty.txt", "status": "added", "additions": 0, "deletions": 0},
        # 对照组：上游显式 reason 仍最优先（不受收紧影响）
        {"filename": "reason_marked.py", "status": "added",
         "patch": {"diff": "", "reason": "unchanged: no diff between PR base and head"}},
    ]

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req",
                        lambda m, u, token="", body=None: [dict(f) for f in upstream])
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200
    _rj = r.json()
    files = _rj["files"] if isinstance(_rj, dict) else _rj  # arch11 裸数组兼容

    reasons = {f["filename"]: f.get("diff_unavailable_reason") for f in files}
    assert reasons["unknown_counts.txt"] == "upstream_missing"
    assert reasons["half_known.txt"] == "upstream_missing"
    assert reasons["really_empty.txt"] == "empty_file"
    assert reasons["reason_marked.py"] == "empty_file"

    # 无 diff ⇒ 必有 reason 且落枚举（互斥不变量在收紧后仍严格成立）
    for f in files:
        assert f["diff"] == ""
        assert f["diff_unavailable_reason"] in {"too_large", "binary", "empty_file", "upstream_missing"}


def test_pulls_files_reason_reuses_patch_reason(monkeypatch, _client):
    """Issue #1786：diff_unavailable_reason 复用 #1765 patch.reason 同一来源（前缀映射）+ .patch 零回归。"""
    base = f"/api/git-repo/{OWNER}/{REPO}"
    upstream = [
        # #1765 PR #1111 形态：patch 对象带 reason（diff 为空）
        {"filename": "big.txt", "status": "modified",
         "patch": {"diff": "", "reason": "too_large: diff omitted by provider (file exceeds diff size limit)"}},
        {"filename": "plain.txt", "status": "modified",
         "patch": {"diff": "", "reason": "no_diff: provider returned no usable diff for this file"}},
        {"filename": "stale.py", "status": "unchanged", "additions": 0, "deletions": 0,
         "patch": {"diff": "", "reason": "unchanged: no diff between PR base and head"}},
        # 旧式 string patch：#731 收敛为对象后顶层别名同样成立
        {"filename": "legacy.py", "status": "modified", "patch": "@@ -1 +1 @@\n+z"},
    ]

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req",
                        lambda m, u, token="", body=None: [dict(f) for f in upstream])
    r = _get(_client, f"{base}/pulls/9/files")
    assert r.status_code == 200
    _rj = r.json()
    files = _rj["files"] if isinstance(_rj, dict) else _rj  # arch11 裸数组兼容

    # #1765 reason 前缀映射到 #1786 枚举（同一来源、同一语义）
    reasons = {f["filename"]: f.get("diff_unavailable_reason") for f in files}
    assert reasons["big.txt"] == "too_large"
    assert reasons["plain.txt"] == "upstream_missing"
    assert reasons["stale.py"] == "empty_file"
    assert reasons["legacy.py"] is None

    # 零回归：patch 对象（含 .patch.diff/.patch.reason）原样保留，投影只增顶层字段
    for f, src in zip(files[:3], upstream[:3]):
        assert f["patch"] == src["patch"]
    # 旧式 string patch 收敛为 {"diff": ...} 且顶层别名与其严格一致
    assert files[3]["patch"] == {"diff": "@@ -1 +1 @@\n+z"}
    assert files[3]["diff"] == files[3]["patch"]["diff"]



import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")

from fastapi.testclient import TestClient

from src.api.main import app
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

SK1790_REPO = "org-1790/repo-1790"  # 重命名避免与 git-repo 段的全局 REPO 冲突
SK1790_REPO_KEY = "gitcode:org-1790/repo-1790"
FORMAT_MESSAGE = "repo 参数格式应为 owner/repo"

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    # arch11 适配：账号走 grape_account_db（D105 UUID 主键；直插绕过 UUID/表结构会失败）
    from src.domains.mgmt.auth import grape_account_db
    _uid = grape_account_db.create_grape_account("test_api_1790", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_api_1790")
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT OR REPLACE INTO external_identities (grape_user_id, provider, external_id, external_login, access_token) VALUES (?, 'gitcode', ?, ?, ?)",
                (str(_uid["id"]), "gc_test_api_1790", "test_api_1790", "test_api_1790_token"))
        except Exception:
            pass  # arch11 表结构差异：external_identities 列名/存在性不同，非本断言必需
        # 仓库级技能 + managed_repos 行（repo_key 前缀解析用，对齐 test_api_routes 模式）
        c.execute(
            "INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, scope, owner_id, is_active) VALUES (?,?,?,?,?,?,1)",
            ("t-1790-repo-skill", "t-1790-repo-skill", "t", "coding", "repo", SK1790_REPO_KEY))
        # 官方 + 本人技能（QA 复审存疑 2：no_repo 用例锁「官方+个人」内容语义，非仅形状）
        c.execute(
            "INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, scope, owner_id, is_active) VALUES (?,?,?,?,?,?,1)",
            ("t-1790-official-skill", "t-1790-official-skill", "t", "coding", "official", "official"))
        c.execute(
            "INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, scope, owner_id, is_active) VALUES (?,?,?,?,?,?,1)",
            ("t-1790-user-skill", "t-1790-user-skill", "t", "coding", "user", str(_uid["id"])))
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
            (SK1790_REPO_KEY, "org-1790", "repo-1790", SK1790_REPO))
        db.commit()
    from src.api.jwt_utils import create_access_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (_uid["id"],))
        row = c.fetchone()
    ver = row[0] if row else 0
    token = create_access_token(_uid["id"], _uid["username"], ver)
    _headers = {"Authorization": f"Bearer {token}"}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM skill WHERE skill_name IN ('t-1790-repo-skill', 't-1790-official-skill', 't-1790-user-skill')")
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (SK1790_REPO_KEY,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
            db.commit()


class TestRepoParamThreeStates:
    def test_bare_repo_name_returns_400(self):
        """切片1（Issue 用户症状）：裸仓库名不得静默回退官方列表 → 400 + owner/repo 格式提示。"""
        r = client.get("/api/skills", params={"repo": "huaweicloud-skills-dev"}, headers=_headers)
        assert r.status_code == 400, f"裸仓库名应返回 400，实际 {r.status_code}: {r.text[:200]}"
        assert r.json()["detail"]["message"] == FORMAT_MESSAGE, \
            f"message 应为完整句 {FORMAT_MESSAGE!r}，实际 {r.json()['detail']['message']!r}"

    def test_invalid_repo_variants_return_400(self):
        """切片2：其他不合规变体（空 owner / 空 repo 名 / 多余 /）同样 400 + 格式提示。"""
        for bad in ("/repo-1790",              # 空 owner
                    "org-1790/",               # 空 repo 名
                    "org-1790/repo-1790/extra"):  # 多余 /
            r = client.get("/api/skills", params={"repo": bad}, headers=_headers)
            assert r.status_code == 400, f"repo={bad!r} 应返回 400，实际 {r.status_code}: {r.text[:200]}"
            assert r.json()["detail"]["message"] == FORMAT_MESSAGE


class TestRepoParamColonVariants:
    """QA 复审需修 1：冒号类不合规格式只剥第一个冒号段、不白名单前缀，
    此前校验放行后 repo_key 原样入库无命中，走 elif repo 分支静默回退
    「官方+个人」列表（Issue #1790 同类残留）——应显式 400。"""

    def test_residual_colon_repo_returns_400(self):
        """repo=a:b:c/d（残留冒号）→ 400 + 完整句格式提示，不得静默回退。"""
        r = client.get("/api/skills", params={"repo": "a:b:c/d"}, headers=_headers)
        assert r.status_code == 400, f"repo='a:b:c/d' 应返回 400，实际 {r.status_code}: {r.text[:200]}"
        assert r.json()["detail"]["message"] == FORMAT_MESSAGE, \
            f"message 应为完整句 {FORMAT_MESSAGE!r}，实际 {r.json()['detail']['message']!r}"

    def test_empty_prefix_repo_returns_400(self):
        """repo=:owner/repo（空前缀）→ 400 + 完整句格式提示。"""
        r = client.get("/api/skills", params={"repo": ":org-1790/repo-1790"}, headers=_headers)
        assert r.status_code == 400, f"repo=':org-1790/repo-1790' 应返回 400，实际 {r.status_code}: {r.text[:200]}"
        assert r.json()["detail"]["message"] == FORMAT_MESSAGE, \
            f"message 应为完整句 {FORMAT_MESSAGE!r}，实际 {r.json()['detail']['message']!r}"

    def test_unknown_prefix_repo_returns_400(self):
        """repo=foo:owner/repo（非白名单前缀）→ 400 + 完整句格式提示。"""
        r = client.get("/api/skills", params={"repo": "foo:org-1790/repo-1790"}, headers=_headers)
        assert r.status_code == 400, f"repo='foo:org-1790/repo-1790' 应返回 400，实际 {r.status_code}: {r.text[:200]}"
        assert r.json()["detail"]["message"] == FORMAT_MESSAGE, \
            f"message 应为完整句 {FORMAT_MESSAGE!r}，实际 {r.json()['detail']['message']!r}"


def test_contract_repo_param_has_format_description():
    """切片3：契约 repo 参数 description 含 owner/repo 格式说明（CLI help 随契约自动生效）。"""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    params = r.json()["paths"]["/api/skills"]["get"]["parameters"]
    repo_param = next(p for p in params if p["name"] == "repo")
    assert "owner/repo" in repo_param.get("description", ""), \
        f"契约 repo 参数缺 owner/repo 格式说明: {repo_param!r}"


class TestRepoParamRegressionLock:
    """切片4：合规格式与既有语义回归锁定（修复前后行为应一致）。"""
    def setup_method(self):
        # arch11 适配：本类走真 JWT（_headers）——清 _client autouse 的假用户 override，
        # 否则 user["id"]="u1790" 与 skill.owner_id(UUID) 不匹配，user 维度技能静默不可见。
        from src.api.main import app as _app
        self._saved_overrides = dict(_app.dependency_overrides)
        _app.dependency_overrides.clear()

    def teardown_method(self):
        from src.api.main import app as _app
        _app.dependency_overrides.update(self._saved_overrides)


    def test_full_name_repo_returns_repo_skills(self):
        """owner/repo 全名 + scope=repo → 正常返回该仓库的仓库级技能（不回归）。"""
        r = client.get("/api/skills", params={"repo": SK1790_REPO, "scope": "repo"}, headers=_headers)
        assert r.status_code == 200, r.text[:200]
        names = [s["name"] for s in r.json()["skills"]]
        assert "t-1790-repo-skill" in names, f"仓库级技能应可见: {names}"

    def test_provider_prefixed_repo_still_valid(self):
        """带 gitcode: 前缀的 repo_key 入参（既有支持）不因新校验被 400。"""
        r = client.get("/api/skills", params={"repo": SK1790_REPO_KEY, "scope": "repo"}, headers=_headers)
        assert r.status_code == 200, r.text[:200]
        names = [s["name"] for s in r.json()["skills"]]
        assert "t-1790-repo-skill" in names, f"前缀形式仓库技能应可见: {names}"

    def test_no_repo_unchanged(self):
        """不传 repo → 行为不变：官方 + 本人技能可见，仓库级技能不混入（内容语义锁定）。"""
        r = client.get("/api/skills", headers=_headers)
        assert r.status_code == 200, r.text[:200]
        assert "skills" in r.json()
        assert isinstance(r.json()["skills"], list)
        names = [s["name"] for s in r.json()["skills"]]
        assert "t-1790-official-skill" in names, f"官方技能应可见: {names[:10]}"
        assert "t-1790-user-skill" in names, f"本人技能应可见: {names[:10]}"
        assert "t-1790-repo-skill" not in names, "无 repo 过滤时仓库级技能不应混入官方+个人列表"

    def test_nonexistent_repo_keeps_empty_semantics(self):
        """不存在的 owner/repo（格式合规）→ 保持原有空结果语义，不报错不回退官方列表。"""
        r = client.get("/api/skills", params={"repo": "ghost-1790/none", "scope": "repo",
                                              "page": 1, "page_size": 5},
                       headers=_headers)
        assert r.status_code == 200, r.text[:200]
        assert r.json()["skills"] == []
        assert r.json()["total"] == 0
