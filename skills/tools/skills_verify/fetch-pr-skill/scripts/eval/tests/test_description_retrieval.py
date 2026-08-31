#!/usr/bin/env python3
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.description_retrieval import (
    extract_triggers,
    extract_triggers_from_content,
    build_skill_index,
    retrieval_hit_count,
    score_retrieval,
    load_default_repo,
    repo_cache_path,
)


def _make_skill_file(content):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_extract_triggers():
    path = _make_skill_file(
        "---\nname: test-skill\ndescription: >-\n  Test skill.\n"
        "  Triggers include: alpha, beta, 测试触发词,\n  gamma\n---\n"
    )
    triggers = extract_triggers(path)
    assert "alpha" in triggers
    assert "beta" in triggers
    assert "测试触发词" in triggers
    assert "gamma" in triggers


def test_extract_triggers_no_triggers_section():
    path = _make_skill_file("---\nname: test-skill\ndescription: no triggers here\n---\n")
    assert extract_triggers(path) == []


def test_extract_triggers_from_content():
    content = (
        "---\nname: test-skill\ndescription: >-\n  Test skill.\n"
        "  Triggers include: alpha, beta, 测试触发词,\n  gamma\n---\n"
    )
    triggers = extract_triggers_from_content(content)
    assert "alpha" in triggers
    assert "beta" in triggers
    assert "测试触发词" in triggers
    assert "gamma" in triggers


def test_extract_triggers_from_content_bare_colon():
    content = (
        "---\nname: test-skill\ndescription: >-\n  Test skill.\n"
        "  Triggers: alpha, beta,\n  gamma\n---\n"
    )
    triggers = extract_triggers_from_content(content)
    assert "alpha" in triggers
    assert "beta" in triggers
    assert "gamma" in triggers


def test_extract_triggers_from_content_no_match():
    assert extract_triggers_from_content("no triggers here") == []


def test_build_skill_index():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "skills", "skill-a"))
    os.makedirs(os.path.join(d, "skills", "skill-b"))
    with open(os.path.join(d, "skills", "skill-a", "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("skill a content")
    with open(os.path.join(d, "skills", "skill-b", "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("skill b content")
    index = build_skill_index(d)
    assert set(index.keys()) == {"skill-a", "skill-b"}


def test_build_skill_index_missing_dir():
    assert build_skill_index("/nonexistent/path") == {}


def test_retrieval_hit_count():
    index = {
        "skill-a": "contains alpha and 测试词",
        "skill-b": "contains beta",
        "skill-c": "other",
    }
    # 触发词 alpha 命中 skill-a（当前），beta 命中 skill-b（非当前），gamma 无人命中
    count = retrieval_hit_count(["alpha", "beta", "gamma"], "skill-a", index)
    assert count == 1  # 只有 alpha 检索回 skill-a


def test_retrieval_hit_count_empty_index():
    assert retrieval_hit_count(["alpha"], "skill-a", {}) == 0


def test_score_retrieval():
    assert score_retrieval(5) == 2.0
    assert score_retrieval(4) == 2.0
    assert score_retrieval(3) == 1.0
    assert score_retrieval(1) == 1.0
    assert score_retrieval(0) == 0.0


def test_load_default_repo():
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "obs-cred.conf")
    with open(conf, "w") as f:
        f.write("OBS_AK=x\nDEFAULT_REPO=huaweiskilldev/huaweicloud-skillsdev\n")
    with mock.patch("eval.description_retrieval.DEFAULT_REPO_CONF", conf):
        assert load_default_repo() == "huaweiskilldev/huaweicloud-skillsdev"


def test_load_default_repo_missing_key():
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "obs-cred.conf")
    with open(conf, "w") as f:
        f.write("OBS_AK=x\n")
    with mock.patch("eval.description_retrieval.DEFAULT_REPO_CONF", conf):
        assert load_default_repo() is None


def test_load_default_repo_missing_file():
    with mock.patch("eval.description_retrieval.DEFAULT_REPO_CONF", "/nonexistent/obs-cred.conf"):
        assert load_default_repo() is None


def test_repo_cache_path_uses_underscore():
    assert repo_cache_path("huaweiskilldev/huaweicloud-skillsdev") == (
        "/tmp/fetch-pr-skill/huaweiskilldev_huaweicloud-skillsdev"
    )


def test_repo_cache_path_matches_pr_path_rule():
    owner, repo = "huaweiskilldev", "huaweicloud-skillsdev"
    assert repo_cache_path(f"{owner}/{repo}") == f"/tmp/fetch-pr-skill/{owner}_{repo}"
