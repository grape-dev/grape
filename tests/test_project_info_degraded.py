"""Issue #1547：codegraph 未建索引时 project-info 应标记 degraded。

覆盖：未索引判定（占位文案 + files 空串双信号）、正常内容防误判、单段缺失不触发、
既有降级分支（二进制缺失 / worktree 失败 / 锁丢失）行为不变。
"""
import json
import os

import pytest


@pytest.fixture(autouse=True)
def _tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr("src.project_info.builder.WORKSPACE_DIR", str(tmp_path))
    yield tmp_path


def _pi_dir(repo_key="gitcode:org/test"):
    from src.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "project-info")


def _patch_codegraph(monkeypatch, explore, files):
    monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
    monkeypatch.setattr(
        "src.project_info.builder._ensure_analysis_worktree",
        lambda repo_key, token="", default_branch="": "/fake/work",
    )
    monkeypatch.setattr("src.project_info.builder._codegraph_explore", explore)
    monkeypatch.setattr("src.project_info.builder._codegraph_files", files)


ARCH_QUERY = "main entry point application startup"


class TestUnindexedDetection:
    def test_all_sections_placeholder_marks_degraded(self, _tmp_workspace, monkeypatch):
        placeholder = "No files indexed. Run codegraph index first"
        _patch_codegraph(monkeypatch, lambda work, q, max_files=10: placeholder, lambda work: placeholder)
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r == {"ok": True, "degraded": True, "reason": "codegraph index missing"}
        pi = _pi_dir()
        assert os.path.isfile(os.path.join(pi, "project-graph.md"))
        assert os.path.isfile(os.path.join(pi, "project-capabilities.md"))
        meta = json.load(open(os.path.join(pi, ".metadata.json"), encoding="utf-8"))
        assert meta.get("degraded") is True
        assert "index missing" in meta.get("note", "")

    def test_normal_sections_not_degraded(self, _tmp_workspace, monkeypatch):
        _patch_codegraph(monkeypatch, lambda work, q, max_files=10: "real content", lambda work: "file-tree")
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r == {"ok": True, "degraded": False, "reason": "codegraph"}
        meta = json.load(open(os.path.join(_pi_dir(), ".metadata.json"), encoding="utf-8"))
        assert meta.get("degraded") is False
        assert meta.get("note") == ""

    def test_single_empty_section_not_degraded(self, _tmp_workspace, monkeypatch):
        def _explore(work, q, max_files=10):
            return "" if q == ARCH_QUERY else "real content"

        _patch_codegraph(monkeypatch, _explore, lambda work: "file-tree")
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is False
        assert r["reason"] == "codegraph"

    def test_empty_files_marks_degraded(self, _tmp_workspace, monkeypatch):
        _patch_codegraph(monkeypatch, lambda work, q, max_files=10: "real content", lambda work: "")
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is True
        assert r["reason"] == "codegraph index missing"

    def test_helper_empty_list_and_mixed_case_marker(self):
        from src.project_info.builder import _codegraph_unindexed
        assert _codegraph_unindexed([]) is False
        assert _codegraph_unindexed(["NO FILES Indexed，无法分析"]) is True
        assert _codegraph_unindexed(["Run CodeGraph Index first"]) is True


class TestExistingDegradedBranchesUnchanged:
    def test_codegraph_unavailable_branch(self, _tmp_workspace, monkeypatch):
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: False)
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r["degraded"] is True
        assert r["reason"] == "codegraph unavailable"
        meta = json.load(open(os.path.join(_pi_dir(), ".metadata.json"), encoding="utf-8"))
        assert "codegraph unavailable" in meta.get("note", "")

    def test_worktree_failed_branch(self, _tmp_workspace, monkeypatch):
        monkeypatch.setattr("src.project_info.builder._codegraph_available", lambda: True)
        monkeypatch.setattr(
            "src.project_info.builder._ensure_analysis_worktree",
            lambda repo_key, token="", default_branch="": None,
        )
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test")
        assert r == {"ok": False, "degraded": True, "reason": "analysis worktree failed"}

    def test_lock_lost_branch(self, _tmp_workspace, monkeypatch):
        _patch_codegraph(monkeypatch, lambda work, q, max_files=10: "real content", lambda work: "file-tree")
        from src.project_info.builder import build_project_info
        r = build_project_info("gitcode:org/test", renew_fn=lambda: False)
        assert r == {"ok": False, "degraded": True, "reason": "lock lost during build"}
        meta = json.load(open(os.path.join(_pi_dir(), ".metadata.json"), encoding="utf-8"))
        assert "lock lost" in meta.get("note", "")