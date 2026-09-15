"""Issue #46 onboarding 重构测试：模板驱动任务创建 + 幂等/自愈 + result_text 单写。

见 issue-46-onboarding-task-notification-fix-design.md §四。
"""
import os
import json
import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


@pytest.fixture(scope="module")
def ob():
    from src.api import onboarding
    return onboarding


@pytest.fixture(scope="module")
def parts():
    from src.api.routes import tasks
    return tasks


def _write_default_template(tmp_path, monkeypatch, tasks_list):
    """把默认模板文件指向 tmp 下的 onboarding-templates.json。"""
    cfg = {"default": {"tasks": tasks_list}}
    f = tmp_path / "onboarding-templates.json"
    f.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("src.api.onboarding.CONFIG_DIR", str(tmp_path))


def _write_repo_template(tmp_path, tasks_list):
    """在仓库根目录（WORKSPACE_DIR/{owner}/{repo}）写自定义模板。"""
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "onboarding-tasks.json").write_text(
        json.dumps({"tasks": tasks_list}, ensure_ascii=False), encoding="utf-8")
    return repo_root


class TestLoadOnboardingTemplate:
    def test_default_template(self, ob, tmp_path, monkeypatch):
        _write_default_template(tmp_path, monkeypatch, [
            {"type": "experience", "title": "引导·快速部署体验", "description": "体验"},
            {"type": "dev", "title": "引导·二次开发", "description": "开发"},
        ])
        tasks = ob._load_onboarding_template("owner/repo")
        assert len(tasks) == 2
        assert tasks[0]["type"] == "experience"
        assert tasks[1]["type"] == "dev"

    def test_repo_custom_overrides_default(self, ob, tmp_path, monkeypatch):
        _write_default_template(tmp_path, monkeypatch, [
            {"type": "experience", "title": "引导·快速部署体验", "description": "体验"},
            {"type": "dev", "title": "引导·二次开发", "description": "开发"},
        ])
        # 仓库根 = {WORKSPACE_DIR}/{provider}/{owner}/{repo}（provider 默认 gitcode）
        repo_root = tmp_path / "gitcode" / "owner" / "repo"
        repo_root.mkdir(parents=True, exist_ok=True)
        (repo_root / "onboarding-tasks.json").write_text(
            json.dumps({"tasks": [
                {"type": "experience", "title": "仅体验", "description": "只有体验"},
            ]}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        tasks = ob._load_onboarding_template("owner/repo")
        assert len(tasks) == 1
        assert tasks[0]["type"] == "experience"
        assert tasks[0]["title"] == "仅体验"

    def test_no_default_file_returns_empty(self, ob, tmp_path, monkeypatch):
        monkeypatch.setattr("src.api.onboarding.CONFIG_DIR", str(tmp_path / "empty"))
        tasks = ob._load_onboarding_template("owner/repo")
        assert tasks == []

    def test_invalid_task_item_skipped(self, ob, tmp_path, monkeypatch):
        _write_default_template(tmp_path, monkeypatch, [
            {"type": "experience", "title": "引导·快速部署体验", "description": "体验"},
            {"type": "", "title": "无类型应跳过"},
            {"no_type": 1},
        ])
        tasks = ob._load_onboarding_template("owner/repo")
        assert len(tasks) == 1
        assert tasks[0]["type"] == "experience"

    def test_unknown_type_default_title(self, ob, tmp_path, monkeypatch):
        _write_default_template(tmp_path, monkeypatch, [
            {"type": "custom", "title": "", "description": "自定义"},
        ])
        tasks = ob._load_onboarding_template("owner/repo")
        # 未知类型无 title → 跳过（不在 _DEFAULT_TITLES）
        assert tasks == []


class TestOnboardingTaskMarkers:
    def test_mark_and_done(self, ob):
        key = "owner/repo"
        ob._mark_task_done(key, "experience")
        assert ob._task_done(key, "experience") is True
        assert ob._task_done(key, "dev") is False

    def test_load_completed_tasks(self, ob):
        key = "owner/repo"
        ob._mark_task_done(key, "experience")
        tasks = [{"type": "experience"}, {"type": "dev"}]
        done = ob._load_completed_tasks(key, tasks)
        assert "experience" in done
        assert "dev" not in done


class TestCreateStandardTask:
    """create_standard_task：真实 Issue 创建 + 建档，幂等 + 复用。"""

    def _patch_http(self, parts, monkeypatch):
        """桩掉 _find_open_issue_by_title / _post_gitcode_issue，模拟 GitCode API。"""
        created = {"issues": [], "calls": 0}

        def fake_find(token, owner, repo, title):
            for it in created["issues"]:
                if it["title"] == title:
                    return it["number"]
            return None

        def fake_post(token, owner, repo, title, body="", assignee=""):
            created["calls"] += 1
            n = len(created["issues"]) + 1
            created["issues"].append({"title": title, "number": n})
            return {"number": n}

        monkeypatch.setattr(parts, "_find_open_issue_by_title", fake_find)
        monkeypatch.setattr(parts, "_post_gitcode_issue", fake_post)
        return created

    def test_create_and_then_reuse(self, parts, monkeypatch):
        from src.api.routes.tasks import create_standard_task
        created = self._patch_http(parts, monkeypatch)
        # 桩 ensure_issue_task：隔离真实 DB
        monkeypatch.setattr(parts, "ensure_issue_task", lambda *a, **k: 1)
        repo_key = "owner/repo"
        r1 = create_standard_task(repo_key, "引导·快速部署体验", "体验描述", "bot-token", assignee="owner")
        assert r1.get("ok") is True
        assert r1.get("created") is True
        r2 = create_standard_task(repo_key, "引导·快速部署体验", "体验描述", "bot-token", assignee="owner")
        assert r2.get("ok") is True
        assert r2.get("created") is False  # 复用已有 Issue
        assert created["calls"] == 1  # 只 POST 一次


class TestPostResultToIssueNoMessage:
    """Issue #46：_post_result_to_issue 不再写 task conversation（result_text 单写）。"""

    def test_no_add_message_call_in_function(self):
        import inspect
        from src.api import event_pipeline
        src = inspect.getsource(event_pipeline._post_result_to_issue)
        # 剥离 docstring：源码前两段三引号之间是 docstring，之后为函数体
        if '"""' in src:
            src = '"""'.join(src.split('"""')[2:])
        # 函数体不允许出现 add_message 调用
        assert "add_message(" not in src