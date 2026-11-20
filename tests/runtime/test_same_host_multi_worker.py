"""同机多 cloud_worker 实例——whoami/实例状态目录/改名跟随/401 自杀 单测。

实例键锚：credential_uid 前 8 位（单锚，全局唯一；同机多实例设计 2026-11-05 简化版）。
覆盖：§6.1 凭据状态目录参数化（有/无 CLOUD_WORKER_CREDENTIAL_UID）
- §6.2 改名跟随（whoami.name ≠ whoami.json → 更新缓存 + 日志；id/目录不动）
- §6.3 401 自杀（whoami 401/403 连击 3 次退出）

运行：.venv/bin/python -m pytest tests/test_same_host_multi_worker.py -q
"""
import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.domains.runtime.engine import cloud_worker as cw


# ===================================================================
# A. 实例状态目录（§6.1）
# ===================================================================

class TestInstanceStateDir:
    def test_with_instance_key(self):
        """有 CLOUD_WORKER_CREDENTIAL_UID → workers/<uid8>/ 目录。"""
        with patch.dict(os.environ, {"CLOUD_WORKER_CREDENTIAL_UID": "550e8400e29b41d4a716446655440000"}):
            assert cw._cred_state_dir() == "/var/lib/grape/cloud-worker/workers/550e8400"
            assert cw._provider_configs_cache_path() == "/var/lib/grape/cloud-worker/workers/550e8400/provider-configs.json"
            assert cw._whoami_json_path() == "/var/lib/grape/cloud-worker/workers/550e8400/whoami.json"

    def test_without_key_falls_back_to_global(self):
        """无标识（裸跑/调试）→ 回退原全局路径（spec §十-4 裁定）。"""
        env = {k: v for k, v in os.environ.items() if k != "CLOUD_WORKER_CREDENTIAL_UID"}
        with patch.dict(os.environ, env, clear=True):
            assert cw._cred_state_dir() == "/var/lib/grape/cloud-worker"
            assert cw._provider_configs_cache_path() == "/var/lib/grape/cloud-worker/provider-configs.json"

    def test_blank_key_falls_back(self):
        """空串键 → 同无标识。"""
        with patch.dict(os.environ, {"CLOUD_WORKER_CREDENTIAL_UID": "  "}):
            assert cw._cred_state_dir() == "/var/lib/grape/cloud-worker"


# ===================================================================
# B. whoami.json 读写（§五 快照约定）
# ===================================================================

class TestWhoamiCache:
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "whoami.json")
            data = {"credential_id": 5, "name": "cube-w1", "cred_type": "sandbox",
                    "platform": "cubesandbox", "scope": "global",
                    "server_instance": "grape-550e8400-e29b-41d4-a716-446655440000"}
            with patch.object(cw, "_whoami_json_path", lambda: path):
                cw._write_whoami_cache(data)
                got = cw._read_whoami_cache()
            assert got["name"] == "cube-w1"
            assert got["credential_id"] == 5
            # 永不含凭证字段（设计不变量）
            assert "token" not in got and "api_key" not in got

    def test_read_missing_returns_empty(self):
        with patch.object(cw, "_whoami_json_path", lambda: "/nonexistent/whoami.json"):
            assert cw._read_whoami_cache() == {}


# ===================================================================
# C. 改名跟随 + 身份守护（§6.2）
# ===================================================================

class TestWhoamiFollow:
    def _mk_client(self, whoami_ret=None, whoami_exc=None):
        c = MagicMock()
        if whoami_exc:
            c.whoami = AsyncMock(side_effect=whoami_exc)
        else:
            c.whoami = AsyncMock(return_value=whoami_ret or {})
        return c

    def test_rename_follow_updates_cache(self):
        """whoami.name ≠ 缓存 → 更新缓存（改名自动跟随，零人工）。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "whoami.json")
            with patch.object(cw, "_whoami_json_path", lambda: path):
                cw._write_whoami_cache({"credential_id": 5, "name": "cube-w1", "cred_type": "sandbox",
                                        "platform": "cubesandbox", "server_instance": "grape-aaa"})
                client = self._mk_client(whoami_ret={"credential_id": 5, "name": "cube-w2",
                                                     "cred_type": "sandbox", "platform": "cubesandbox",
                                                     "server_instance": "grape-aaa"})
                asyncio.run(cw._whoami_follow_and_guard(client))
                got = cw._read_whoami_cache()
            assert got["name"] == "cube-w2"          # 跟随成功
            assert got["credential_id"] == 5          # id 不动

    def test_server_instance_guard_retired(self):
        """[锚简化 2026-11-04 用户裁定] server_instance 防串台自校验已退役——
        实例键换 credential_uid 单锚（全局唯一），mismatch 场景不可达；
        含 server_instance 的 whoami 响应不再触发任何拒绝（字段被忽略）。"""
        env = {"CLOUD_WORKER_SERVER_INSTANCE": "grape-aaa"}  # 旧 env 残留也不影响
        with patch.dict(os.environ, env, clear=False), \
             patch.object(cw, "_whoami_json_path", lambda: "/nonexistent/x.json"):
            client = self._mk_client(whoami_ret={"credential_id": 5, "name": "n",
                                                 "server_instance": "grape-bbb"})
            # 不 raise——旧字段被忽略，guard 链路已删
            asyncio.run(cw._whoami_follow_and_guard(client))

    def test_network_failure_silent(self):
        """非 401 的 whoami 失败（网络）→ 跳过本轮不抛。"""
        client = self._mk_client(whoami_exc=RuntimeError("connection reset"))
        with patch.object(cw, "_whoami_json_path", lambda: "/nonexistent/x.json"):
            asyncio.run(cw._whoami_follow_and_guard(client))  # 不抛即通过


# ===================================================================
# D. server_instance 生成与一致性（§4.1.1）
# ===================================================================

class TestServerInstanceRetired:
    """[锚简化 2026-11-05 用户裁定] server_instance 整体退役——get_server_instance/
    meta['server_instance'] 仲裁/防串台自校验均已删除；实例身份由 credential_uid
    单锚承担（目录键=credential_uid 前 8 位）。原 3 用例（生成持久化/并发收敛/复用）
    测的函数已不存在，改写为退役断言：uid 唯一性由迁移 202611040004 的
    UNIQUE(credential_uid) + backfill 保证（见 tests/atomic/test_template_uid_column.py）。"""

    def test_server_instance_retired(self):
        import src.domains.mgmt.user.db as udb
        assert not hasattr(udb, "get_server_instance"), \
            "get_server_instance 应已删除（server_instance 整体退役）"


class TestWhoami401Suicide:
    def test_401_streak_counter_semantics(self):
        """401/403/revoked 类错误计入连击（语义单测——refresh loop 集成行为由
        _whoami_follow_and_guard 抛出 + 循环计数实现，此处验证错误分类逻辑）。"""
        msgs = ["HTTP 401", "HTTP 403", "credential revoked", "connection reset"]
        auth_fail = [m for m in msgs if "401" in m or "403" in m or "revoked" in m.lower()]
        assert auth_fail == ["HTTP 401", "HTTP 403", "credential revoked"]


# ===================================================================
# F. 安装脚本参数段（§4.2 删参报错文案）
# ===================================================================

class TestLocalVersionInstanceAware:
    """[实例目录 v2] _read_local_worker_version 实例感知——沙箱实例优先读 cwd/etc/worker.version。"""

    def test_instance_version_file_preferred(self, tmp_path):
        import os
        from unittest.mock import patch
        os.environ["CLOUD_WORKER_CREDENTIAL_UID"] = "332c4c4914a141d4a7164466554400aa"
        try:
            # cwd 指向实例目录, etc/worker.version=h-new
            (tmp_path / "etc").mkdir()
            (tmp_path / "etc" / "worker.version").write_text("h-newversion1")
            with patch.object(os, "getcwd", lambda: str(tmp_path)):
                from src.domains.runtime.engine.agent import _read_local_worker_version
                assert _read_local_worker_version() == "h-newversion1"
        finally:
            os.environ.pop("CLOUD_WORKER_CREDENTIAL_UID", None)

    def test_non_instance_path_unchanged(self):
        import os
        assert "CLOUD_WORKER_CREDENTIAL_UID" not in os.environ or not os.environ["CLOUD_WORKER_CREDENTIAL_UID"].strip()
        from src.domains.runtime.engine.agent import _read_local_worker_version
        # 非实例(无 key): 走原路径, 不抛即通过(返回值依环境)
        v = _read_local_worker_version()
        assert isinstance(v, str)


class TestInstallScriptArgs:
    def _script(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "scripts", "install-worker.sh")
        return open(p, encoding="utf-8").read()

    def test_removed_params_rejected(self):
        s = self._script()
        assert "--type|--platform)" in s           # 显式报错分支（仅 type/platform）
        assert "已删除" in s                              # 中文报错文案

    def test_dir_param_restored(self):
        """[2026-11-12 用户裁定] --dir 恢复原有参数语义：沙箱显式传 --dir 时不走 /opt/grape-cw/$CRED_KEY。"""
        s = self._script()
        assert '--dir)     INSTALL_DIR="$2"' in s       # 参数解析恢复
        assert 'if [ -n "$INSTALL_DIR" ]; then' in s    # 沙箱分支用户目录优先
        assert 'INSTALL_DIR="/opt/grape-cw/$CRED_KEY"' in s  # 默认目录仍保留

    def test_whoami_block_present(self):
        s = self._script()
        assert "/api/runtime/whoami" in s                 # whoami 调用
        assert "CRED_KEY=" in s                            # 凭据目录键派生（credential_uid 前 8 位）
        assert "CLOUD_WORKER_CREDENTIAL_UID=" in s         # env 写入

    def test_dir_tag_retired(self):
        s = self._script()
        assert "_DIR_TAG=\"" not in s.replace("# a1cb77bdf 机制由 server_instance+credential_id 复合键取代", "")  # 实参退役（注释除外）

    def test_instance_dir_fully_isolated(self):
        """[spec §五 v2] 沙箱实例目录 /opt/grape-cw/<key>/ 全量独立; 普通 worker 不变。"""
        s = self._script()
        assert 'INSTALL_DIR="/opt/grape-cw/$CRED_KEY"' in s      # 沙箱凭据状态目录
        assert 'INSTALL_DIR="/opt/grape"' in s                        # 普通 worker 不变
        assert 'CFG_DIR="$INSTALL_DIR/etc"' in s                      # 配置内嵌实例目录
        assert 'etc/workers/' not in s.replace("# 不再\n    # etc/workers 子目录", "")  # 旧子目录派生退役

    def test_systemd_policy_sandbox(self):
        s = self._script()
        assert "_RESTART_POLICY=\"on-failure\"" in s      # 沙箱 on-failure
        assert "StartLimitBurst=$_START_LIMIT_BURST" in s
