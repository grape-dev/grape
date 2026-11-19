"""Issue #3168 技能详情页：摘要 / 版本 / 文件清单 / 相关技能 纯逻辑与装配测试。

覆盖：
- 摘要清洗：禁「→」「->」链式符号、压空白、50 字截断；
- 版本比较与递增、显式版本不大于历史时的 400 拦截；
- zip 文件清单解析（去公共顶层目录、保留人类可读大小原始字节数）；
- 手工摘要含箭头时拒绝；
- by-id 聚合装饰器字段（D2/D3/D5/D6/D9/D11）；
- 新表 DDL 与迁移登记（runtime 域）。
"""
import io
import zipfile

import pytest
from fastapi import HTTPException

from src.domains.mgmt.routes import skills as skills_route
from src.api import db_setup


def test_clean_summary_strips_arrow_chain_and_truncates():
    raw = "按 Spec 实现——先协商测试 seams→TDD 红-绿→bug 先建反馈环→建 PR→交棒 QA"
    out = skills_route._clean_summary(raw)
    assert "→" not in out and "->" not in out
    assert out.startswith("按 Spec 实现")
    assert len(out) <= 50


def test_clean_summary_collapses_whitespace():
    assert skills_route._clean_summary("  一句话\n说明   用途  ") == "一句话 说明 用途"


def test_validate_manual_summary_rejects_arrow():
    with pytest.raises(HTTPException) as ei:
        skills_route._validate_manual_summary("先做 A→再做 B")
    assert ei.value.status_code == 400


def test_validate_manual_summary_accepts_plain_sentence():
    assert skills_route._validate_manual_summary("按需求单逐切片实现代码并建 PR") == "按需求单逐切片实现代码并建 PR"


def test_version_compare_and_bump():
    assert skills_route._version_gt("1.0.1", "1.0.0")
    assert skills_route._version_gt("1.2", "1.1.9")
    assert not skills_route._version_gt("1.0.0", "1.0.0")
    assert skills_route._bump_patch("1.0.0") == "1.0.1"
    assert skills_route._bump_patch("2.3") == "2.3.1"


def test_list_zip_entries_strips_common_root_and_keeps_sizes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo/SKILL.md", "hello")
        zf.writestr("demo/references/api.md", "0123456789")
        zf.writestr("demo/", "")
    entries = dict(skills_route._list_zip_entries(buf.getvalue()))
    assert entries == {"SKILL.md": 5, "references/api.md": 10}


def test_decorate_skill_detail_exposes_detail_fields(monkeypatch):
    rows = {
        "skill_detail_extra_by_id": ("一句话摘要", 7, "2026-01-01 00:00:00",
                                     "2026-02-02 00:00:00", "alice", "bob"),
        "skill_version_latest": ("1.2.0", "2026-02-02 00:00:00", "alice", "修复"),
        "skill_comment_count": (3,),
        "skills_related_pool": [],
    }
    monkeypatch.setattr(skills_route._rt_skills, "read_one_any",
                        lambda name, params=(): rows.get(name))
    monkeypatch.setattr(skills_route._rt_skills, "read_all_any",
                        lambda name, params=(): [
                            ("1.2.0", "2026-02-02 00:00:00", "alice", "修复"),
                            ("1.1.0", "2026-01-01 00:00:00", "alice", "初版"),
                        ] if name == "skill_versions_by_skill" else [])
    monkeypatch.setattr(skills_route, "_ensure_official_skill_files",
                        lambda sid, scope, content: [("SKILL.md", 12)])
    payload = skills_route._decorate_skill_detail(
        {"id": 9, "description": "长描述→流程", "agent_type": "coding",
         "tags": ["TDD"], "scope": "user"},
        {"id": "u1"}, "user", "")
    assert payload["summary"] == "一句话摘要"
    assert payload["download_count"] == 7
    assert payload["updated_at"].startswith("2026-02-02")
    assert payload["created_by"] == "alice"
    assert payload["version"] == "1.2.0"
    assert [v["version"] for v in payload["versions"]] == ["1.2.0", "1.1.0"]
    assert payload["versions"][0]["is_latest"] is True
    assert payload["file_count"] == 1
    assert payload["comment_count"] == 3


def test_skill_detail_tables_registered_and_migrations_are_runtime():
    names = {n for n, _ddl in db_setup._NEW_TABLES}
    for n in ("grape_t_skill_version", "grape_t_skill_file", "grape_t_skill_comment"):
        assert n in names
    versions = {m.version: m for m in db_setup._MIGRATIONS}
    assert versions["202611181301"].domains == ("runtime",)
    assert versions["202611181302"].domains == ("runtime",)
    assert versions["202611181302"].kind == "backfill"
