"""PluginRunner：插件调用统一入口（路由 + 凭据注入 + 墙钟超时 + JSONL 审计）。

凭据链路（D6/方案口径）：明文仅在 run() 调用瞬间由调用方/凭据库 provider 提供，
经内存进入注入位（env/query/header/argv）直达目标 API；
任何被审计/结果回传的文本先经 redact() 掩码，明文不进日志/审计/LLM 对话。
"""
import json
import time
from dataclasses import dataclass

from .adapters import (
    ChannelHealth,
    FALLBACK_REMOVAL_THRESHOLD,
    call_cli,
    call_http,
    call_mcp_remote,
    call_mcp_stdio,
    call_script,
    resolve_script_path,
    try_call_with_fallback,
)
from .audit import JsonlAuditWriter
from .credentials import build_argv, build_env, build_headers, build_query, redact
from .errors import (
    CallTimeout,
    EngineNotSupported,
    MissingCredential,
    PluginRunnerError,
    TransportError,
)
from .manifest import Manifest


@dataclass
class CallResult:
    command: str
    adapter_id: str | None
    transport: str | None
    outcome: str  # ok | error | timeout
    exit_code: int | None = None
    output: str | None = None
    error: str | None = None
    duration_ms: int = 0


@dataclass
class _CallContext:
    """单次调用的参数束（run → dispatch → adapter 间传递，替代成串散参）。"""

    cmd_name: str
    adapter: dict
    params: dict | None
    secret: str | None
    timeout: float


class PluginRunner:
    """以 manifest 驱动的薄 Runner：command -> adapter -> 执行。

    manifest 接受 dict 或 plugin.json 路径；结构合法性由 A1 L0 校验器把关。
    """

    def __init__(
        self,
        manifest,
        *,
        audit_path=None,
        timeout_seconds: float = 60.0,
        engine: str | None = None,
        workdir=None,
        plugin_dir=None,
        credential_provider=None,
        health: ChannelHealth | None = None,
    ):
        self.manifest = Manifest.load(manifest)
        self.timeout_seconds = timeout_seconds
        self.engine = engine
        self.workdir = str(workdir) if workdir else None
        self.plugin_dir = str(plugin_dir) if plugin_dir else None
        self.credential_provider = credential_provider
        self.health = health or ChannelHealth(
            removal_threshold=FALLBACK_REMOVAL_THRESHOLD
        )
        self.audit = JsonlAuditWriter(audit_path) if audit_path else None

    # ---- 公开入口 -------------------------------------------------------
    def run(
        self,
        command: str,
        params: dict | None = None,
        *,
        credentials: dict | None = None,
        adapter_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CallResult:
        t0 = time.monotonic()
        secrets: list = []
        resolved_adapter: str | None = None
        resolved_transport: str | None = None
        try:
            cmd, channels = self.manifest.resolve_channels(command, adapter_id)
            primary = channels[0]
            resolved_adapter = primary.get("id")
            resolved_transport = primary.get("transport")
            chain = self.health.build_chain(self._engine_channels(channels))
            timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
            result, _served = try_call_with_fallback(
                chain,
                lambda adapter: self._attempt(
                    cmd, adapter, params, credentials, timeout, secrets
                ),
                health=self.health,
                on_fallback=self._fallback_auditor(command, secrets),
            )
        except PluginRunnerError as e:
            if self.audit:
                self._write_audit(
                    {
                        "ts": _ts(),
                        "plugin": self.manifest.name,
                        "command": command,
                        "adapter": resolved_adapter,
                        "transport": resolved_transport,
                        "engine": self.engine,
                        "outcome": "rejected",
                        "exit_code": None,
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                        "error": redact(str(e), secrets),
                    },
                    secrets,
                )
            raise
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        result.output = redact(result.output, secrets)
        result.error = redact(result.error, secrets)
        if self.audit:
            self._write_audit(
                {
                    "ts": _ts(),
                    "plugin": self.manifest.name,
                    "command": result.command,
                    "adapter": result.adapter_id,
                    "transport": result.transport,
                    "engine": self.engine,
                    "outcome": result.outcome,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                },
                secrets,
            )
        return result

    # ---- 路由 / 凭据 -----------------------------------------------------
    def _engine_channels(self, channels) -> list:
        """D7 引擎门禁：过滤调用引擎不兼容的通道；全不兼容时按主通道显式拒绝。

        引擎兼容性是清单级强声明（不做动态回落）：门禁失败按原语义拒绝，
        而非回落到下一个通道。
        """
        if not self.engine:
            return channels
        compatible = [a for a in channels if not self._engine_blocked(a)]
        if not compatible:
            a = channels[0]
            engines = (a.get("compat") or {}).get("engines") or []
            raise EngineNotSupported(
                f"adapter {a['id']} 的 compat.engines={engines} 不含引擎 {self.engine}，"
                "不做动态回落"
            )
        return compatible

    def _engine_blocked(self, adapter) -> bool:
        engines = (adapter.get("compat") or {}).get("engines") or []
        return bool(self.engine and engines and self.engine not in engines)

    def _resolve_credential(self, adapter, credentials, secrets):
        """解析 adapter.auth 声明的凭据；required 缺失即拒绝，明文仅存内存。"""
        auth = adapter.get("auth")
        if not auth:
            return None, None
        name = auth["credential"]
        secret = (credentials or {}).get(name)
        if not secret and self.credential_provider:
            secret = self.credential_provider(name)
        if not secret:
            if self.manifest.credential_required(name):
                raise MissingCredential(f"凭据 {name} 为 required，调用时未提供")
            return None, auth
        secrets.append(secret)
        return secret, auth

    # ---- 分发 -----------------------------------------------------------
    def _attempt(self, cmd, adapter, params, credentials, timeout, secrets) -> CallResult:
        """单通道尝试：凭据按通道解析（各通道可声明不同凭据/注入位）+ 分发。"""
        secret, auth = self._resolve_credential(adapter, credentials, secrets)
        return self._dispatch(cmd, adapter, params, secret, timeout, secrets)

    def _fallback_auditor(self, command: str, secrets):
        """回落审计回调：每次回落一行（通道 / 回落目标 / 原因 / 耗时）。"""
        if not self.audit:
            return None

        def on_fallback(failed, nxt, reason, duration_ms):
            self._write_audit(
                {
                    "ts": _ts(),
                    "plugin": self.manifest.name,
                    "command": command,
                    "event": "fallback",
                    "outcome": "fallback",
                    "adapter": failed.get("id"),
                    "transport": failed.get("transport"),
                    "engine": self.engine,
                    "fallback_to": nxt.get("id"),
                    "reason": reason,
                    "exit_code": None,
                    "duration_ms": duration_ms,
                },
                secrets,
            )

        return on_fallback

    def _dispatch(self, cmd, adapter, params, secret, timeout, secrets) -> CallResult:
        transport = adapter["transport"]
        handler = self._DISPATCH.get(transport)
        if handler is None:
            raise PluginRunnerError(f"不支持的 transport: {transport}")
        ctx = _CallContext(
            cmd_name=cmd["name"],
            adapter=adapter,
            params=params,
            secret=secret,
            timeout=timeout,
        )
        try:
            return CallResult(
                command=ctx.cmd_name,
                adapter_id=adapter["id"],
                transport=transport,
                **handler(self, ctx),
            )
        except CallTimeout as e:
            return CallResult(
                command=ctx.cmd_name,
                adapter_id=adapter["id"],
                transport=transport,
                outcome="timeout",
                error=str(e),
            )

    def _cli_call(self, ctx: "_CallContext") -> dict:
        adapter = ctx.adapter
        inject_env, inject_argv = {}, []
        if adapter.get("auth") and ctx.secret is not None:
            inject = adapter["auth"]["inject"]
            if inject == "env":
                inject_env = build_env(adapter["auth"], ctx.secret)
            elif inject == "argv":
                inject_argv = build_argv(adapter["auth"], ctx.secret)
            else:
                raise PluginRunnerError(
                    f"注入位 {inject} 不适用于 cli transport（支持 env/argv）"
                )
        exit_code, out, err = call_cli(
            adapter,
            ctx.params,
            inject_env=inject_env,
            inject_argv=inject_argv,
            timeout_seconds=ctx.timeout,
            workdir=self.workdir,
            venv_dir=self.plugin_dir,
        )
        outcome = "ok" if exit_code == 0 else "error"
        return {
            "outcome": outcome,
            "exit_code": exit_code,
            "output": out,
            "error": None if exit_code == 0 else (err or f"exit={exit_code}"),
        }

    def _http_call(self, ctx: "_CallContext") -> dict:
        adapter = ctx.adapter
        headers, query = {}, {}
        if adapter.get("auth") and ctx.secret is not None:
            inject = adapter["auth"]["inject"]
            if inject == "header":
                headers = build_headers(adapter["auth"], ctx.secret)
            elif inject == "query":
                query = build_query(adapter["auth"], ctx.secret)
            elif inject == "env":
                # GET 无子进程环境，env 注入位按 query 语义落地（凭据名作参数名）
                query = build_query(adapter["auth"], ctx.secret)
            else:
                raise PluginRunnerError(
                    f"注入位 {inject} 不适用于 http transport（支持 header/query/env）"
                )
        status, body, error = call_http(
            adapter,
            ctx.params,
            inject_headers=headers,
            inject_query=query,
            timeout_seconds=ctx.timeout,
        )
        return {
            "outcome": "ok" if error is None else "error",
            "exit_code": status,
            "output": body,
            "error": error,
        }

    def _script_call(self, ctx: "_CallContext") -> dict:
        """script adapter：子进程执行签名脚本 → 产物（签名 params/headers）交 call_http。

        script 型是内建第五型通道（#1311 裁决）：签名逻辑进插件自带脚本
        （路径白名单限插件目录内），产物复用既有传输与审计体系。
        """
        adapter = ctx.adapter
        inject_env = {}
        if adapter.get("auth") and ctx.secret is not None:
            inject = adapter["auth"]["inject"]
            if inject != "env":
                raise PluginRunnerError(
                    f"注入位 {inject} 不适用于 script transport（支持 env）"
                )
            inject_env = build_env(adapter["auth"], ctx.secret)
        if not self.plugin_dir:
            raise PluginRunnerError(
                "script transport 需要插件目录（plugin_dir）以解析脚本白名单"
            )
        # 提前解析一次：加载后被篡改的越目录路径在此拒绝（白名单双重把关）
        resolve_script_path(adapter.get("script"), self.plugin_dir)
        exit_code, out, err = call_script(
            adapter,
            ctx.params,
            command_name=ctx.cmd_name,
            inject_env=inject_env,
            timeout_seconds=ctx.timeout,
            workdir=self.workdir,
            plugin_dir=self.plugin_dir,
        )
        if exit_code != 0:
            return {
                "outcome": "error",
                "exit_code": exit_code,
                "output": None,
                "error": err or f"exit={exit_code}",
            }
        try:
            artifact = json.loads(out or "")
        except ValueError as e:
            raise TransportError(f"script 产物非法 JSON: {e}") from e
        if not isinstance(artifact, dict):
            raise TransportError("script 产物须为 JSON 对象（含 params/headers）")
        # 产物交传输通道（via=http）：签名 params/headers 并入 http 调用
        status, body, error = call_http(
            {"url": adapter["url"]},
            artifact.get("params") or {},
            inject_headers=artifact.get("headers") or {},
            timeout_seconds=ctx.timeout,
        )
        return {
            "outcome": "ok" if error is None else "error",
            "exit_code": status,
            "output": body,
            "error": error,
        }

    def _mcp_stdio_call(self, ctx: "_CallContext") -> dict:
        adapter = ctx.adapter
        inject_env = {}
        if adapter.get("auth") and ctx.secret is not None:
            inject = adapter["auth"]["inject"]
            if inject == "env":
                inject_env = build_env(adapter["auth"], ctx.secret)
            else:
                raise PluginRunnerError(
                    f"注入位 {inject} 不适用于 mcp-stdio transport（支持 env）"
                )
        exit_code, text, error = call_mcp_stdio(
            adapter,
            ctx.params,
            command_name=ctx.cmd_name,
            inject_env=inject_env,
            timeout_seconds=ctx.timeout,
            workdir=self.workdir,
            venv_dir=self.plugin_dir,
        )
        return {
            "outcome": "ok" if error is None else "error",
            "exit_code": exit_code,
            "output": text,
            "error": error,
        }

    def _mcp_remote_call(self, ctx: "_CallContext") -> dict:
        adapter = ctx.adapter
        inject_headers = {}
        if adapter.get("auth") and ctx.secret is not None:
            inject = adapter["auth"]["inject"]
            if inject == "header":
                inject_headers = build_headers(adapter["auth"], ctx.secret)
            else:
                raise PluginRunnerError(
                    f"注入位 {inject} 不适用于 mcp-remote transport（支持 header）"
                )
        status, text, error = call_mcp_remote(
            adapter,
            ctx.params,
            command_name=ctx.cmd_name,
            inject_headers=inject_headers,
            timeout_seconds=ctx.timeout,
        )
        return {
            "outcome": "ok" if error is None else "error",
            "exit_code": status,
            "output": text,
            "error": error,
        }

    # transport -> 处理函数（dict 分发，替代 if 链）
    _DISPATCH = {
        "cli": _cli_call,
        "http": _http_call,
        "mcp-stdio": _mcp_stdio_call,
        "mcp-remote": _mcp_remote_call,
        "script": _script_call,
    }

    # ---- 审计 -----------------------------------------------------------
    def _write_audit(self, record: dict, secrets) -> None:
        self.audit.append(record, secrets=secrets)


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
