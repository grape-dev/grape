"""Runtime 注册表：注册 / 查询 / 发现本机可用的 Agent 执行引擎。

实现复用 src/domains/runtime/agents/executor.py 的注册表（M0 迁移期间统一为一份 dict，
避免调用方/测试 mock 路径分裂；后续可整体搬移）。
"""
from src.domains.runtime.agents.executor import (
    _BACKENDS as _RUNTIMES,
    register_backend as register_runtime,
    get_backend as get_runtime,
    discover_backends as discover_runtimes,
)
from .base import AgentRuntime


def validate_registration_model(engine_name: str, config_path: str | None = None,
                                env: dict | None = None, fetch=None,
                                timeout: float = 3.0) -> dict:
    """注册/启动阶段模型可用性校验入口（Issue #3820）。

    用引擎配置的 key 调 `/v1/models` 校验配置模型名是否在可用列表：
      - `misaligned`：调用方必须拒绝该引擎的「健康注册」（healthy=False），
        并把 `message`（配置模型名 vs 可用列表差异）写进日志/探活原因；
      - `unreachable` / `no_config` / `no_validatable_target` / `disabled`：放行，
        不因网关波动误拒（风险缓解：仅确认缺失才拒绝）。

    注册链路（worker `_discover_local_runtimes`）在关键路径上同步调用，故默认
    3s 短超时——已有 `--version` 探测同为该链路的阻塞调用，量级一致。
    """
    from .engine_model import validate_engine_model

    return validate_engine_model(engine_name, config_path=config_path, env=env,
                                 fetch=fetch, timeout=timeout)