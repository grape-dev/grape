"""项目模板（project-templates）路由与一键创建链路测试"""
import os
import sys
import json
import asyncio
from unittest.mock import patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import templates as templates_mod
from src.user import db as user_db

client = TestClient(app)

_uid = None
_token = ""
_headers = {}

CONFIG_PATH = os.path.join(ROOT, "configs", "project-templates.json")


def setup_module():
    global _uid, _token, _headers
    _uid = user_db.create_user("test_tmpl", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test_tmpl")
    acc = user_db.get_user_account("test_tmpl") or {}
    acc["gitcode_api_token"] = "test_tmpl_token_12345"
    user_db.save_user_account("test_tmpl", acc)
    _token = user_db.create_session_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])


def _reload_config():
    return templates_mod._load_templates()


# ===================== 配置合法性 =====================
class TestConfig:
    def test_config_exists_and_valid(self):
        assert os.path.exists(CONFIG_PATH), "project-templates.json 必须存在"
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cats = data.get("categories", [])
        assert isinstance(cats, list) and len(cats) >= 1

    def test_template_ids_unique(self):
        data = _reload_config()
        ids = []
        for cat in data.get("categories", []):
            for t in cat.get("templates", []):
                ids.append(t.get("id"))
        assert len(ids) == len(set(ids)), "template id 必须唯一"

    def test_source_repo_required_for_git(self):
        data = _reload_config()
        for cat in data.get("categories", []):
            for t in cat.get("templates", []):
                if t.get("source") == "git":
                    assert t.get("source_repo"), f"{t.get('id')} git 模板缺少 source_repo"
                    assert "/" in t["source_repo"]

    def test_local_template_dir_exists(self):
        data = _reload_config()
        for cat in data.get("categories", []):
            for t in cat.get("templates", []):
                if t.get("source") == "local":
                    d = os.path.join(ROOT, "configs", "project-templates", t["id"])
                    assert os.path.isdir(d), f"local 模板目录不存在: {d}"

    def test_video_url_field_valid(self):
        """video_url 可选：空或合法 http(s) 链接"""
        data = _reload_config()
        for cat in data.get("categories", []):
            for t in cat.get("templates", []):
                v = t.get("video_url", "")
                assert isinstance(v, str), f"{t.get('id')} video_url 必须是字符串"
                if v:
                    assert v.startswith("http://") or v.startswith("https://"), \
                        f"{t.get('id')} video_url 必须是 http(s) 链接: {v}"

    def test_scene_id_valid(self):
        data = _reload_config()
        with open(os.path.join(ROOT, "configs", "agent-skill-mapping.json"), "r", encoding="utf-8") as f:
            scenes = json.load(f).get("scenes", {})
        for cat in data.get("categories", []):
            for t in cat.get("templates", []):
                sid = t.get("scene_id") or cat.get("scene_id")
                assert sid in scenes, f"模板场景 '{sid}' 不在 agent-skill-mapping 中"


# ===================== GET 列表 =====================
class TestList:
    def test_list_requires_auth(self):
        r = client.get("/api/project-templates")
        assert r.status_code == 401

    def test_list_returns_categories(self):
        r = client.get("/api/project-templates", headers=_headers)
        assert r.status_code == 200
        cats = r.json()["categories"]
        assert len(cats) >= 1
        first = cats[0]
        assert "templates" in first and isinstance(first["templates"], list)


# ===================== POST 创建 =====================
class TestCreate:
    def test_template_not_found(self):
        r = client.post("/api/project-templates/create",
                        json={"template_id": "nope", "repo_name": "x"},
                        headers=_headers)
        assert r.status_code == 404

    def test_invalid_repo_name(self):
        with patch.object(templates_mod, "_find_template", return_value={"template": {"id": "hello-skill"}, "scene_id": "skill-dev"}):
            r = client.post("/api/project-templates/create",
                            json={"template_id": "hello-skill", "repo_name": "bad/name"},
                            headers=_headers)
            assert r.status_code == 400

    def test_duplicate_repo_name_409(self):
        """同名仓库已存在（GitCode 返回 409）→ 409 错误提示"""
        with patch.object(templates_mod, "_find_template",
                          return_value={"template": {"id": "hello-skill", "source": "local"}, "scene_id": "skill-dev"}), \
             patch.object(templates_mod, "get_user_gitcode_login", return_value="test_tmpl"), \
             patch.object(templates_mod, "create_repo",
                          side_effect=RuntimeError("GitCode API POST https://api.gitcode.com/api/v5/user/repos failed (409): exists")):
            r = client.post("/api/project-templates/create",
                            json={"template_id": "hello-skill", "repo_name": "dup-repo"},
                            headers=_headers)
            assert r.status_code == 409
            assert "已存在" in r.json()["detail"]["message"]

    def test_no_token(self):
        """无 GitCode token 用户 → 400"""
        uid = user_db.create_user("test_tmpl_notok", "test123")
        try:
            acc = user_db.get_user_account("test_tmpl_notok") or {}
            acc["gitcode_api_token"] = ""
            user_db.save_user_account("test_tmpl_notok", acc)
            tok = user_db.create_session_token(uid["id"])
            h = {"Authorization": f"Bearer {tok}"}
            with patch.object(templates_mod, "_find_template",
                              return_value={"template": {"id": "hello-skill", "source": "local"}, "scene_id": "skill-dev"}):
                r = client.post("/api/project-templates/create",
                                json={"template_id": "hello-skill", "repo_name": "some-repo"},
                                headers=h)
                assert r.status_code == 400
        finally:
            user_db.delete_user(uid["id"])

    def test_success_local_template(self):
        """成功路径：local 模板，mock 建仓库/push/托管/场景"""
        with patch.object(templates_mod, "_find_template",
                          return_value={"template": {"id": "hello-skill", "source": "local",
                                                     "default_repo_name": "hello-skill", "scene_id": "skill-dev"},
                                        "scene_id": "skill-dev"}), \
             patch.object(templates_mod, "get_user_gitcode_login", return_value="test_tmpl"), \
             patch.object(templates_mod, "create_repo", return_value={"full_name": "test_tmpl/hello-skill"}), \
             patch.object(templates_mod, "push_template_to_repo", return_value=True), \
             patch("src.api.routes.repos.host_repo", new=_fake_host), \
             patch("src.api.routes.scenes.apply_scene", new=_fake_apply_scene):
            r = client.post("/api/project-templates/create",
                            json={"template_id": "hello-skill", "repo_name": ""},
                            headers=_headers)
            assert r.status_code == 200
            data = r.json()
            assert data["full_name"] == "test_tmpl/hello-skill"
            assert data["webhook_id"] == 123
            assert data["scene_id"] == "skill-dev"

    def test_host_failure(self):
        """仓库已创建但托管失败 → 502 + 提示"""
        with patch.object(templates_mod, "_find_template",
                          return_value={"template": {"id": "hello-skill", "source": "local",
                                                     "default_repo_name": "hello-skill", "scene_id": "skill-dev"},
                                        "scene_id": "skill-dev"}), \
             patch.object(templates_mod, "get_user_gitcode_login", return_value="test_tmpl"), \
             patch.object(templates_mod, "create_repo", return_value={"full_name": "test_tmpl/hello-skill"}), \
             patch.object(templates_mod, "push_template_to_repo", return_value=True), \
             patch("src.api.routes.repos.host_repo", side_effect=RuntimeError("webhook boom")):
            r = client.post("/api/project-templates/create",
                            json={"template_id": "hello-skill", "repo_name": ""},
                            headers=_headers)
            assert r.status_code == 502
            assert "托管失败" in r.json()["detail"]["message"]


async def _fake_host(body, user):
    return {"message": "仓库托管成功", "webhook_id": 123}


async def _fake_apply_scene(owner, repo, body, user):
    return {"message": "ok", "updated": 5}


# ===================== repo_manager 新函数 =====================
class TestRepoManager:
    def test_create_repo_calls_api(self):
        from src.user.repo_manager import create_repo
        with patch("src.user.repo_manager._req") as mock_req:
            mock_req.return_value = {"full_name": "me/myrepo", "name": "myrepo"}
            result = create_repo("tok", "myrepo", "desc", private=False)
            url = mock_req.call_args[0][1]
            assert url.endswith("/user/repos")
            body = mock_req.call_args[0][3]
            assert body["name"] == "myrepo"
            assert body["private"] is False
            assert result["full_name"] == "me/myrepo"

    def test_create_repo_no_token(self):
        from src.user.repo_manager import create_repo
        with patch("src.user.repo_manager._req") as mock_req:
            try:
                create_repo("", "repo")
                assert False, "应抛错"
            except RuntimeError:
                pass
            mock_req.assert_not_called()

    def test_get_user_gitcode_login(self):
        from src.user.repo_manager import get_user_gitcode_login
        with patch("src.user.repo_manager._req") as mock_req:
            mock_req.return_value = {"login": "grape_dev"}
            assert get_user_gitcode_login("tok") == "grape_dev"

    def test_push_local_template(self):
        """local 模板：复制本地目录并执行 git init/add/commit/push"""
        from src.user.repo_manager import push_template_to_repo
        tpl = {"id": "hello-skill", "source": "local"}
        with patch("src.user.repo_manager._run_git") as mock_git, \
             patch("src.user.repo_manager._run_git_has_origin", return_value=False), \
             patch("src.user.repo_manager._git_has_changes", return_value=True):
            mock_git.side_effect = [""] * 20  # init/add/branch/commit/push 等
            ok = push_template_to_repo(tpl, "me", "hello-skill", "user_tok", bot_token="")
            assert ok is True
            cmds = [c[0][0] if c and c[0] else [] for c in mock_git.call_args_list]
            assert ["init"] in cmds
            assert ["push", "-u", "origin", "HEAD:main"] in cmds

    def test_push_git_template_needs_bot_token(self):
        """git 模板无 bot token → 抛错"""
        from src.user.repo_manager import push_template_to_repo
        tpl = {"id": "micro-svc", "source": "git", "source_repo": "devops-bot/cloud-native-microsvc"}
        with patch("src.user.repo_manager._run_git") as mock_git:
            try:
                push_template_to_repo(tpl, "me", "repo", "user_tok", bot_token="")
                assert False, "应抛错"
            except RuntimeError as e:
                assert "Bot" in str(e)
            mock_git.assert_not_called()
