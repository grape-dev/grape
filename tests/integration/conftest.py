"""Issue #2153: org pipeline end-to-end integration fixtures.

设计约束（见 docs/issue-2153/plan.md 与 Issue #2153 Spec）：

- 每个用例使用独立 ``tmp_path`` SQLite 文件，不复用 tests/ 全局测试库；
  用例结束把 ``session_manager.db`` 与被重绑的模块引用还原，避免污染其它套件；
- mock 仅限 VCS 建仓/托管/bot token（adapter 层）与 worker 派发入口；
- 保留真实 DB schema、真实 ``org_dispatch`` 状态机与真实子 Issue 建档逻辑。
"""
from __future__ import annotations

import itertools
import sqlite3
import sys

import pytest

# 固定身份 / 凭据 / 场景（确定性：不随环境与时间变化）
E2E_OWNER_ID = 22153
E2E_OWNER_NAME = "issue2153-owner"
E2E_BOT_TOKEN = "issue2153-bot-token"
ORG_ISSUE_NUMBER = 21530

_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=FULL",
    "PRAGMA foreign_keys=ON",
)


def _iter_src_modules():
    for name, mod in list(sys.modules.items()):
        if name == "src" or name.startswith("src."):
            yield name, mod


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 ``session_manager.db`` 指向本用例独占的 tmp_path SQLite 文件。

    做法：先对当前已 ``init_database()`` 的库做一次 schema 快照（sqlite backup），再重绑到
    快照副本，避免从未建表的空库启动。仓库以模块级 ``db`` 全局复用连接，故这里同时按引用
    把各 src 模块的 ``db`` 重绑到新连接；teardown 无条件还原，保证同进程内后续用例仍打回
    全局测试库。
    """
    import src.kernel.session_manager as sm

    base_path = sm._db_path
    db_path = str(tmp_path / "integration.db")
    monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")

    old_db = sm.db
    old_path = sm._db_path
    new_db = sqlite3.connect(db_path, check_same_thread=False)
    # schema 快照：直接复制已初始化的全局测试库结构（含迁移后的完整表/列）
    snapshot = sqlite3.connect(base_path)
    try:
        snapshot.backup(new_db)
    finally:
        snapshot.close()
    for pragma in _SQLITE_PRAGMAS:
        new_db.execute(pragma)
    try:  # 与 session_manager 口径一致：schema 灰度开启时套 SQLite 前缀包装
        from src.kernel.db import SchemaSQLiteDB, schema_mode_enabled

        if schema_mode_enabled():
            new_db = SchemaSQLiteDB(new_db)
    except Exception:  # noqa: BLE001 - schema 灰度探测失败按普通连接处理
        pass

    sm.db = new_db
    sm._db_path = db_path
    for _name, mod in _iter_src_modules():
        if getattr(mod, "db", None) is old_db:
            mod.db = new_db

    # 兜底：快照若缺 mgmt 基础表（base 库尚未初始化/被并发重建），显式补齐 schema
    tables = {
        row[0]
        for row in new_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "managed_repos" not in tables or "organization" not in tables:
        from src.domains.mgmt.user import db as user_db_init

        user_db_init.init_db()
        from src.api import db_setup

        db_setup.init_database()

    # 清进程级场景缓存：避免上一用例的旧 DB 结果串到本用例的新库
    try:
        from src.api.pipeline_config import invalidate_scene_cache

        invalidate_scene_cache()
    except Exception:  # noqa: BLE001 - 缓存模块缺失不影响主流程
        pass

    try:
        yield new_db
    finally:
        for _name, mod in _iter_src_modules():
            if getattr(mod, "db", None) is new_db:
                mod.db = old_db
        sm.db = old_db
        sm._db_path = old_path
        try:
            new_db.close()
        except Exception:  # noqa: BLE001 - 包装器无 close 时忽略
            pass


class OrgE2E:
    """组织流水线端到端夹具：真实 DB + 真实状态机，mock 只覆盖 VCS/派发 adapter。"""

    def __init__(self, client, db, create_repo_state, created_child_issues):
        self.client = client
        self.db = db
        self._create_repo_state = create_repo_state
        self.created_child_issues = created_child_issues  # [(repo_key, issue_number)]
        self.org_id = 0
        self.org_repo_key = ""
        self.org_scene_id = ""

    # -- provisioning / 关联仓库 ------------------------------------------
    def create_org(self, slug: str) -> dict:
        resp = self.client.post(
            "/api/orgs",
            json={"name": slug, "slug": slug, "description": "issue2153 e2e"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        self.org_id = str(body["id"])
        # start_provision 已被替换为同步 provision；响应即反映 ready 态
        assert body["provision_status"] == "ready", body
        self.org_repo_key = body["org_repo_key"]
        self.org_scene_id = body["org_scene_id"]
        assert self.org_repo_key.startswith("gitcode:"), body
        assert self.org_scene_id, body
        assert self.org_repo_key in self._create_repo_state["repo_keys"]
        return body

    def link_target_repo(self, repo_key: str, scene_id: str) -> dict:
        from src.domains.mgmt.user.db import add_managed_repo
        from src.kernel.repo_key import parse_repo_key
        from src.kernel.session_manager import db_lock

        _, owner, name = parse_repo_key(repo_key)
        # 先托管（org_id=0），再走真实关联 API 绑定到组织
        assert add_managed_repo(repo_key, owner, name, webhook_id=100, dev_scene=scene_id)
        with db_lock:
            c = self.db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active) VALUES (?,?,?,'[]',1)",
                (scene_id, scene_id, repo_key),
            )
            self.db.commit()

        resp = self.client.post(
            f"/api/orgs/{self.org_id}/repos",
            json={"repo_key": repo_key},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["org_id"] == self.org_id, resp.text
        return resp.json()

    def list_org_repos(self) -> list[dict]:
        resp = self.client.get(f"/api/orgs/{self.org_id}/repos")
        assert resp.status_code == 200, resp.text
        return resp.json()["repos"]

    # -- 组织需求 ---------------------------------------------------------
    def create_org_issue(self, *, number: int = ORG_ISSUE_NUMBER,
                         title: str = "组织需求：统一登录改造",
                         description: str = "落地到两个目标仓库") -> int:
        from src.kernel.session_manager import db_lock

        with db_lock:
            c = self.db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "task_description, status, scene_id, stage) "
                "VALUES (?,?,?,?,?,?,'active',?,'issue')",
                (E2E_OWNER_ID, E2E_OWNER_NAME, self.org_repo_key, number,
                 title, description, self.org_scene_id),
            )
            self.db.commit()
        return number

    # -- 下派 -------------------------------------------------------------
    def dispatch(self, targets, *, org_issue_number: int = ORG_ISSUE_NUMBER,
                 title: str = "统一登录改造", description: str = "仅改造登录与鉴权模块"):
        return self.client.post(
            f"/api/orgs/{self.org_id}/dispatches",
            json={
                "org_issue_number": org_issue_number,
                "targets": list(targets),
                "context": {"goal": "mvp"},
                "title": title,
                "description": description,
                "scene_id": self.org_scene_id,
            },
        )

    def dispatch_detail(self, dispatch_id: int) -> dict:
        resp = self.client.get(f"/api/orgs/{self.org_id}/dispatches/{dispatch_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    # -- 子任务真实状态迁移 ----------------------------------------------
    def complete_child_issue(self, repo_key: str, issue_number: int) -> None:
        """走真实完成迁移（issue → completed + stage=done），由 sweeper 汇总。"""
        from src.api.issues_center import confirm_issue_done

        changed = confirm_issue_done(repo_key, issue_number, confirmed_by="e2e_test")
        assert changed == 1, f"child issue {repo_key}#{issue_number} not completed"

    def sweep(self) -> None:
        """驱动真实 sweeper：读子 Issue 状态 + 超时判定 + 聚合。"""
        from src.domains.mgmt import org_dispatch

        org_dispatch.sweep_once()

    def child_issue_rows(self, repo_key: str) -> list[tuple]:
        from src.kernel.session_manager import db_lock

        with db_lock:
            c = self.db.cursor()
            return c.execute(
                "SELECT issue_number, status, stage FROM issue WHERE repo_key=? "
                "ORDER BY issue_number",
                (repo_key,),
            ).fetchall()


@pytest.fixture
def org_e2e(isolated_db, monkeypatch):
    """TestClient + VCS adapter mock + 同步 provision 的组织 e2e 夹具。"""
    from fastapi.testclient import TestClient

    from src.api import issues_center
    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app
    from src.domains.mgmt import org_provision
    from src.domains.mgmt.routes import org_repos as org_repos_route
    from src.domains.mgmt.routes import orgs as orgs_route
    from src.domains.mgmt.user import db as user_db

    create_state = {"repo_keys": []}

    def _fake_create_new_repo(body, owner_user):
        full_name = f"{E2E_OWNER_NAME}-org/{body.name}"
        from src.kernel.repo_key import to_repo_key

        repo_key = to_repo_key(full_name, provider="gitcode")
        create_state["repo_keys"].append(repo_key)
        return {"full_name": full_name, "provider": "gitcode",
                "owner": E2E_OWNER_NAME + "-org", "name": body.name, "hosted": False}

    def _fake_host_repo(body, user, request=None, provider=""):
        # 仅“托管”动作走 mock：真实写入 managed_repos（保留真实 DB 行为）
        from src.domains.mgmt.user.db import add_managed_repo
        from src.kernel.repo_key import parse_repo_key, to_repo_key

        repo_key = to_repo_key(body.full_name, provider=provider or "gitcode")
        _, owner, name = parse_repo_key(repo_key)
        add_managed_repo(repo_key, owner, name, webhook_id=4242, org_id=user.get("org_id", 0))
        return {"message": "hosted", "webhook_id": 4242}

    monkeypatch.setattr(org_provision, "create_new_repo", _fake_create_new_repo)
    monkeypatch.setattr(org_provision, "host_repo", _fake_host_repo)
    # orgs 路由默认后台线程 provision → 改为同步，保证确定性
    monkeypatch.setattr(orgs_route, "start_provision", org_provision.provision_org)
    # 关联仓库的 maintainer 校验是外部 VCS 权限调用 → 放行
    monkeypatch.setattr(org_repos_route, "_require_repo_maintainer", lambda user, repo_key: None)

    # bot token（VCS 凭据 adapter）
    monkeypatch.setattr(user_db, "get_gitcode_token", lambda repo_key="", user_id=0: E2E_BOT_TOKEN)
    monkeypatch.setattr(user_db, "get_github_token", lambda repo_key="", user_id=0: E2E_BOT_TOKEN)

    # 子 Issue 远端建单 / 查重（VCS adapter）——create_task_via_issue 其余逻辑保持真实
    child_seq = itertools.count(90001)
    created_child_issues: list[tuple[str, int]] = []

    def _fake_find_open_issue(*args, **kwargs):
        return None

    def _fake_post_gitcode_issue(api_token, owner, repo, title, body_text="", assignee=""):
        number = next(child_seq)
        created_child_issues.append((f"gitcode:{owner}/{repo}", number))
        return {"number": number}

    monkeypatch.setattr(issues_center, "_find_open_issue_by_title", _fake_find_open_issue)
    monkeypatch.setattr(issues_center, "_post_gitcode_issue", _fake_post_gitcode_issue)
    monkeypatch.setattr(issues_center, "_attach_list", lambda *a, **kw: [])

    # D21/S32 收口后建单走 VcsApiClient.create_issue（_post_gitcode_issue 仅存量委托），
    # 集成用例必须在适配器层拦截，否则会真打 VCS 接口（401 token not found）。
    from src.domains.runtime.vcs.api_client import VcsApiClient

    def _fake_vcs_create_issue(self, title, body="", assignee=""):
        number = next(child_seq)
        created_child_issues.append((f"{self.provider}:{self.owner}/{self.repo}", number))
        return {"number": number, "title": title}

    monkeypatch.setattr(VcsApiClient, "create_issue", _fake_vcs_create_issue)

    # worker 派发入口（不启真实 worker / 不触网）
    def _fake_run_pipeline(*args, **kwargs):
        return {"ok": True, "seq": 0}

    # Issue #2251：patch 模块对象本身而非字符串路径——`_isolate_src_modules_and_env`
    # 会按用例清理 sys.modules，但父包属性可能仍指向旧模块对象，字符串路径补丁会
    # 打在旧对象上导致 `_trigger_child_issue` 的真实 import 拿到未打补丁的新对象。
    import importlib
    _event_pipeline = importlib.import_module(
        "src.domains.runtime.pipeline.event_pipeline")
    monkeypatch.setattr(_event_pipeline, "run_pipeline", _fake_run_pipeline)
    try:  # 覆盖 webhook 模块的 module-level 绑定，避免绕过上面这层 mock
        from src.domains.runtime.routes import webhook as webhook_route

        monkeypatch.setattr(webhook_route, "run_pipeline", _fake_run_pipeline)
    except Exception:  # noqa: BLE001 - 无该模块时忽略
        pass
    # Issue #2251：组织 Issue 聚合评论走 VCS bot token，集成用例不触网。
    try:
        from src.domains.runtime.vcs.api_client import VcsApiClient

        monkeypatch.setattr(
            VcsApiClient, "post_issue_comment", lambda self, num, body: {"ok": True})
    except Exception:  # noqa: BLE001 - VCS client 缺失时忽略
        pass
    # 离线守卫：project-info 后台刷新会 git fetch 远端仓库（VCS 网络副作用），
    # 集成用例不需要它，直接置空保证无外网。
    try:
        from src.domains.runtime.project_info import builder as project_info_builder

        monkeypatch.setattr(
            project_info_builder, "maybe_refresh_project_info", lambda *a, **kw: None
        )
    except Exception:  # noqa: BLE001 - 模块缺失时忽略
        pass

    def _fake_user():
        return {"id": E2E_OWNER_ID, "username": E2E_OWNER_NAME, "auth_type": "jwt"}

    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    client = TestClient(app)

    harness = OrgE2E(client, isolated_db, create_state, created_child_issues)
    try:
        yield harness
    finally:
        app.dependency_overrides.clear()
