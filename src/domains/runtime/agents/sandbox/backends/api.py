"""PlatformAdapter 契约 + 数据类——平台适配器设计 v3。

新增平台只需（3 步）：
1. 创建 configs/{platform}-config.json
2. 创建 backends/{platform}.py：{Platform}Provider + {Platform}Adapter
3. 重启服务

新增平台不需要改：__init__.py、init_tasks、cloud_worker、client、settings、sandbox.js、manager.py

注意：本次改造（v3）会改 manager.py 三处 factory 调用点（位置参数→全关键字），
但这是"契约变更"造成的，与"新增平台"无关。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, runtime_checkable

log = logging.getLogger("sandbox-backends")


@dataclass
class ParsedConfig:
    """配置文件解析结果——通用数据结构，所有平台统一。"""
    endpoint: str
    api_key: str
    max_sandboxes: int = 10
    extra_config: dict = field(default_factory=dict)
    templates: list[dict] = field(default_factory=list)


class RuntimeConfig(TypedDict):
    """DB → Provider runtime config——normalize_db_config 的返回类型。"""
    platform: str
    name: str
    api_url: str
    api_key: str
    max_sandboxes: int
    # 平台特有字段由各 adapter 显式映射，未知字段丢弃并 warn


@dataclass
class FormField:
    """前端表单字段定义——声明式 UI schema。"""
    name: str
    type: str  # text | textarea | bool | select | number | file
    label: str = ""
    help: str = ""
    default: Any = ""
    required: bool = False
    placeholder: str = ""
    options: list[dict] = field(default_factory=list)  # [{value, label}]


@dataclass
class FormGroup:
    """表单分组——支持条件显示和折叠。"""
    title: str = ""
    fields: list[FormField] = field(default_factory=list)
    show_when: str = ""


@dataclass
class FormSchema:
    """完整表单 schema——adapter.ui_schema() 返回。"""
    groups: list[FormGroup] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "groups": [
                {
                    "title": g.title,
                    "show_when": g.show_when,
                    "fields": [
                        {
                            "name": f.name, "type": f.type, "label": f.label,
                            "help": f.help, "default": f.default,
                            "required": f.required, "placeholder": f.placeholder,
                            "options": f.options,
                        }
                        for f in g.fields
                    ],
                }
                for g in self.groups
            ]
        }


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台适配器契约——每个平台实现自己的适配器实例。

    注册表存实例（非 class），方法为实例方法。
    框架通过此契约操作平台，不直接感知任何平台特有字段。
    """

    # --- 元数据 ---
    platform: str        # 必须和模块文件名一致
    display_name: str     # 展示名
    priority: int        # 排序优先级（小的优先，默认 100）
    is_cloud: bool       # 是否云沙箱平台（bwrap/null = False）

    # --- Provider 构造 ---
    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        """由 adapter 负责构造自己的 Provider。全关键字参数。
        bwrap/null 忽略 config；云沙箱使用 config。
        TODO: 未来 Provider 需要新参数时改为 ProviderContext dataclass 打包。"""
        ...

    # --- 数据转换 ---
    def parse_config_file(self, raw: dict) -> ParsedConfig:
        """从配置文件 JSON 解析为 ParsedConfig。"""
        ...

    def normalize_db_config(self, db_row: dict) -> RuntimeConfig:
        """DB 行 → Provider runtime config。
        白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。不豁免 _ 前缀。"""
        ...

    def prepare_artifacts(self, config: dict, workdir: str, provider_id: int) -> None:
        """落盘平台特有文件（如 SSL 证书）。
        允许原地修改 config（如注入 _ssl_cert_path），注入的字段名由 adapter 定义。"""
        ...

    # --- 校验 ---
    def validate_extra_config(self, raw: dict) -> dict:
        """校验 extra_config——必填+类型检查+白名单过滤。
        白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。
        不合法时抛 ValueError，调用方捕获转 HTTPException 400。"""
        ...

    # --- 健康检查 ---
    def health_check(self, config: dict) -> tuple[bool, str]:
        """返回 (healthy, reason)。adapter 自己决定探测方式。"""
        ...

    # --- 模板扫描（可选能力） ---
    def list_templates(self, config: dict) -> list[dict] | None:
        """扫描平台侧已有模板，返回统一格式列表；不支持时返回 None。

        统一格式（与平台 API 解耦）：
        [{"template_id": str, "name": str, "status": str, "description": str}]
        status 用小写字符串（如 ready）；调用方自行判断有效性（ready/ok 视为有效）。
        """
        return None

    # --- 沙箱销毁 ---
    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool:
        """adapter 决定如何构造 Provider 并 kill。"""
        ...

    # --- UI 自描述 ---
    def ui_schema(self) -> FormSchema:
        """返回声明式表单描述，前端通用渲染器消费。"""
        ...

    # --- 生命周期 ---
    def cleanup(self, provider_id: int, workdir: str) -> None:
        """清理落盘文件（provider 删除时调用）。workdir 和 prepare_artifacts 一致。"""
        ...

    # --- [USP] ACP Transport 工厂（docs/sandbox/usp-sandbox-protocol-spec.md §3）---
    def build_transport(self, sandbox, hermes_bin: str, envs: dict):
        """返回该平台的 ACP Transport 实例（CubeSandboxTransport / DevBoxTransport）。

        平台内部自决 PTY vs 文件轮询——executor 的 supports_pty() if-else
        分叉消失，选择逻辑回归 adapter 知识。无 ACP 能力的平台
        NotImplementedError。
        """
        raise NotImplementedError


# ---- [USP] ACP Transport 协议 + 异常族（自 hermes_acp.py 随迁, docs/sandbox/usp-sandbox-protocol-spec.md §3）----
# hermes_acp.py 留 re-export 别名过渡一个 commit（引用点全部切换后删除）。

# ═══════════════ [沙箱迁移] 云沙箱 ACP Transport 层（旧分支移植——CubeSandbox/DevBox 云路径 + _run_acp_session 会话循环）═══════════════
class TransportError(Exception):
    """传输层异常基类。"""


class TransportTimeout(TransportError):
    """可恢复——静默超时，transport 完好，可重试 readline。"""


class TransportClosed(TransportError):
    """不可恢复——进程死了/pipe 关了/解码失败，必须退出。"""


class ACPTransport(Protocol):
    """ACP 传输层接口——NDJSON 分帧（每行一个 JSON-RPC 消息）。"""
    def write(self, data: bytes) -> None: ...
    def readline(self, timeout: float = 30.0) -> str: ...
    def close(self) -> None: ...


def cert_artifact_key(provider_id, config: dict):
    """[锚简化 2026-11-04] rootCA 工件命名键——credential_uid 前 8 位（全局唯一）。

    pid 跨主控撞号时同名文件会错配他主控证书；credential_uid 消除该面。
    旧服务端下发响应无 credential_uid 字段 → 回退 pid（双向兼容）。
    写入（prepare_artifacts）/读取（CA bundle 路径）/清理（cleanup）三处共用，
    键必须一致否则 SSL 断。
    """
    _cuid = (config or {}).get("credential_uid") or ""
    _cuid = _cuid.strip()
    return _cuid[:8] if _cuid else provider_id
