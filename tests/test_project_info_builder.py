"""服务端 project-info 生成器测试（见 codegraph-builder-plan.md）。

覆盖：codegraph 可用/不可用两种路径、metadata 降级标记、should_refresh 判定。
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
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is True
        pi = os.path.join(str(_tmp_workspace), "org", "test", "project-info")
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
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is False
        pi = os.path.join(str(_tmp_workspace), "org", "test", "project-info")
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


class TestShouldRefresh:
    def test_missing_files_refresh(self, _tmp_workspace):
        from src.project_info.builder import should_refresh
        assert should_refresh("gitcode:org/test") is True  # 无文件

    def test_degraded_with_codegraph_refresh(self, _tmp_workspace, monkeypatch):
        """degraded=true 且 codegraph 可用 → 应刷新。"""
        import os
        pi = os.path.join(str(_tmp_workspace), "org", "test", "project-info")
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
        pi = os.path.join(str(_tmp_workspace), "org", "test", "project-info")
        os.makedirs(pi, exist_ok=True)
        open(os.path.join(pi, "project-graph.md"), "w", encoding="utf-8").write("g")
        open(os.path.join(pi, "project-capabilities.md"), "w", encoding="utf-8").write("c")
        import datetime
        recent = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
        json.dump({"degraded": False, "last_updated": recent}, open(os.path.join(pi, ".metadata.json"), "w", encoding="utf-8"))
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        from src.project_info.builder import should_refresh
        assert should_refresh("gitcode:org/test") is False
