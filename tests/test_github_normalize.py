"""GitHub Webhook 事件归一化单元测试"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.vcs.github import GitHubBackend


def make_payload(event_type: str, overrides: dict = None) -> tuple[bytes, dict]:
    """构造模拟 GitHub Webhook 请求"""
    base = {
        "push": {
            "ref": "refs/heads/main",
            "repository": {"full_name": "org/repo", "name": "repo"},
            "sender": {"login": "alice"},
            "commits": [{"message": "fix bug"}],
        },
        "issues": {
            "action": "opened",
            "issue": {"number": 42, "title": "Bug", "user": {"login": "bob"}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bob"},
        },
        "pull_request": {
            "action": "opened",
            "pull_request": {"number": 128, "title": "Fix", "base": {"ref": "main"}},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        },
        "release": {
            "action": "published",
            "release": {"tag_name": "v1.0", "name": "v1.0"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        },
        "issue_comment": {
            "action": "created",
            "issue": {"number": 42, "user": {"login": "bob"}},
            "comment": {"body": "LGTM"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bob"},
        },
    }

    body = base.get(event_type, {"repository": {"full_name": "org/repo"}, "sender": {"login": "test"}})
    if overrides:
        body.update(overrides)

    return json.dumps(body).encode(), body


backend = GitHubBackend()

tests = [
    ("push", "push", "org/repo", "main", "alice"),
    ("issues", "issue_opened", "org/repo", "", "bob"),
    ("pull_request", "pull_request_opened", "org/repo", "main", "alice"),
    ("release", "release_created", "org/repo", "", "alice"),
    ("issue_comment", "issue_comment", "org/repo", "", "bob"),
]

passed = 0
for header, expected_type, expected_repo, expected_branch, expected_actor in tests:
    raw, _ = make_payload(header)
    result = backend.normalize(raw, header)
    ok = True
    errors = []
    if result["type"] != expected_type:
        errors.append(f"type: got={result['type']}, expected={expected_type}")
        ok = False
    if result["repository"] != expected_repo:
        errors.append(f"repo: got={result['repository']}, expected={expected_repo}")
        ok = False
    if result["branch"] != expected_branch:
        errors.append(f"branch: got={result['branch']}, expected={expected_branch}")
        ok = False
    if result["actor"] != expected_actor:
        errors.append(f"actor: got={result['actor']}, expected={expected_actor}")
        ok = False
    if result["source"] != "github":
        errors.append(f"source: got={result['source']}, expected=github")
        ok = False

    if ok:
        print(f"  PASS {header} -> {expected_type}")
        passed += 1
    else:
        print(f"  FAIL {header}: {', '.join(errors)}")

total = len(tests)
print(f"\nGitHub normalization: {passed}/{total} passed")
assert passed == total, f"Only {passed}/{total} passed"
print("All GitHub tests OK")
