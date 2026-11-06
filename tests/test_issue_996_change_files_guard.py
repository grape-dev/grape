"""Issue #996：变更事件文件清单 + 产物清理守卫影响预检。

覆盖五类场景：
1. payload 自带 files：push / pull_request_merged / pull_request_updated 归一；
2. API 兜底补齐（fetcher 注入模拟 provider files 接口）；
3. 超阈值截断：truncated=true 且 deleted 全量保留；
4. 守卫脚本引用待删路径：清理跳过删除并标注 guard_impact{breaking=true}；
5. 无守卫引用：清理照常、不产生 guard_impact 字段。
"""
import json

from src.vcs.gitcode import GitCodeBackend
from src.vcs.github import GitHubBackend
from src.vcs.gitlab import GitLabBackend
from src.vcs.event_types import normalize_files
from src.vcs.files_enrich import enrich_event_files
from src.vcs.guard_impact import scan_guard_impact, is_guard_script
from src.runtime.cleanup import clean_issue_artifacts


def _raw(payload):
    return json.dumps(payload).encode("utf-8")


def _push_payload():
    return {
        "ref": "refs/heads/feat/issue-996",
        "project": {"path_with_namespace": "owner/repo"},
        "user_name": "bot",
        "after": "abc123",
        "commits": [
            {
                "id": "c1",
                "added": ["docs/issue-996/spec.md"],
                "modified": ["src/app.py"],
                "removed": ["docs/issue-996/old.md"],
            }
        ],
    }


def test_push_payload_carries_files():
    evt = GitCodeBackend().normalize(_raw(_push_payload()), "push")
    files = evt["payload"]["files"]
    assert [f["path"] for f in files["added"]] == ["docs/issue-996/spec.md"]
    assert [f["path"] for f in files["modified"]] == ["src/app.py"]
    assert [f["path"] for f in files["deleted"]] == ["docs/issue-996/old.md"]
    assert files["truncated"] is False


def test_pr_merged_updated_payload_and_platform_normalize():
    # GitCode merge hook：object_attributes + files
    gc = GitCodeBackend().normalize(
        _raw({
            "project": {"path_with_namespace": "owner/repo"},
            "object_attributes": {"action": "merge", "title": "PR", "iid": 5},
            "files": [
                {"filename": "docs/issue-996/plan.md", "status": "removed"},
                {"filename": "src/b.py", "status": "modified"},
            ],
        }),
        "Merge Request Hook",
    )
    assert gc["type"] == "pull_request_merged"
    assert [f["path"] for f in gc["payload"]["files"]["deleted"]] == ["docs/issue-996/plan.md"]
    assert [f["path"] for f in gc["payload"]["files"]["modified"]] == ["src/b.py"]

    # GitHub pull_request synchronize：commits 自带 removed
    gh = GitHubBackend().normalize(
        _raw({
            "action": "synchronize",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"title": "PR", "number": 5, "merged": False},
            "commits": [{"added": [], "modified": ["x.py"], "removed": ["docs/issue-996/task.md"]}],
            "sender": {"login": "bob"},
        }),
        "pull_request",
    )
    assert gh["type"] == "pull_request_updated"
    assert [f["path"] for f in gh["payload"]["files"]["deleted"]] == ["docs/issue-996/task.md"]

    # GitLab merge_request update：changes 列表
    gl = GitLabBackend().normalize(
        _raw({
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "owner/repo"},
            "object_attributes": {"action": "update", "title": "MR", "iid": 7},
            "changes": [
                {"old_path": "docs/issue-996/spec.md", "new_path": "docs/issue-996/spec2.md"},
                {"old_path": "src/c.py", "new_path": "src/d.py"},
            ],
        }),
        "Merge Request Hook",
    )
    assert gl["type"] == "pull_request_updated"
    assert "docs/issue-996/spec.md" in [f["path"] for f in gl["payload"]["files"]["deleted"]]


def test_api_fallback_fills_files():
    evt = {
        "type": "pull_request_merged",
        "source": "gitcode",
        "payload": {
            "project": {"path_with_namespace": "owner/repo"},
            "object_attributes": {"action": "merge", "iid": 12},
        },
    }

    def _fetcher(source, payload, repo_key):
        return [
            {"filename": "docs/issue-996/spec.md", "status": "removed"},
            {"filename": "src/new.py", "status": "added"},
        ]

    files = enrich_event_files(evt, "gitcode:owner/repo", fetcher=_fetcher)
    assert [f["path"] for f in files["deleted"]] == ["docs/issue-996/spec.md"]
    assert evt["payload"]["files"]["deleted"][0]["path"] == "docs/issue-996/spec.md"
    assert [f["path"] for f in evt["payload"]["files"]["added"]] == ["src/new.py"]


def test_truncation_keeps_all_deleted():
    commits = [{
        "added": [f"docs/issue-996/f{i}.md" for i in range(260)],
        "modified": [f"src/m{i}.py" for i in range(20)],
        "removed": [f"docs/issue-996/old{i}.md" for i in range(10)],
    }]
    files = normalize_files(commits, limit=200)
    assert files["truncated"] is True
    assert len(files["added"]) == 200
    assert len(files["deleted"]) == 10
    assert files["deleted"][-1]["path"] == "docs/issue-996/old9.md"


def test_guard_hit_skips_cleanup_and_marks_impact():
    removed = []
    scripts = {
        "scripts/check-docs.sh": "test -f docs/issue-996/spec.md\n",
        "tests/check_other.sh": "echo ok\n",
    }
    payload = {}
    result = clean_issue_artifacts(
        "gitcode:owner/repo", "weekly-mys", 996,
        targets=["docs/issue-996/spec.md", "docs/issue-996/plan.md"],
        scripts=scripts, remover=lambda items: removed.extend(items), payload=payload,
    )
    assert payload["guard_impact"]["breaking"] is True
    assert removed == []
    assert result["deleted"] == []
    assert result["guard_impact"]["breaking"] is True
    assert result["guard_impact"]["scripts"] == ["scripts/check-docs.sh"]
    assert "docs/issue-996/spec.md" in result["skipped"]


def test_no_guard_hit_cleans_normally_without_impact_field():
    removed = []
    scripts = {"scripts/check-other.sh": "grep docs/issue-1/spec.md\n"}
    result = clean_issue_artifacts(
        "gitcode:owner/repo", "weekly-mys", 996,
        targets=["docs/issue-996/spec.md"],
        scripts=scripts, remover=lambda items: removed.extend(items),
    )
    assert [p for p in removed] == ["docs/issue-996/spec.md"]
    assert result["deleted"] == ["docs/issue-996/spec.md"]
    assert "guard_impact" not in result


def test_guard_impact_helpers():
    assert is_guard_script("scripts/check-all.sh")
    assert is_guard_script("tests/check-foo.py")
    assert not is_guard_script("scripts/deploy.sh")
    impact = scan_guard_impact(
        ["docs/issue-996/spec.md"],
        {"tests/check-docs.sh": "cat docs/issue-996/*", "scripts/check-all.sh": "echo ok"},
    )
    assert impact == {"scripts": ["tests/check-docs.sh"], "breaking": True}