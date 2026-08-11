"""M0 project-info 服务端化测试（见 multi-issue-isolation-design.md §7.1）"""
import json
import pytest

from src.config import WORKSPACE_DIR, log


@pytest.fixture(autouse=True)
def _clean_tasks():
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()


def _write_project_info(tmp_path, repo="org/test", graph="graph", caps="caps", meta=None):
    """在 tmp 工作区写入服务端权威 project-info，返回 project-info 目录"""
    pi = tmp_path / repo / "project-info"
    pi.mkdir(parents=True, exist_ok=True)
    (pi / "project-graph.md").write_text(graph, encoding="utf-8")
    (pi / "project-capabilities.md").write_text(caps, encoding="utf-8")
    if meta is not None:
        (pi / ".metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return pi


class TestLoadProjectInfo:
    def test_loads_files_and_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path, meta={"last_commit_sha": "abc123", "last_updated": "2026-08-09"})
        from src.runtime import service
        files, meta = service.load_project_info("gitcode:org/test")
        assert files["project-graph.md"] == "graph"
        assert files["project-capabilities.md"] == "caps"
        assert json.loads(files[".metadata.json"])["last_commit_sha"] == "abc123"
        assert meta["last_commit_sha"] == "abc123"

    def test_no_baseline_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        from src.runtime import service
        files, meta = service.load_project_info("gitcode:org/none")
        assert files == {} and meta == {}

    def test_missing_one_file_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path)
        from src.runtime import service
        files, _ = service.load_project_info("gitcode:org/test")
        # 删除 capabilities 后只打包存在的文件
        import os
        os.remove(os.path.join(str(tmp_path), "org", "test", "project-info", "project-capabilities.md"))
        files2, _ = service.load_project_info("gitcode:org/test")
        assert "project-graph.md" in files2
        assert "project-capabilities.md" not in files2

    def test_repo_key_without_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path)
        from src.runtime import service
        files, _ = service.load_project_info("org/test")
        assert "project-graph.md" in files


class TestEnqueueProjectInfo:
    def test_enqueue_packs_project_info(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path, meta={"last_commit_sha": "deadbeef"})
        from src.runtime import service
        tid = service.enqueue_task(
            agent_session_id="sess-1", runtime_id=1, prompt="p", event_seq=1,
            issue_id="#5", repo_key="gitcode:org/test", agent_type="issue",
            skill="issue",
        )
        t = service.get_task(tid)
        pif = json.loads(t["project_info_files"])
        assert "project-graph.md" in pif and "project-capabilities.md" in pif
        pim = json.loads(t["project_info_meta"])
        assert pim["last_commit_sha"] == "deadbeef"

    def test_enqueue_without_baseline_no_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        from src.runtime import service
        tid = service.enqueue_task(
            agent_session_id="sess-1", runtime_id=1, prompt="p", event_seq=1,
            issue_id="#5", repo_key="gitcode:org/none", agent_type="issue",
            skill="issue",
        )
        t = service.get_task(tid)
        assert json.loads(t["project_info_files"]) == {}
        assert json.loads(t["project_info_meta"]) == {}

    def test_explicit_project_info_override(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        from src.runtime import service
        tid = service.enqueue_task(
            agent_session_id="sess-1", runtime_id=1, prompt="p", event_seq=1,
            issue_id="#5", repo_key="gitcode:org/test", agent_type="issue",
            skill="issue",
            project_info_files={"project-graph.md": "custom"}, project_info_meta={"last_commit_sha": "x"},
        )
        t = service.get_task(tid)
        assert json.loads(t["project_info_files"])["project-graph.md"] == "custom"


class TestPromptProjectInfoSection:
    def test_prompt_injects_baseline(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path, graph="# Graph", caps="# Caps")
        from src import prompt_builder
        evt = {
            "type": "issue_opened", "source": "gitcode", "repository": "org/test",
            "title": "t", "body": "b", "actor": "a", "issue_id": "#5", "payload": {},
        }
        prompt = prompt_builder.build_prompt(evt, [], "", ["issue"])
        assert "项目知识基线" in prompt
        assert "# Graph" in prompt and "# Caps" in prompt
        assert "{{PROJECT_INFO_DIR}}" in prompt

    def test_prompt_degraded_without_baseline(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        from src import prompt_builder
        evt = {
            "type": "issue_opened", "source": "gitcode", "repository": "org/none",
            "title": "t", "body": "b", "actor": "a", "issue_id": "#5", "payload": {},
        }
        prompt = prompt_builder.build_prompt(evt, [], "", ["issue"])
        assert "无项目知识基线" in prompt
        assert "降级" in prompt
