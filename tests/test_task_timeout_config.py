"""Issue #516 M1：任务超时配置读取链单元测试。

覆盖 resolve_task_timeout / timeout_retry_count / retry_backoff_sec / load_timeout_config：
优先级（task-env > per-skill > per-repo > agent 默认 > 全局 env > 兜底 3600）、
coding 默认 7200、其余 agent 不回退、非法配置回退。
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime import task_timeout as tt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离进程级 env，避免宿主环境干扰优先级断言。"""
    for k in ("RUNTIME_TASK_TIMEOUT", "RUNTIME_TASK_TIMEOUT_RETRIES",
              "RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF", "GRAPE_TASK_TIMEOUT_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    yield


CFG = {
    "agent_defaults": {"coding": 7200, "qa": 5400},
    "per_repo": {"gitcode:foo/bar": 9000},
    "per_skill": {"coding-cli-1": 10800},
    "timeout_retries": 2,
    "retry_backoff_sec": 15,
}


class TestResolvePriority:
    """优先级链：task-env > per-skill > per-repo > agent 默认 > 全局 env > 兜底。"""

    def test_task_env_explicit_wins(self):
        t = {"agent_type": "coding", "skill": "s", "repo_key": "gitcode:foo/bar",
             "env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT": "1234"})}
        v, src = tt.resolve_task_timeout(t, cfg=CFG)
        assert (v, src) == (1234.0, "task-env")

    def test_per_skill_over_all(self):
        t = {"agent_type": "coding", "skill": "coding-cli-1", "repo_key": "gitcode:foo/bar"}
        v, src = tt.resolve_task_timeout(t, cfg=CFG)
        assert (v, src) == (10800.0, "per-skill:coding-cli-1")

    def test_per_repo_over_agent_default(self):
        t = {"agent_type": "coding", "skill": "other-skill", "repo_key": "gitcode:foo/bar"}
        v, src = tt.resolve_task_timeout(t, cfg=CFG)
        assert (v, src) == (9000.0, "per-repo:gitcode:foo/bar")

    def test_agent_default_over_global_env(self):
        t = {"agent_type": "coding", "skill": "s", "repo_key": "gitcode:x/y"}
        v, src = tt.resolve_task_timeout(t, cfg=CFG, process_env={"RUNTIME_TASK_TIMEOUT": "1800"})
        assert (v, src) == (7200.0, "agent-default:coding")

    def test_global_env_for_unmapped_agent(self):
        t = {"agent_type": "vod"}
        v, src = tt.resolve_task_timeout(t, cfg=CFG, process_env={"RUNTIME_TASK_TIMEOUT": "2700"})
        assert (v, src) == (2700.0, "env:RUNTIME_TASK_TIMEOUT")

    def test_fallback_default_3600(self):
        v, src = tt.resolve_task_timeout({"agent_type": "vod"}, cfg={})
        assert (v, src) == (3600.0, "default")


class TestCodingDefault:
    """验收：coding 默认 ≥7200s；其余 agent 默认值不回退（3600）。"""

    def test_coding_default_7200(self):
        v, _ = tt.resolve_task_timeout({"agent_type": "coding"}, cfg={})
        assert v >= 7200

    def test_coding_default_case_insensitive(self):
        v, src = tt.resolve_task_timeout({"agent_type": "Coding"}, cfg={})
        assert (v, src) == (7200.0, "agent-default:coding")

    def test_other_agents_not_regressed(self):
        for ag in ("qa", "sre", "vod", "issue", "chat", "unknown", ""):
            v, src = tt.resolve_task_timeout({"agent_type": ag}, cfg={})
            assert (v, src) == (3600.0, "default"), f"{ag} 不应回退"


class TestRobustness:
    """非法值/坏 JSON 静默回退，不抛。"""

    def test_invalid_task_env_ignored(self):
        t = {"agent_type": "coding", "env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT": "abc"})}
        v, _ = tt.resolve_task_timeout(t, cfg={})
        assert v == 7200.0

    def test_negative_and_zero_ignored(self):
        for bad in (0, -5, "0", "-1"):
            t = {"agent_type": "vod", "env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT": bad})}
            v, src = tt.resolve_task_timeout(t, cfg={})
            assert (v, src) == (3600.0, "default")

    def test_bad_env_extra_json_ignored(self):
        t = {"agent_type": "coding", "env_extra": "{not json"}
        v, _ = tt.resolve_task_timeout(t, cfg={})
        assert v == 7200.0

    def test_cfg_invalid_values_ignored(self):
        # skill/repo 覆盖非法 → 落到 agent 默认；coding 配置值非法 → 回退内置 7200
        cfg = {"per_skill": {"s": "oops"}, "per_repo": {"r": -1}, "agent_defaults": {"coding": "x"}}
        t = {"agent_type": "coding", "skill": "s", "repo_key": "r"}
        v, _ = tt.resolve_task_timeout(t, cfg=cfg)
        assert v == 7200.0

    def test_cfg_agent_default_invalid_falls_to_builtin(self):
        # 配置里 coding 值非法 → 回退内置 AGENT_TIMEOUT_DEFAULTS（7200），行为不回退
        cfg = {"agent_defaults": {"coding": "bad"}}
        v, _ = tt.resolve_task_timeout({"agent_type": "coding"}, cfg=cfg)
        assert v == 7200.0

    def test_case_insensitive_lookup(self):
        cfg = {"per_skill": {"Coding-CLI-1": 9000}}
        v, src = tt.resolve_task_timeout({"agent_type": "coding", "skill": "coding-cli-1"}, cfg=cfg)
        assert (v, src) == (9000.0, "per-skill:coding-cli-1")


class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        assert tt.load_timeout_config(path=str(tmp_path / "nope.json")) == {}

    def test_load_valid_file(self, tmp_path):
        p = tmp_path / "tt.json"
        p.write_text(json.dumps(CFG), encoding="utf-8")
        got = tt.load_timeout_config(path=str(p))
        assert got["per_repo"] == CFG["per_repo"]
        assert got["timeout_retries"] == 2

    def test_corrupt_file_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{oops", encoding="utf-8")
        assert tt.load_timeout_config(path=str(p)) == {}

    def test_non_dict_returns_empty(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1,2]", encoding="utf-8")
        assert tt.load_timeout_config(path=str(p)) == {}


class TestRetryKnobs:
    def test_default_retry_is_at_least_one(self):
        assert tt.timeout_retry_count({}) >= 1
        assert tt.DEFAULT_TIMEOUT_RETRIES == 1

    def test_task_env_retry_wins(self):
        t = {"env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT_RETRIES": "3"})}
        assert tt.timeout_retry_count(t, cfg=CFG) == 3

    def test_task_env_zero_disables(self):
        t = {"env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT_RETRIES": "0"})}
        assert tt.timeout_retry_count(t, cfg=CFG) == 0

    def test_cfg_retry(self):
        assert tt.timeout_retry_count({}, cfg=CFG) == 2

    def test_env_retry(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT_RETRIES", "4")
        assert tt.timeout_retry_count({}, cfg={}) == 4

    def test_invalid_retry_falls_back(self):
        t = {"env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT_RETRIES": "abc"})}
        assert tt.timeout_retry_count(t, cfg={}) == 1

    def test_backoff_default_and_override(self, monkeypatch):
        assert tt.retry_backoff_sec(cfg={}) == 30.0
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF", "0.5")
        assert tt.retry_backoff_sec(cfg={}) == 0.5
        assert tt.retry_backoff_sec(cfg=CFG) == 0.5  # env 优先于配置文件
        monkeypatch.delenv("RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF")
        assert tt.retry_backoff_sec(cfg=CFG) == 15.0
