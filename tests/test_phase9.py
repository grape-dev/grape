"""Phase 9 — Plugin interface verification"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 9.1 discover_backends
from src.agent.executor import discover_backends, register_backend
backends = discover_backends()
assert len(backends) >= 4
print("P9.1 discover_backends:", len(backends), "engines")

# 9.1 register_backend
from src.agent.base import AgentBackend
class HermesBackend(AgentBackend):
    name = "hermes"
    binary = "hermes"
    def build_args(self, prompt, session_id=""):
        return ["hermes", "-p", prompt]
register_backend("hermes", HermesBackend())
print("P9.1 register_backend: OK (~20 lines)")

from src.agent.executor import get_backend
b = get_backend("hermes")
assert b.name == "hermes"
print("P9.1 get_backend hermes: OK")

# 9.2 IMChannel
from src.im.base import IMChannel
assert hasattr(IMChannel, "send_message")
assert hasattr(IMChannel, "send_card")
assert hasattr(IMChannel, "get_channel_info")
print("P9.2 IMChannel abstract class: OK")

class WeComChannel(IMChannel):
    name = "wecom"; display_name = "企业微信"
    def send_message(self, target, text, msg_type="text"): return True
    def send_card(self, target, title, content, actions=None): return True
    def get_channel_info(self): return {"connected": False, "name": self.name}

wc = WeComChannel()
assert wc.send_message("test", "hello") == True
print("P9.2 WeComChannel stub: OK")

# 9.3 GitHub normalization
from src.vcs.github import GitHubBackend
import json

gh = GitHubBackend()
payloads = {
    "push": {"ref": "refs/heads/main", "repository": {"full_name": "org/repo"}, "sender": {"login": "alice"}},
    "issues": {"issue": {"number": 1, "user": {"login": "bob"}}, "repository": {"full_name": "org/repo"}, "sender": {"login": "bob"},
               "action": "opened"},
}
for header, body in payloads.items():
    raw = json.dumps(body).encode()
    r = gh.normalize(raw, header)
    assert r["source"] == "github"
    assert r["repository"] == "org/repo"
print("P9.3 GitHub normalization: OK")

# VCS adapter integration
from src.vcs.adapter import normalize_event, register_vcs_backend
r1 = normalize_event(json.dumps(payloads["push"]).encode(), "push", "github")
assert r1["source"] == "github"
print("P9.3 VCS adapter github: OK")

from src.vcs.base import VcsBackend
class GitLabBackend(VcsBackend):
    name = "gitlab"; webhook_path = "/webhook/gitlab"
    def normalize(self, raw, event_header):
        return {"source": "gitlab", "type": event_header, "repository": "org/repo", "branch": "", "actor": "", "payload": {}}
register_vcs_backend("gitlab", GitLabBackend())
r2 = normalize_event(b"{}", "Push Hook", "gitlab")
assert r2["source"] == "gitlab"
print("P9.3 register GitLab: OK")

print("\nAll Phase 9 tests PASSED")
