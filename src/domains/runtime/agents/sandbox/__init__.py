"""沙箱门面——对外 API 导出 + 模块级 sandbox_manager。

调用方只允许 from src.domains.runtime.agents.sandbox import sandbox_manager。
禁止 import 内部路径（backends/isolation）。audit 从 common.audit 导入。

模块级默认实例用 Null 默认值（轻量，不打开文件/不生成密钥）。
生产环境必须显式构造覆盖：

    from src.domains.runtime.agents.sandbox import SandboxManager
    from src.domains.runtime.agents.sandbox.audit import FileAuditLogger

    sandbox_manager = SandboxManager(audit=FileAuditLogger("/var/log/grape/sandbox-audit.jsonl"))

不覆盖则线上无审计（NullAuditLogger）。
"""
from .api import Sandbox, SandboxProvider
from .types import SandboxMode, AlertSeverity, WhitelistRule, CompiledRule
from .errors import SandboxUnavailableError
from .manager import SandboxManager
from .alert import AlertEngine, NullAlertEngine, Alert
from .redact import RedactEngine, NullRedactEngine, RedactResult
from .whitelist import (
    normalize_path, match_path_prefix, match_whitelist,
    compile_rules, get_rules, invalidate_cache, load_rules_from_db,
    RuleCache,
)

sandbox_manager = SandboxManager()

__all__ = [
    "sandbox_manager", "SandboxManager",
    "Sandbox", "SandboxProvider",
    "SandboxMode", "SandboxUnavailableError",
    "AlertSeverity", "AlertEngine", "NullAlertEngine", "Alert",
    "RedactEngine", "NullRedactEngine", "RedactResult",
    "WhitelistRule", "CompiledRule", "RuleCache",
    "normalize_path", "match_path_prefix", "match_whitelist",
    "compile_rules", "get_rules", "invalidate_cache",
]


# ===== [沙箱迁移] 兼容层：src/agent/sandbox.py 模块被本包遮蔽 =====
# main 谱系代码（executor.py / hermes_acp.py / hwcloud_acp.py）使用旧模块级 API：
#   from src.domains.runtime.agents.sandbox import build_bwrap_cmd / sandbox_enabled
# 包引入后同名模块不可再被 import（包优先），此处 re-export 保持兼容。
# 旧分支最终态已全部迁移到 sandbox_manager.prepare(...) Sandbox 对象接口；
# 本地 Popen 路径仍走 build_bwrap_cmd（bwrap 白名单锁，行为与 main 基线一致）。
def sandbox_enabled() -> bool:  # noqa: F811
    """bwrap 沙箱开关（兼容 re-export，实现见同目录 _compat_module.py）。"""
    from src.domains.runtime.agents.sandbox._compat_module import sandbox_enabled as _f
    return _f()


def build_bwrap_cmd(cwd: str, backend_name: str = ""):  # noqa: F811
    """构造 bwrap 包装命令前缀（兼容 re-export）。"""
    from src.domains.runtime.agents.sandbox._compat_module import build_bwrap_cmd as _f
    return _f(cwd, backend_name=backend_name)
