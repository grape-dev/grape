"""Issue #471 QA Suggested 修复：worker 侧 _reregister 健壮性。

- S2：engine_by_rid 按 name 配对（服务端 register 返回含 name 的 runtimes
  明细，极端情况下个别引擎被跳过时 zip 位置对齐会错位）。
- S3：重扫瞬时为空沿用上次快照（周期性重注册可自愈，但本轮不该把
  runtime_ids 误清空——与「扫描异常」区分开）。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime import agent as ra


def _mk_engine(name, cli="/bin/x"):
    return {"name": name, "display_name": name, "version": "1.0",
            "healthy": True, "cli_path": cli, "installed": True}


class FakeRegClient:
    """对齐真实 client.register 签名（含 cli_version）。"""

    def __init__(self, ids, names=None):
        self.ids = ids
        self.names = names  # 服务端返回的 runtimes 明细里的 name（可与 installed 错位）
        self.recovered = []

    async def reset_session(self):
        pass

    async def register(self, runtimes, device_name="", cli_version="", local_skills=None):
        if self.names is None:
            return self.ids, "probe-url"
        # 服务端逐引擎 upsert，返回 {id, name} 明细（个别引擎可能被跳过）
        detail = [{"id": i, "name": n} for i, n in zip(self.ids, self.names)]
        self.last_register_detail = detail
        return [d["id"] for d in detail], "probe-url"

    async def recover_orphans(self, runtime_id):
        self.recovered.append(runtime_id)


class TestEngineByRidNamePairing:
    def test_name_pairing_no_misalignment(self):
        """S2：服务端跳过中间一个引擎时，rid→引擎名不得错位。

        本机 installed = [opencode, codearts, hermes]，服务端只注册成功
        opencode/hermes（codearts 被跳过）：54 应配 opencode、55 应配 hermes，
        而非 zip 位置对齐的 codearts/hermes。
        """
        c = FakeRegClient([54, 55], names=["opencode", "hermes"])
        installed = [_mk_engine("opencode"), _mk_engine("codearts"), _mk_engine("hermes")]
        with patch.object(ra, "_discover_local_runtimes", return_value=installed):
            result = asyncio.run(ra._reregister(c, [], "dev"))
        assert result is not None
        ids, engine_by_rid, _ = result
        assert ids == [54, 55]
        assert "opencode" in engine_by_rid[54]
        assert "hermes" in engine_by_rid[55]
        assert "codearts" not in engine_by_rid[54]

    def test_name_pairing_same_order_still_works(self):
        """S2：顺序一致时行为不变（回归看护）。"""
        c = FakeRegClient([7], names=["hermes"])
        installed = [_mk_engine("hermes")]
        with patch.object(ra, "_discover_local_runtimes", return_value=installed):
            result = asyncio.run(ra._reregister(c, [], "dev"))
        assert result is not None
        _, engine_by_rid, _ = result
        assert "hermes" in engine_by_rid[7]


class TestRescanEmptyKeepsSnapshot:
    def test_rescan_transient_empty_keeps_previous(self):
        """S3：重扫瞬时为空（异常状态）→ 沿用上次快照，不清空 runtime_ids。"""
        c = FakeRegClient([54, 55])
        installed = [_mk_engine("opencode"), _mk_engine("hermes")]
        with patch.object(ra, "_discover_local_runtimes", return_value=[]):
            result = asyncio.run(ra._reregister(c, installed, "dev"))
        # 重扫为空 → 不发起注册（沿用快照），返回 None 由调用方保留原 ids
        assert result is None
        # register 不应被调用（避免把空列表注册上去）
        # （FakeRegClient 无 call 记录，用 AsyncMock 版再验一次）

    def test_rescan_transient_empty_skips_register_call(self):
        """S3：重扫为空时跳过 register 调用（不动服务端状态）。"""
        c = MagicMock()
        c.register = AsyncMock(return_value=([], ""))
        c.reset_session = AsyncMock()
        installed = [_mk_engine("opencode")]
        with patch.object(ra, "_discover_local_runtimes", return_value=[]):
            result = asyncio.run(ra._reregister(c, installed, "dev"))
        assert result is None
        c.register.assert_not_awaited()

    def test_real_empty_still_registers_empty(self):
        """S3：启动起就无引擎（installed 快照也为空）→ 正常注册空列表（C1 回归看护）。"""
        c = MagicMock()
        c.register = AsyncMock(return_value=([], ""))
        c.reset_session = AsyncMock()
        with patch.object(ra, "_discover_local_runtimes", return_value=[]):
            result = asyncio.run(ra._reregister(c, [], "dev"))
        assert result == ([], {}, [])
        c.register.assert_awaited_once()
