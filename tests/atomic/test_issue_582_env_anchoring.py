"""Issue #582 原子测试：atom_selfcheck.run_case 非路径 env 原值透传。

背景：run_case 旧启发式（值不含 ``://`` 即锚定仓根）会把标量 env
（如 ``GITCODE_OWNER=stub``）误改写为绝对路径，导致自检用例路由失配、
断言意外通过（误报绿）。期望：仅路径型 env 键（白名单 GRAPE_SRC_DIR +
存在性探测兜底）锚定仓根，其余原样透传。
"""
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import pytest

from atom_selfcheck import PATH_ENV_KEYS, REPO_ROOT, anchor_env, run_case

pytestmark = pytest.mark.atomic


class TestAnchorEnv:
    """anchor_env / run_case 的 env 锚定契约（Issue #582 Spec 验收标准）。"""

    def test_non_path_env_passthrough(self):
        # 验收 1：非路径型标量 env 原值透传，不被改写为绝对路径
        env = anchor_env({"GITCODE_OWNER": "stub", "SOME_FLAG": "1"})
        assert env["GITCODE_OWNER"] == "stub"
        assert env["SOME_FLAG"] == "1"

    def test_url_env_passthrough(self):
        # URL 形态值透传（历史 ``://`` 判断保留）
        env = anchor_env({"ENDPOINT": "https://gitcode.com", "GRAPE_SRC_DIR": "src"})
        assert env["ENDPOINT"] == "https://gitcode.com"

    def test_absolute_env_passthrough(self):
        env = anchor_env({"GRAPE_SRC_DIR": "/tmp/abs"})
        assert env["GRAPE_SRC_DIR"] == "/tmp/abs"

    def test_whitelist_path_env_anchored(self):
        # 验收 2：路径型 env 键（GRAPE_SRC_DIR）相对值锚定到仓根
        env = anchor_env({"GRAPE_SRC_DIR": "src"})
        assert env["GRAPE_SRC_DIR"] == os.path.join(REPO_ROOT, "src")

    def test_whitelist_includes_grape_src_dir(self):
        assert "GRAPE_SRC_DIR" in PATH_ENV_KEYS

    def test_probe_fallback_anchors_existing_relpath(self):
        # 探测兜底：非白名单键但值在仓根下真实存在（如 scripts/）→ 锚定
        env = anchor_env({"SOME_DIR": "scripts"})
        assert env["SOME_DIR"] == os.path.join(REPO_ROOT, "scripts")

    def test_probe_fallback_not_triggered_for_missing_value(self):
        # 非路径标量值（仓根下不存在）→ 原样透传（stub 不得被误锚定）
        env = anchor_env({"GITCODE_OWNER": "stub"})
        assert env["GITCODE_OWNER"] == "stub"

    def test_none_and_empty_env(self):
        assert anchor_env(None) == {}
        assert anchor_env({}) == {}

    def test_non_string_value_passthrough(self):
        env = anchor_env({"COUNT": 3, "FLAG": True})
        assert env["COUNT"] == 3
        assert env["FLAG"] is True


class TestRunCaseEnvPassThrough:
    """run_case 端到端：子进程实际收到的 env 遵循锚定契约。"""

    def _probe_env(self, extra_env):
        # python 打印目标 env 后退出 0，便于断言子进程视角
        script = (
            "import os,sys;"
            "sys.stdout.write(os.environ.get('GITCODE_OWNER','<unset>')+'|'"
            "+os.environ.get('GRAPE_SRC_DIR','<unset>'))"
        )
        result = run_case("probe", [sys.executable, "-c", script], extra_env=extra_env)
        assert result["passed"], result["stderr"]
        return result["stdout"].split("|")

    def test_run_case_scalar_env_verbatim(self):
        # 验收 1（端到端）：GITCODE_OWNER=stub 透传到子进程
        owner, _ = self._probe_env({"GITCODE_OWNER": "stub"})
        assert owner == "stub"

    def test_run_case_path_env_anchored(self):
        # 验收 2（端到端）：GRAPE_SRC_DIR=src 锚定为仓根绝对路径
        _, src = self._probe_env({"GRAPE_SRC_DIR": "src"})
        assert src == os.path.join(REPO_ROOT, "src")
        assert os.path.isdir(src)

    def test_run_case_failure_reported_not_green(self):
        # 失败用例必须如实报 FAIL（防止自检误报绿回归）
        result = run_case("must-fail", [sys.executable, "-c", "raise SystemExit(3)"])
        assert not result["passed"]
        assert result["returncode"] == 3
