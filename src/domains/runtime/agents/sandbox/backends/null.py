"""NullProvider + NullSandbox——组合式，和 BwrapSandbox 结构同构。

Null Object 模式：和真实实现同构，只是无操作。
NullSandbox 持有 Null 组件（NullNetworkIsolation / NullResourceLimiter / NullAuditLogger / NullCredentialStrategy）。
"""
from __future__ import annotations

from ..api import Sandbox, SandboxProvider
from ..types import SandboxState, IsolationLevel, ExecutionMode
from ..credential import NullCredentialStrategy
from ..isolation.network import NullNetworkIsolation
from ..isolation.resource import NullResourceLimiter
from src.domains.runtime.agents.common.audit import NullAuditLogger


class NullSandbox:
    """空沙箱——wrap 返回原命令，不包装。

    组合式：持有 Null 组件，和 BwrapSandbox 结构同构。
    execution_mode = NONE：仅 caps 显式 allow_unsandboxed=True 时选。

    非 bwrap 模式行为和基线 weekly-05 完全一致：
    - 不注入 per-task HOME（HOME 保持宿主机路径）
    - 不调 prepare_task_env
    - 不注入代理 env——清除继承的 proxy 环境变量
    """

    execution_mode = ExecutionMode.NONE

    def __init__(self, reason: str = ""):
        self._network = NullNetworkIsolation()
        self._limiter = NullResourceLimiter()
        self._audit = NullAuditLogger()
        self._credential = NullCredentialStrategy()
        self._state = SandboxState.PREPARED
        self._reason = reason

    @property
    def state(self) -> SandboxState:
        return self._state

    def prepare_env(self, env: dict[str, str], engine_backend: object, resource_stack: object | None = None) -> None:
        """NullSandbox = 不隔离：不注入 per-task HOME，不调 prepare_task_env。

        非bwrap模式行为和基线 weekly-05 完全一致——env 不被修改，HOME 保持宿主机路径。

        根因：v5 把 HOME 注入放在 agent.py 无条件执行，非 bwrap 模式下 codearts installer
        用 per-task HOME 拼接 KERNEL_DATA_DIR → 空目录 → database migration → text_len=0。
        v6 把 HOME 注入移到 BwrapSandbox.prepare_env，NullSandbox.prepare_env 空操作。
        设计文档：docs/weekly-05/bwrap-sandbox-abstraction-v6.md
        """
        pass

    def wrap(self, cmd: list[str], proc_env: dict[str, str] | None = None) -> list[str]:
        if self._state == SandboxState.CLEANED:
            raise RuntimeError("Sandbox already cleaned")
        self._state = SandboxState.ACTIVE
        # NullSandbox 不注入代理 env——清除继承的 proxy 环境变量
        # 防止 codearts 子进程读到宿主 HTTP_PROXY 连不存在的 8888 端口
        if proc_env is not None:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                      "http_proxy", "https_proxy", "all_proxy",
                      "NO_PROXY", "no_proxy",
                      "BUN_HTTP_PROXY", "GRPC_PROXY", "grpc_proxy"):
                proc_env.pop(k, None)
        return cmd

    def popen_kwargs(self) -> dict:
        return {}

    def cleanup(self) -> None:
        if self._state == SandboxState.CLEANED:
            return
        self._network.stop()
        self._limiter.release()
        self._state = SandboxState.CLEANED

    def is_active(self) -> bool:
        return False  # NullSandbox 不是真实沙箱，is_active 返回 False

    def kill(self) -> bool:
        """空操作。"""
        return True

    def __enter__(self) -> "NullSandbox":
        if self._state == SandboxState.CLEANED:
            raise RuntimeError("Sandbox already cleaned")
        self._network.start("")
        self._limiter.prepare()
        self._state = SandboxState.ACTIVE
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


class NullProvider:
    """Null Provider——创建 NullSandbox。统一走 Provider 路径。"""

    name = "null"

    def is_available(self) -> bool:
        # P0-18 修复：NullProvider 不是真正的沙箱，is_available 返回 False
        # 让 prepare() 的 fail-closed 逻辑对 REQUIRED 模式生效
        # PREFERRED 模式下 prepare() 会降级到 NullProvider（不经过 is_available 检查）
        return False

    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.PROCESS

    def create_sandbox(
        self, cwd: str, exec_backend: str, task_id: str = "",
        runtime_id: int | None = None, **kwargs,
    ) -> NullSandbox:
        return NullSandbox(reason="null provider")


class NullAdapter:
    """Null 沙箱适配器。is_cloud=False -> 不进 UI 平台列表。"""
    platform = "null"
    display_name = "None"
    priority = 9999
    is_cloud = False

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config=None):
        return NullProvider()

    def parse_config_file(self, raw: dict):
        from .api import ParsedConfig
        return ParsedConfig(endpoint="", api_key="")
    def normalize_db_config(self, db_row: dict): return {}
    def prepare_artifacts(self, config, workdir, provider_id) -> None: pass
    def validate_extra_config(self, raw: dict): return {}
    def health_check(self, config: dict) -> tuple: return (True, "")
    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool: return True
    def ui_schema(self):
        from .api import FormSchema
        return FormSchema()
    def cleanup(self, provider_id: int, workdir: str) -> None: pass


PROVIDER = NullProvider
ADAPTER = NullAdapter()
