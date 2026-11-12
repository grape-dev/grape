"""接管 prompt 的资产点名（§8 ①② 缺口修复）：项目图谱要点名、沉淀知识要有/无都说清。"""
from __future__ import annotations

from src.runtime.takeover import render_takeover_prompt as R


def _render(project_files: list[str] | None = None,
            knowledge_files: list[str] | None = None) -> str:
    return R(
        repo='a/b', issue_number='7', agent_type='coding', skill='implementation',
        cancel_ok=True, cancel_error='', task_id='t_x', workspace_created=False,
        cwd='/tmp/ws', skill_ok=True, has_references=True, messages=[], original_prompt='do it',
        project_files=project_files, knowledge_files=knowledge_files,
    )


def test_project_graph_named_when_present():
    p = _render(project_files=['project-graph.md', 'project-capabilities.md', '.metadata.json'])
    assert '4) 项目图谱已放在工作目录根部' in p
    assert 'project-graph.md' in p
    assert 'project-capabilities.md' in p
    assert '.metadata.json' in p
    assert '开工前先读前两个' in p


def test_project_graph_item_absent_when_empty():
    assert '4) 项目图谱' not in _render()


def test_knowledge_empty_is_stated():
    p = _render(project_files=['project-graph.md'])
    assert '## 项目知识（已沉淀）' in p
    assert 'assets.knowledge` 为空' in p
    assert '以上面的项目图谱为准' in p


def test_knowledge_empty_falls_back_without_graph():
    p = _render()
    assert 'assets.knowledge` 为空' in p
    assert '读 README / 目录结构' in p


def test_knowledge_files_listed_when_present():
    p = _render(project_files=['project-graph.md'],
                knowledge_files=['member/architecture.md', 'member/conventions.md'])
    sec = p[p.index('## 项目知识'):]
    assert '`member/architecture.md`' in sec
    assert '`member/conventions.md`' in sec
    assert 'assets.knowledge` 为空' not in p