"""plugin_runner 加载插件 manifest（plugin.json）并做路由决策。

仅消费 #1079 plugin.schema.json 已声明的字段（路由所需）；
加载期做结构校验（transport/compat/auth/引用一致性），
坏结构显式 ManifestError，不允许运行时 KeyError 绕过审计或门禁静默放行。
"""
import json
from pathlib import Path

from .errors import ManifestError, PluginRunnerError, UnknownCommand
from .adapters import script_path_allowed

SUPPORTED_TRANSPORTS = ("cli", "http", "mcp-stdio", "mcp-remote", "script")
SUPPORTED_SCRIPT_VIAS = ("http",)
SUPPORTED_INJECTS = ("env", "query", "header", "argv")


class Manifest:
    """plugin.json 的运行时视图（只读）。"""

    def __init__(self, data: dict, *, validate: bool = True):
        self.data = data
        if validate:
            self.validate()

    @classmethod
    def load(cls, source) -> "Manifest":
        """source 为 manifest dict 或 plugin.json 路径。"""
        if isinstance(source, dict):
            return cls(source)
        path = Path(source)
        if path.is_dir():
            path = path / "plugin.json"
        if not path.is_file():
            raise ManifestError(f"plugin.json 未找到: {path}")
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f))
        except json.JSONDecodeError as e:
            raise ManifestError(f"plugin.json 非法 JSON: {path}: {e}") from e

    # ---- 结构校验 --------------------------------------------------------
    def validate(self) -> None:
        if not isinstance(self.data.get("name"), str) or not self.data.get("name"):
            raise ManifestError("manifest 缺少 name")
        self._validate_adapters(self.data.get("adapters"))
        self._validate_commands(self.data.get("commands"))
        self._validate_credentials(self.data.get("credentials"))

    def _validate_adapters(self, adapters) -> None:
        if not isinstance(adapters, list) or not adapters:
            raise ManifestError("manifest.adapters 必须为非空数组")
        ids = set()
        for a in adapters:
            if not isinstance(a, dict):
                raise ManifestError(f"adapter 必须为对象: {a!r}")
            aid = a.get("id")
            if not aid or not isinstance(aid, str):
                raise ManifestError(f"adapter 缺少 id: {a!r}")
            if aid in ids:
                raise ManifestError(f"adapter id 重复: {aid}")
            ids.add(aid)
            transport = a.get("transport")
            if transport not in SUPPORTED_TRANSPORTS:
                raise ManifestError(
                    f"adapter {aid} transport 非法: {transport!r}（支持 {SUPPORTED_TRANSPORTS}）"
                )
            if transport == "script":
                self._validate_script_adapter(aid, a)
            engines = (a.get("compat") or {}).get("engines")
            if not isinstance(engines, list) or not engines:
                raise ManifestError(
                    f"adapter {aid} 缺少 compat.engines（引擎门禁强声明，不做静默放行）"
                )
            self._validate_auth(aid, a.get("auth"))

    def _validate_script_adapter(self, adapter_id: str, adapter: dict) -> None:
        """script 型声明校验：脚本路径白名单 + 产物交付端点（url）必填。

        - script：插件目录内相对路径（禁绝对路径/穿越/盘符/反斜杠），
          白名单在加载期与运行时双重把关（script 是子进程执行入口）；
        - url：脚本产物的交付端点（须 https，schema 层把关）；
        - via：产物交付的传输通道（当前仅 http）。
        """
        script = adapter.get("script")
        if not script:
            raise ManifestError(
                f"adapter {adapter_id} transport=script 缺少 script（签名脚本相对路径）"
            )
        if not script_path_allowed(script):
            raise ManifestError(
                f"adapter {adapter_id} script 路径非法（须为插件目录内相对路径，"
                f"禁绝对路径/穿越/盘符/反斜杠）: {script!r}"
            )
        if not adapter.get("url"):
            raise ManifestError(
                f"adapter {adapter_id} transport=script 缺少 url（脚本产物交付端点）"
            )
        via = adapter.get("via")
        if via is not None and via not in SUPPORTED_SCRIPT_VIAS:
            raise ManifestError(
                f"adapter {adapter_id} script.via 非法: {via!r}（支持 {SUPPORTED_SCRIPT_VIAS}）"
            )

    def _validate_auth(self, adapter_id: str, auth) -> None:
        if auth is None:
            return
        if not isinstance(auth, dict):
            raise ManifestError(f"adapter {adapter_id} auth 必须为对象")
        if not auth.get("credential"):
            raise ManifestError(f"adapter {adapter_id} auth 缺少 credential")
        inject = auth.get("inject")
        if inject is not None and inject not in SUPPORTED_INJECTS:
            raise ManifestError(
                f"adapter {adapter_id} auth.inject 非法: {inject!r}（支持 {SUPPORTED_INJECTS}）"
            )
        template = auth.get("template")
        if template is not None and "{credential}" not in template:
            raise ManifestError(
                f"adapter {adapter_id} auth.template 缺少 {{credential}} 占位符，"
                "凭据将无法注入（明文静默丢弃）"
            )

    def _validate_commands(self, commands) -> None:
        if not isinstance(commands, list) or not commands:
            raise ManifestError("manifest.commands 必须为非空数组")
        adapter_ids = {a.get("id") for a in self.data.get("adapters") or []}
        for c in commands:
            if not isinstance(c, dict):
                raise ManifestError(f"command 必须为对象: {c!r}")
            if not c.get("name"):
                raise ManifestError(f"command 缺少 name: {c!r}")
            refs = c.get("adapters")
            if not isinstance(refs, list) or not refs:
                raise ManifestError(f"command {c['name']} 的 adapters 必须为非空数组")
            for ref in refs:
                if ref not in adapter_ids:
                    raise ManifestError(
                        f"command {c['name']} 引用的 adapter 不存在: {ref}"
                    )
            self._validate_channels(c, adapter_ids)

    def _validate_channels(self, command, adapter_ids) -> None:
        """commands[].channels 优先级声明校验（可选；未声明时 adapters 列表即通道序）。"""
        channels = command.get("channels")
        if channels is None:
            return
        if not isinstance(channels, list) or not channels:
            raise ManifestError(
                f"command {command['name']} 的 channels 必须为非空数组"
            )
        seen = set()
        for ref in channels:
            if ref not in adapter_ids:
                raise ManifestError(
                    f"command {command['name']} 的 channels 引用不存在: {ref}"
                )
            if ref in seen:
                raise ManifestError(
                    f"command {command['name']} 的 channels 通道重复: {ref}"
                )
            seen.add(ref)

    def _validate_credentials(self, credentials) -> None:
        if credentials is None:
            return
        if not isinstance(credentials, dict):
            raise ManifestError("manifest.credentials 必须为对象")
        declared_inject = {}
        for a in self.data.get("adapters") or []:
            auth = a.get("auth") or {}
            if auth.get("inject") is not None:
                declared_inject.setdefault(auth["credential"], set()).add(auth["inject"])
        for name, spec in credentials.items():
            if not isinstance(spec, dict):
                raise ManifestError(f"credentials.{name} 必须为对象")
            inject = spec.get("inject")
            if inject is not None and inject not in SUPPORTED_INJECTS:
                raise ManifestError(
                    f"credentials.{name}.inject 非法: {inject!r}（支持 {SUPPORTED_INJECTS}）"
                )
            uses = declared_inject.get(name)
            if inject is not None and uses and uses != {inject}:
                raise ManifestError(
                    f"credentials.{name}.inject={inject!r} 与 adapter 声明 {sorted(uses)} 漂移"
                )

    # ---- 查询 / 路由 ------------------------------------------------------
    @property
    def name(self) -> str:
        return self.data["name"]

    def command(self, command_name: str) -> dict | None:
        for c in self.data.get("commands", []):
            if c.get("name") == command_name:
                return c
        return None

    def adapter(self, adapter_id: str) -> dict | None:
        for a in self.data.get("adapters", []):
            if a.get("id") == adapter_id:
                return a
        return None

    def credential_required(self, credential_name: str) -> bool:
        spec = (self.data.get("credentials") or {}).get(credential_name) or {}
        return bool(spec.get("required", False))

    def resolve_route(self, command_name: str, adapter_id: str | None = None) -> tuple[dict, dict]:
        """command -> (command 定义, adapter 定义)。不做引擎门禁（由 Runner 做，错误信息带引擎）。"""
        cmd = self.command(command_name)
        if cmd is None:
            raise UnknownCommand(f"命令未在 manifest.commands 声明: {command_name}")
        want = adapter_id or (cmd.get("adapters") or [None])[0]
        ad = self.adapter(want) if want else None
        if ad is None:
            raise PluginRunnerError(f"命令 {command_name} 引用的 adapter 不存在: {want}")
        return cmd, ad

    def resolve_channels(self, command_name: str, adapter_id: str | None = None) -> tuple[dict, list]:
        """command -> (command 定义, 按优先级降序的通道列表)（failover 通道序，#1311）。

        - 显式 adapter_id：单通道直选（不参与动态回落）；
        - 声明 channels：按 channels 顺序（优先级降序）；
        - 未声明 channels：沿用旧 adapters 列表顺序（旧单 adapter 写法等价单通道，
          向后兼容，零行为变化）。
        不做引擎门禁（由 Runner 做，错误信息带引擎）。
        """
        cmd = self.command(command_name)
        if cmd is None:
            raise UnknownCommand(f"命令未在 manifest.commands 声明: {command_name}")
        ordered = [adapter_id] if adapter_id else (cmd.get("channels") or cmd.get("adapters") or [])
        channels = []
        for ref in ordered:
            ad = self.adapter(ref)
            if ad is None:
                raise PluginRunnerError(f"命令 {command_name} 引用的 adapter 不存在: {ref}")
            channels.append(ad)
        if not channels:
            raise PluginRunnerError(f"命令 {command_name} 未声明任何通道")
        return cmd, channels
