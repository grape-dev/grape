"""Phase 9 — 插件接口验证（runtime 注册 / IM 抽象 / VCS 归一化适配器）"""
import json

import pytest

from src.runtime.registry import discover_runtimes, register_runtime, get_runtime
from src.runtime.base import AgentRuntime
from src.im.base import IMChannel
from src.vcs.base import VcsBackend


class HermesRuntime(AgentRuntime):
    name = "hermes"
    binary = "hermes"

    def build_args(self, prompt, session_id=""):
        return ["hermes", "-p", prompt]


class WeComChannel(IMChannel):
    name = "wecom"
    display_name = "企业微信"

    def send_message(self, target, text, msg_type="text"):
        return True

    def send_card(self, target, title, content, actions=None):
        return True

    def get_channel_info(self):
        return {"connected": False, "name": self.name}


class GitLabBackend(VcsBackend):
    name = "gitlab"
    webhook_path = "/webhook/gitlab"
    event_header_key = "X-GitLab-Event"

    def normalize(self, raw, event_header):
        return {"source": "gitlab", "type": event_header, "repository": "org/repo",
                "branch": "", "actor": "", "payload": {}}


class TestRuntimeRegistry:
    def test_discover_runtimes_returns_engines(self):
        assert len(discover_runtimes()) >= 4

    def test_register_and_get_runtime(self):
        register_runtime("hermes_test", HermesRuntime())
        try:
            b = get_runtime("hermes_test")
            assert isinstance(b, HermesRuntime)
            assert b.build_args("p", "s") == ["hermes", "-p", "p"]
        finally:
            from src.agent import executor
            executor._BACKENDS.pop("hermes_test", None)


class TestIMChannelAbstract:
    def test_abstract_methods_present(self):
        assert hasattr(IMChannel, "send_message")
        assert hasattr(IMChannel, "send_card")
        assert hasattr(IMChannel, "get_channel_info")

    def test_stub_channel_works(self):
        wc = WeComChannel()
        assert wc.send_message("test", "hello") is True
        assert wc.get_channel_info() == {"connected": False, "name": "wecom"}


class TestVcsAdapter:
    def test_github_normalize_through_adapter(self):
        from src.vcs.adapter import normalize_event
        payload = {"ref": "refs/heads/main", "repository": {"full_name": "org/repo"},
                   "sender": {"login": "alice"}}
        r = normalize_event(json.dumps(payload).encode(), "push", "github")
        assert r["source"] == "github"
        assert r["repository"] == "org/repo"

    def test_register_gitlab_and_normalize(self):
        from src.vcs.adapter import normalize_event, register_vcs_backend
        register_vcs_backend("gitlab", GitLabBackend())
        r = normalize_event(b"{}", "Push Hook", "gitlab")
        assert r["source"] == "gitlab"
        assert r["repository"] == "org/repo"
