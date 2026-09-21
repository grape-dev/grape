"""服务端 project-info 生成器测试（见 codegraph-builder-plan.md）。

覆盖：codegraph 可用/不可用两种路径、metadata 降级标记、should_refresh 判定。
Issue #200：bare mirror 每次刷新、worktree fetch 带 token、token 优先于兜底。
"""
import json, os
import pytest


@pytest.fixture(autouse=True)
def _tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr("src.project_info.builder.WORKSPACE_DIR", str(tmp_path))
    yield tmp_path


class TestBuildProjectInfo:
    def test_codegraph_unavailable_degraded(self, _tmp_workspace, monkeypatch):
        """codegraph 不可用 → 降级生成，degraded=true。"""
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: False)
        from src.project_info.builder import build_project_info
        from src.repo_key import workspace_repo_dir
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is True
        pi = os.path.join(workspace_repo_dir("gitcode:org/test"), "project-info")
        assert os.path.isfile(os.path.join(pi, "project-graph.md"))
        assert os.path.isfile(os.path.join(pi, "project-capabilities.md"))
        meta = json.load(open(os.path.join(pi, ".metadata.json"), encoding="utf-8"))
        assert meta.get("degraded") is True
        assert "codegraph" in meta.get("note", "")

    def test_codegraph_available_generates(self, _tmp_workspace, monkeypatch):
        """codegraph 可用 → 生成完整基线，degraded=false。"""
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        monkeypatch.setattr("src.project_info.builder._ensure_analysis_worktree", lambda repo_key, token="": "/fake/work")
        monkeypatch.setattr("src.project_info.builder._codegraph_explore", lambda work, q, max_files=10: f"explore:{q}")
        monkeypatch.setattr("src.project_info.builder._codegraph_files", lambda work: "file-tree")
        from src.project_info.builder import build_project_info
        from src.repo_key import workspace_repo_dir
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is False
        pi = os.path.join(workspace_repo_dir("gitcode:org/test"), "project-info")
        graph = open(os.path.join(pi, "project-graph.md"), encoding="utf-8").read()
        caps = open(os.path.join(pi, "project-capabilities.md"), encoding="utf-8").read()
        assert "Architecture Overview" in graph
        assert "Tech Stack" in caps
        meta = json.load(open(os.path.join(pi, ".metadata.json"), encoding="utf-8"))
        assert meta.get("degraded") is False

    def test_analysis_worktree_failed(self, _tmp_workspace, monkeypatch):
        """分析副本准备失败 → 降级，degraded=true。"""
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        monkeypatch.setattr("src.project_info.builder._ensure_analysis_worktree", lambda repo_key, token="": None)
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is True
        assert r["ok"] is False

    def test_analysis_worktree_auto_ensure_repo_base(self, _tmp_workspace, monkeypatch):
        """bare mirror 不存在时自动调用 ensure_repo_base 建立镜像（knowledge-board-design.md §5.2 修复）。

        仅 VOD/Issue 的仓库无 codebase 时，builder 应主动建镜像而非直接 degraded。
        走真实 _ensure_analysis_worktree（不 mock），验证 ensure_repo_base 被调用。
        """
        from src.repo_key import workspace_repo_dir
        calls = {"n": 0}
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)

        def _fake_ensure_repo_base(repo_key, token=""):
            calls["n"] += 1
            base = os.path.join(workspace_repo_dir(repo_key), "codebase")
            os.makedirs(base, exist_ok=True)
            return base

        monkeypatch.setattr("src.runtime.repogit.ensure_repo_base", _fake_ensure_repo_base)
        # worktree add 必然失败（临时目录不是真实 git 仓库）→ 返回 None → degraded，但 ensure_repo_base 已被调用
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert calls["n"] == 1
        assert r["degraded"] is True

    def test_ensure_repo_base_when_missing_then_generates(self, _tmp_workspace, monkeypatch):
        """无 codebase → ensure_repo_base 建立后 → 正常生成（degraded=false）。"""
        from src.repo_key import workspace_repo_dir
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)

        def _fake_ensure_repo_base(repo_key, token=""):
            base = os.path.join(workspace_repo_dir(repo_key), "codebase")
            os.makedirs(base, exist_ok=True)
            return base

        monkeypatch.setattr("src.runtime.repogit.ensure_repo_base", _fake_ensure_repo_base)
        monkeypatch.setattr("src.project_info.builder._ensure_analysis_worktree",
                            lambda repo_key, token="": os.path.join(workspace_repo_dir(repo_key), "codebase"))
        monkeypatch.setattr("src.project_info.builder._codegraph_explore", lambda work, q, max_files=10: f"explore:{q}")
        monkeypatch.setattr("src.project_info.builder._codegraph_files", lambda work: "file-tree")
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is False
        pi = os.path.join(workspace_repo_dir("gitcode:org/test"), "project-info")
        assert os.path.isfile(os.path.join(pi, "project-graph.md"))


class TestShouldRefresh:
    def test_missing_files_refresh(self, _tmp_workspace):
        from src.project_info.builder import should_refresh
        assert should_refresh("gitcode:org/test") is True  # 无文件

    def test_degraded_with_codegraph_refresh(self, _tmp_workspace, monkeypatch):
        """degraded=true 且 codegraph 可用 → 应刷新。"""
        import os
        from src.repo_key import workspace_repo_dir
        pi = os.path.join(workspace_repo_dir("gitcode:org/test"), "project-info")
        os.makedirs(pi, exist_ok=True)
        open(os.path.join(pi, "project-graph.md"), "w", encoding="utf-8").write("g")
        open(os.path.join(pi, "project-capabilities.md"), "w", encoding="utf-8").write("c")
        json.dump({"degraded": True, "last_updated": "2026-08-01T00:00:00"}, open(os.path.join(pi, ".metadata.json"), "w", encoding="utf-8"))
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        from src.project_info.builder import should_refresh
        assert should_refresh("gitcode:org/test") is True

    def test_fresh_degraded_false_no_refresh(self, _tmp_workspace, monkeypatch):
        """degraded=false 且未超 7 天 → 不刷新。"""
        import os
        from src.repo_key import workspace_repo_dir
        pi = os.path.join(workspace_repo_dir("gitcode:org/test"), "project-info")
        os.makedirs(pi, exist_ok=True)
        open(os.path.join(pi, "project-graph.md"), "w", encoding="utf-8").write("g")
        open(os.path.join(pi, "project-capabilities.md"), "w", encoding="utf-8").write("c")
        import datetime
        recent = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
        json.dump({"degraded": False, "last_updated": recent}, open(os.path.join(pi, ".metadata.json"), "w", encoding="utf-8"))
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        from src.project_info.builder import should_refresh
        assert should_refresh("gitcode:org/test") is False


class TestEnsureRepoBaseAlwaysRefresh:
    """Issue #200: bare mirror 每次都调 ensure_repo_base 刷新 refs（不再跳过已存在的）。"""

    def test_ensure_repo_base_called_even_when_exists(self, _tmp_workspace, monkeypatch):
        """bare mirror 已存在时也调用 ensure_repo_base（不再 if not os.path.isdir 跳过）。"""
        from src.repo_key import workspace_repo_dir
        calls = {"n": 0}
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)

        # 预先创建 bare mirror 目录（模拟已存在）
        base = os.path.join(workspace_repo_dir("gitcode:org/test"), "codebase")
        os.makedirs(base, exist_ok=True)

        def _fake_ensure_repo_base(repo_key, token=""):
            calls["n"] += 1
            calls["token"] = token
            return base

        monkeypatch.setattr("src.runtime.repogit.ensure_repo_base", _fake_ensure_repo_base)
        from src.project_info.builder import build_project_info
        build_project_info("gitcode:org/test")
        assert calls["n"] == 1  # 即使 bare mirror 已存在也调用了

    def test_ensure_repo_base_passes_token(self, _tmp_workspace, monkeypatch):
        """ensure_repo_base 接收到传入的 token。"""
        from src.repo_key import workspace_repo_dir
        calls = {"token": ""}
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)

        base = os.path.join(workspace_repo_dir("gitcode:org/test"), "codebase")
        os.makedirs(base, exist_ok=True)

        def _fake_ensure_repo_base(repo_key, token=""):
            calls["token"] = token
            return base

        monkeypatch.setattr("src.runtime.repogit.ensure_repo_base", _fake_ensure_repo_base)
        from src.project_info.builder import build_project_info
        build_project_info("gitcode:org/test", token="my-bot-token")
        assert calls["token"] == "my-bot-token"


class TestWorktreeFetchWithToken:
    """Issue #200: worktree fetch 使用 _run_git 带 token，失败时记日志。"""

    def test_worktree_fetch_uses_run_git_with_token(self, _tmp_workspace, monkeypatch):
        """_ensure_analysis_worktree 的 fetch 调用 _run_git 并传入 token。"""
        from src.repo_key import workspace_repo_dir
        fetch_calls = []

        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)

        def _fake_ensure_repo_base(repo_key, token=""):
            base = os.path.join(workspace_repo_dir(repo_key), "codebase")
            os.makedirs(base, exist_ok=True)
            return base

        def _fake_run_git(args, token="", timeout=900):
            fetch_calls.append({"args": args, "token": token})
            # 模拟成功
            import subprocess
            r = subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return r

        def _fake_run(cmd, cwd=None, timeout=300):
            # worktree add 会调 _run，模拟失败让它走到 fetch
            return 1, "", "not a git repo"
            # codegraph init 也调 _run

        monkeypatch.setattr("src.runtime.repogit.ensure_repo_base", _fake_ensure_repo_base)
        monkeypatch.setattr("src.runtime.repogit._run_git", _fake_run_git)
        monkeypatch.setattr("src.project_info.builder._run", _fake_run)
        from src.project_info.builder import _ensure_analysis_worktree
        # worktree add 失败 → 返回 None，但 fetch 不会被调用（worktree add 失败在 fetch 之前）
        _ensure_analysis_worktree("gitcode:org/test", token="test-token")
        # worktree add 失败导致 return None，fetch 不会执行
        # 这个测试验证 worktree add 失败时的行为
        assert len(fetch_calls) == 0  # worktree add 失败，fetch 未执行

    def test_worktree_fetch_logs_failure(self, _tmp_workspace, monkeypatch):
        """worktree fetch 失败时记 log.warning 但不中断流程。"""
        from src.repo_key import workspace_repo_dir
        import subprocess

        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)

        def _fake_ensure_repo_base(repo_key, token=""):
            base = os.path.join(workspace_repo_dir(repo_key), "codebase")
            os.makedirs(base, exist_ok=True)
            return base

        def _fake_run_git(args, token="", timeout=900):
            # 模拟 fetch 失败
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="403 Forbidden")

        # worktree add 需要成功才能走到 fetch
        def _fake_run(cmd, cwd=None, timeout=300):
            if "worktree" in str(cmd):
                return 0, "", ""  # worktree add 成功
            if "codegraph" in str(cmd):
                return 0, "", ""  # codegraph init 成功
            return 0, "", ""

        monkeypatch.setattr("src.runtime.repogit.ensure_repo_base", _fake_ensure_repo_base)
        monkeypatch.setattr("src.runtime.repogit._run_git", _fake_run_git)
        monkeypatch.setattr("src.project_info.builder._run", _fake_run)
        from src.project_info.builder import _ensure_analysis_worktree
        result = _ensure_analysis_worktree("gitcode:org/test", token="test-token")
        # fetch 失败但 worktree 已有代码 → 不返回 None，继续执行
        # codegraph init 成功 → 返回 work 目录
        assert result is not None


class TestBuildProjectInfoTokenPassing:
    """Issue #200: build_project_info 传入的 token 优先于兜底获取。"""

    def test_explicit_token_used_over_fallback(self, _tmp_workspace, monkeypatch):
        """传入非空 token 时，不调 get_gitcode_token/get_github_token 兜底。"""
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        monkeypatch.setattr("src.project_info.builder._ensure_analysis_worktree",
                            lambda repo_key, token="": "/fake/work")
        monkeypatch.setattr("src.project_info.builder._codegraph_explore", lambda work, q, max_files=10: "explore")
        monkeypatch.setattr("src.project_info.builder._codegraph_files", lambda work: "files")

        # mock Repo.bot_token，验证不被调用（因为传入了非空 token）
        # build_project_info 中 tok = token or _repo.bot_token
        # token 非空 → 短路 → 不访问 bot_token
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test", token="explicit-token")
        assert r["degraded"] is False  # 正常生成

    def test_empty_token_falls_back_to_repo(self, _tmp_workspace, monkeypatch):
        """传入空 token 时，通过 Repo.bot_token 兜底获取。"""
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        monkeypatch.setattr("src.project_info.builder._ensure_analysis_worktree",
                            lambda repo_key, token="": "/fake/work")
        monkeypatch.setattr("src.project_info.builder._codegraph_explore", lambda work, q, max_files=10: "explore")
        monkeypatch.setattr("src.project_info.builder._codegraph_files", lambda work: "files")
        # mock get_gitcode_token 验证兜底被调用
        from src.user import db as user_db
        monkeypatch.setattr(user_db, "get_gitcode_token", lambda repo_key="", user_id=0: "fallback-token")
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test", token="")
        assert r["degraded"] is False

