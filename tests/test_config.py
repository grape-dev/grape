"""Tests for config module."""
import pytest
import os
import sys
import tempfile
import json

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


def _import_config(routing_json=None):
    """Import config with optional custom routing JSON path."""
    # Clear module cache
    for mod in ["src.config", "config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)

    if routing_json is not None:
        # Write routing to temp file and override CONFIG_DIR
        import src.config as cfg
        tmp_dir = tempfile.mkdtemp()
        routing_path = os.path.join(tmp_dir, "event-routing.json")
        with open(routing_path, "w", encoding="utf-8") as f:
            json.dump(routing_json, f)
        cfg.CONFIG_DIR = tmp_dir
        cfg._load_routing_config()
        return cfg
    else:
        import src.config as cfg
        return cfg


class TestEventRouting:
    def test_default_fallback(self):
        """Unknown event types should return fallback_skill."""
        cfg = _import_config()
        skill = cfg.get_domain_skill("unknown_event_type")
        # Default fallback from event-routing.json is empty string
        assert skill == ""

    def test_known_routes(self):
        """Known event types route to correct skills."""
        cfg = _import_config()
        assert cfg.get_domain_skill("issue_opened") == "issue"
        assert cfg.get_domain_skill("issue_comment") == "issue"
        assert cfg.get_domain_skill("issue_labeled") == "issue"
        assert cfg.get_domain_skill("pull_request_opened") == "pr"
        assert cfg.get_domain_skill("pull_request_updated") == "pr"
        assert cfg.get_domain_skill("build_failure") == "build-debug"
        assert cfg.get_domain_skill("deploy_start") == "deploy-guard"
        assert cfg.get_domain_skill("deploy_failure") == "deploy-guard"
        assert cfg.get_domain_skill("release_created") == "changelog-gen"

    def test_removed_events_not_in_routing(self):
        """Events like push, tag_created, build_success should fallback to empty."""
        cfg = _import_config()
        assert cfg.get_domain_skill("push") == ""
        assert cfg.get_domain_skill("tag_created") == ""
        assert cfg.get_domain_skill("build_success") == ""

    def test_custom_routing(self):
        """Custom routing JSON overwrites defaults."""
        cfg = _import_config({
            "routing": {"custom_event": "custom-skill"},
            "fallback_skill": "fallback-skill"
        })
        assert cfg.get_domain_skill("custom_event") == "custom-skill"
        assert cfg.get_domain_skill("unknown") == "fallback-skill"

    def test_empty_routing(self):
        """Empty routing with custom fallback."""
        cfg = _import_config({
            "routing": {},
            "fallback_skill": "my-fallback"
        })
        assert cfg.get_domain_skill("anything") == "my-fallback"
