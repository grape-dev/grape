"""Tests for skill discovery and frontmatter parsing."""
from gitsync.skills import find_skills


def test_find_skills_nested(tmp_path):
    skills = tmp_path / "skills"
    (skills / "ai" / "maas").mkdir(parents=True)
    skill_dir = skills / "ai" / "maas" / "huawei-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: huawei-skill\ndescription: test skill\n---\nbody"
    )
    # a dir without SKILL.md should be ignored
    (skills / "ai" / "maas" / "not-a-skill").mkdir()
    (skills / "ai" / "maas" / "not-a-skill" / "foo.txt").write_text("x")

    found = find_skills(skills)
    assert len(found) == 1
    assert found[0].name == "huawei-skill"
    assert found[0].folder.name == "huawei-skill"


def test_name_falls_back_to_dir_name(tmp_path):
    skills = tmp_path / "skills"
    d = skills / "no-frontmatter"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# plain markdown, no frontmatter")
    found = find_skills(skills)
    assert found[0].name == "no-frontmatter"


def test_no_skills(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_skills(empty) == []
