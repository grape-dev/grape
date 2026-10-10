"""M0 project-info 服务端化测试（见 multi-issue-isolation-design.md §7.1）"""
import json
from pathlib import Path
import pytest

from src.config import WORKSPACE_DIR, log


@pytest.fixture(autouse=True)
def _clean_tasks():
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()


def _write_project_info(tmp_path, repo="gitcode:org/test", graph="graph", caps="caps", meta=None):
    """在 tmp 工作区写入服务端权威 project-info，返回 project-info 目录。
    repo 参数接受 repo_key（含 provider 前缀），路径用 workspace_repo_dir 构造。"""
    from src.repo_key import workspace_repo_dir
    pi = Path(workspace_repo_dir(repo)) / "project-info"
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
        from src.repo_key import workspace_repo_dir
        os.remove(os.path.join(workspace_repo_dir("gitcode:org/test"), "project-info", "project-capabilities.md"))
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
    """Option B（issue-219）：prompt 不再内联项目知识基线，由技能通过 grape-cli/API 获取。
    本节直接测 _build_project_info_section，验证其仍产出一致段落（供技能/管理侧复用）。"""

    def test_section_injects_baseline(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path, graph="# Graph", caps="# Caps")
        from src import prompt_builder
        sec = prompt_builder._build_project_info_section("gitcode:org/test", agent_type="issue")
        assert "项目知识基线" in sec
        assert "# Graph" in sec and "# Caps" in sec
        assert "{{PROJECT_INFO_DIR}}" in sec

    def test_section_degraded_without_baseline(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        from src import prompt_builder
        sec = prompt_builder._build_project_info_section("gitcode:org/none", agent_type="issue")
        assert "无项目知识基线" in sec
        assert "降级" in sec

    def test_devops_prompt_not_injecting_baseline(self, tmp_path, monkeypatch):
        """Option B 主验收：build_devops_agent_prompt 输出不再含知识基线段。"""
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path))
        _write_project_info(tmp_path, graph="# Graph", caps="# Caps")
        from src import prompt_builder
        evt = {
            "type": "issue_opened", "source": "gitcode", "repository": "org/test",
            "repo_key": "gitcode:org/test",
            "title": "t", "body": "b", "actor": "a", "issue_id": "#5", "payload": {},
        }
        prompt = prompt_builder.build_devops_agent_prompt(evt, [], "", ["issue"])
        assert "项目知识基线" not in prompt
        assert "{{PROJECT_INFO_DIR}}" not in prompt
