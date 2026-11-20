"""Grape 插件市场 L0 校验器（Issue #1079，总纲 #1078 A1）。

L0 = 纯静态校验：不依赖凭据、不依赖沙箱，作为上传/上架前的发布门禁。
三个子检查：
- check_schema    ① schema 结构校验（contracts/plugin.schema.json，draft 2020-12，错误定位到字段路径）
- check_binaries  ② 禁二进制扫描（magic bytes / 文件后缀 / 命令路径白名单核验）
- check_commands  ③ 命令有效性免凭据探测（方法名探测 + 401 分层）

错误统一为 ValidationError：check / code / path / reason / hint / severity，
path 采用 `adapters[0].compat.engines` 风格，根对象记为 `(root)`。
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urlparse

from src.domains.mgmt.plugin_market.schema import load_plugin_schema

CHECK_SCHEMA = "schema"
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass
class ValidationError:
    """结构化校验错误：路径 + 原因 + 修复提示。"""

    check: str
    code: str
    path: str
    reason: str
    hint: str
    severity: str = SEVERITY_ERROR

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "code": self.code,
            "path": self.path,
            "reason": self.reason,
            "hint": self.hint,
            "severity": self.severity,
        }


def _friendly_path(json_path: str) -> str:
    """jsonschema 的 $.adapters[0].id → adapters[0].id；根 → (root)。"""
    if not json_path or json_path == "$":
        return "(root)"
    p = json_path
    if p.startswith("$"):
        p = p[1:]
    if p.startswith("."):
        p = p[1:]
    return p or "(root)"


_HINTS = (
    ("is a required property", "补充该必填字段"),
    ("is not one of", "取值需为 schema 枚举内的合法值"),
    ("is not one of the allowed values", "取值需为 schema 枚举内的合法值"),
    ("was expected", "取值需满足 schema 约束（const）"),
    ("should not be valid under", "该取值与当前 transport 的约束冲突，请按 schema 调整"),
    ("does not match", "格式不满足 schema pattern 要求"),
    ("Additional properties are not allowed", "移除该未知字段（清单 schema 禁止未声明字段）"),
    ("is not of type", "修正字段类型"),
    ("is not valid under any of the given schemas", "该字段未满足任一分支约束，详见 contracts/plugin.schema.json"),
    ("is too short", "数组/字符串长度不满足要求"),
    ("is too long", "数组/字符串长度超出上限"),
    ("is not unique", "数组元素必须唯一"),
)


def _hint_for(message: str) -> str:
    for key, hint in _HINTS:
        if key in message:
            return hint
    return "对照 contracts/plugin.schema.json 修正该字段"


def _raw_schema_errors(manifest: dict) -> list[ValidationError]:
    """纯 JSON Schema（draft 2020-12）校验，错误映射为 ValidationError。"""
    import re

    from jsonschema import Draft202012Validator

    schema = load_plugin_schema()
    validator = Draft202012Validator(schema)
    out: list[ValidationError] = []
    for err in sorted(validator.iter_errors(manifest), key=lambda e: (list(e.absolute_path), e.message)):
        base = _friendly_path(err.json_path)
        if err.validator == "required":
            # 对象级 required 错误 → 定位到缺失字段本身（name / adapters[0].command）
            for prop in re.findall(r"'([^']+)' is a required property", err.message):
                path = prop if base == "(root)" else f"{base}.{prop}"
                out.append(ValidationError(
                    check=CHECK_SCHEMA, code="schema.required", path=path,
                    reason=err.message, hint=_hint_for(err.message),
                ))
            continue
        if err.validator == "additionalProperties":
            # 对象级 additionalProperties 错误 → 定位到多余字段本身
            for prop in re.findall(r"\('([^']+)' was unexpected\)", err.message) or \
                    re.findall(r"\('([^']+)'", err.message):
                path = prop if base == "(root)" else f"{base}.{prop}"
                out.append(ValidationError(
                    check=CHECK_SCHEMA, code="schema.additionalProperties", path=path,
                    reason=err.message, hint=_hint_for(err.message),
                ))
            continue
        out.append(
            ValidationError(
                check=CHECK_SCHEMA,
                code=f"schema.{err.validator}",
                path=base,
                reason=err.message,
                hint=_hint_for(err.message),
            )
        )
    return out


def _duplicate_adapter_id_errors(adapters: list) -> list[ValidationError]:
    """adapter id 唯一性。"""
    out: list[ValidationError] = []
    seen_ids: set[str] = set()
    for i, ad in enumerate(adapters):
        aid = ad.get("id", "")
        if aid in seen_ids:
            out.append(ValidationError(
                check=CHECK_SCHEMA, code="schema.semantic",
                path=f"adapters[{i}].id",
                reason=f"adapter id 重复: {aid}",
                hint="每个 adapter 的 id 必须唯一，command.adapters 通过它引用实现",
            ))
        seen_ids.add(aid)
    return out


def _command_reference_errors(commands: list, adapter_ids: set[str]) -> list[ValidationError]:
    """command 名唯一 + adapters/channels 引用完整性（引用必须指向已声明的 adapter id）。"""
    out: list[ValidationError] = []
    seen_cmds: set[str] = set()
    for i, cmd in enumerate(commands):
        cname = cmd.get("name", "")
        if cname in seen_cmds:
            out.append(ValidationError(
                check=CHECK_SCHEMA, code="schema.semantic",
                path=f"commands[{i}].name",
                reason=f"command 名重复: {cname}",
                hint="command 是统一命令面，重名会破坏命令路由",
            ))
        seen_cmds.add(cname)
        for j, ref in enumerate(cmd.get("adapters") or []):
            if ref not in adapter_ids:
                out.append(ValidationError(
                    check=CHECK_SCHEMA, code="schema.semantic",
                    path=f"commands[{i}].adapters[{j}]",
                    reason=f"引用了不存在的 adapter: {ref}",
                    hint=f"可用的 adapter id: {', '.join(sorted(adapter_ids)) or '（无）'}",
                ))
        for j, ref in enumerate(cmd.get("channels") or []):
            # channels 优先级声明（Issue #1311 failover）：引用完整性与 adapters 同语义
            if ref not in adapter_ids:
                out.append(ValidationError(
                    check=CHECK_SCHEMA, code="schema.semantic",
                    path=f"commands[{i}].channels[{j}]",
                    reason=f"引用了不存在的 adapter: {ref}",
                    hint=f"可用的 adapter id: {', '.join(sorted(adapter_ids)) or '（无）'}",
                ))
    return out


def _credential_reference_errors(adapters: list, credentials: dict) -> list[ValidationError]:
    """adapter.auth.credential 必须引用 credentials 表键。"""
    out: list[ValidationError] = []
    for i, ad in enumerate(adapters):
        cred = (ad.get("auth") or {}).get("credential")
        if cred and cred not in credentials:
            out.append(ValidationError(
                check=CHECK_SCHEMA, code="schema.semantic",
                path=f"adapters[{i}].auth.credential",
                reason=f"auth.credential 引用了未声明的凭据: {cred}",
                hint=f"请在顶层 credentials 中声明 {cred}（required/inject/scope/expiry）",
            ))
    return out


def _network_whitelist_warnings(adapters: list, permissions: dict) -> list[ValidationError]:
    """http/mcp-remote 出网域名白名单覆盖（声明性提示）。"""
    out: list[ValidationError] = []
    network = permissions.get("network") or []
    for i, ad in enumerate(adapters):
        url = ad.get("url")
        if not url:
            continue
        host = url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
        if not network:
            out.append(ValidationError(
                check=CHECK_SCHEMA, code="schema.semantic", severity=SEVERITY_WARNING,
                path=f"adapters[{i}].url",
                reason=f"声明了远端端点但未声明 permissions.network 出网域名白名单: {host}",
                hint="建议在 permissions.network 声明该域名，便于上架评审与后续 L1/L2 强制",
            ))
        elif not _host_allowed(host, network):
            out.append(ValidationError(
                check=CHECK_SCHEMA, code="schema.semantic", severity=SEVERITY_WARNING,
                path=f"adapters[{i}].url",
                reason=f"出网域名白名单未覆盖: {host}",
                hint=f"将 {host} 加入 permissions.network（或使用 *.通配）",
            ))
    return out


def _semantic_errors(manifest: dict) -> list[ValidationError]:
    """跨字段语义检查（JSON Schema 难以表达的引用完整性等）。"""
    adapters = manifest.get("adapters") or []
    commands = manifest.get("commands") or []
    adapter_ids = {a.get("id", "") for a in adapters}
    return [
        *(_duplicate_adapter_id_errors(adapters)),
        *(_command_reference_errors(commands, adapter_ids)),
        *(_credential_reference_errors(adapters, manifest.get("credentials") or {})),
        *(_network_whitelist_warnings(adapters, manifest.get("permissions") or {})),
    ]


def _host_allowed(host: str, whitelist: list[str]) -> bool:
    for entry in whitelist:
        entry = entry.lower()
        if entry.startswith("*."):
            if host == entry[2:] or host.endswith("." + entry[2:]):
                return True
        elif host == entry:
            return True
    return False


def check_schema(manifest: dict) -> list[ValidationError]:
    """① schema 结构校验：JSON Schema（draft 2020-12）+ 跨字段语义检查。

    返回 ValidationError 列表（severity=error 拒绝；warning 仅提示）；空列表表示通过。
    """
    if not isinstance(manifest, dict):
        return [ValidationError(
            check=CHECK_SCHEMA, code="schema.type", path="(root)",
            reason=f"manifest 必须是 JSON 对象，实际为 {type(manifest).__name__}",
            hint="plugin.json 顶层必须是对象",
        )]
    return _raw_schema_errors(manifest) + _semantic_errors(manifest)


CHECK_BINARY = "binary"

# 可执行/二进制产物后缀黑名单：插件包应为纯文本分发（脚本 + manifest + skills）
BINARY_SUFFIXES = (
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".obj", ".a", ".lib",
    ".pyc", ".pyo", ".class", ".jar", ".wasm", ".node",
    ".deb", ".rpm", ".msi", ".apk", ".dmg", ".app", ".framework",
    ".7z", ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar",
)

# 常见可执行二进制 magic bytes（shebang/UTF-8 文本不受影响）
BINARY_MAGICS = (
    b"\x7fELF",        # Linux ELF
    b"MZ",             # Windows PE
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",   # Mach-O 32
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",   # Mach-O 64
    b"\xca\xfe\xba\xbe",                        # Mach-O fat / Java class
    b"\x00\x00\x01\x00",                        # ICO
)


def _is_binary_file(path: str) -> str | None:
    """返回判定原因；非二进制返回 None。先查后缀，再查 magic bytes。"""
    lower = path.lower()
    for suffix in BINARY_SUFFIXES:
        if lower.endswith(suffix):
            return f"二进制产物后缀 {suffix}"
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    for magic in BINARY_MAGICS:
        if head.startswith(magic):
            return f"命中二进制 magic bytes {magic!r}"
    return None


def _scan_binaries(plugin_dir: str) -> list[ValidationError]:
    out: list[ValidationError] = []
    base = os.path.abspath(plugin_dir)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(root, name), base).replace(os.sep, "/")
            why = _is_binary_file(os.path.join(root, name))
            if why:
                out.append(ValidationError(
                    check=CHECK_BINARY, code="binary.file", path=rel,
                    reason=f"检测到二进制产物: {rel}（{why}）",
                    hint="插件包仅允许文本产物（脚本/manifest/skills）；请移除二进制文件并在安装期由用户环境提供可执行依赖",
                ))
    return out


def _check_command_paths(manifest: dict) -> list[ValidationError]:
    """命令路径白名单核验：adapter.command 禁止路径穿越；须落入 permissions.exec 白名单。

    cli/mcp-stdio 适配器的 command 会被免凭据探测（probe）以子进程真实执行，
    因此 permissions.exec 未声明时同样拒绝（不做静默跳过），强制 command ∈ 白名单。
    """
    out: list[ValidationError] = []
    adapters = manifest.get("adapters") or []
    exec_list = (manifest.get("permissions") or {}).get("exec") or []
    for i, ad in enumerate(adapters):
        transport = ad.get("transport")
        command = ad.get("command")
        if not command:
            continue
        if "/" in command or "\\" in command or ".." in command:
            out.append(ValidationError(
                check=CHECK_BINARY, code="binary.command_path",
                path=f"adapters[{i}].command",
                reason=f"command 含路径分隔符/穿越: {command}",
                hint="command 只允许可执行文件名（如 demo-weather），不允许路径",
            ))
            continue
        if transport in ("cli", "mcp-stdio"):
            if not exec_list:
                out.append(ValidationError(
                    check=CHECK_BINARY, code="binary.exec_whitelist_missing",
                    path=f"adapters[{i}].command",
                    reason=f"permissions.exec 白名单未声明，无法核验命令: {command}",
                    hint="在 permissions.exec 中声明该命令（探测/执行前均须先过白名单核验）",
                ))
            elif command not in exec_list:
                out.append(ValidationError(
                    check=CHECK_BINARY, code="binary.exec_whitelist",
                    path=f"adapters[{i}].command",
                    reason=f"adapter command 未落入 permissions.exec 白名单: {command}",
                    hint=f"将 {command} 加入 permissions.exec，或从白名单语义上覆盖它",
                ))
    return out


def _check_bundled_skill_paths(manifest: dict) -> list[ValidationError]:
    """bundled_skills 路径安全核验：拒绝绝对路径/盘符/.. 穿越/反斜杠（D1 纵深防御）。

    schema 层已用 pattern 拦截，这里独立复核，避免未来 schema 放松后校验器失守。
    """
    out: list[ValidationError] = []
    for i, skill in enumerate(manifest.get("bundled_skills") or []):
        path = (skill or {}).get("path", "") if isinstance(skill, dict) else ""
        normalized = path.replace("\\", "/")
        unsafe = (
            not path
            or path[0] in "/\\"
            or (len(path) >= 2 and path[1] == ":")
            or "\\" in path
            or ".." in normalized.split("/")
        )
        if unsafe:
            out.append(ValidationError(
                check=CHECK_BINARY, code="binary.skill_path",
                path=f"bundled_skills[{i}].path",
                reason=f"bundled_skills.path 含绝对路径或路径穿越: {path}",
                hint="path 必须是插件包内相对路径（禁止 .. 段、绝对路径、盘符与反斜杠）",
            ))
    return out


# script 路径段白名单（与 plugin.schema.json script pattern /
# plugin_runner.adapters.SCRIPT_SEGMENT_RE 同一口径；
# 跨实现对拍钉扎：tests/test_plugin_market_l0.py::TestScriptPathRuleParity）
_SCRIPT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_script_paths(manifest: dict) -> list[ValidationError]:
    """script adapter 路径安全核验（Issue #1311）：拒绝绝对路径/盘符/.. 穿越/
    反斜杠/空段/`.` 段/段内非法字符。

    schema 层已用 pattern 拦截，这里独立复核（与 bundled_skills 同纵深防御口径）——
    script 是子进程执行入口，路径越界 = 任意代码执行面。
    与 plugin_runner.script_path_allowed 同口径（TestScriptPathRuleParity 对拍
    钉扎，防三处实现规则漂移）。
    """
    out: list[ValidationError] = []
    for i, ad in enumerate(manifest.get("adapters") or []):
        path = (ad or {}).get("script", "") if isinstance(ad, dict) else ""
        if (ad or {}).get("transport") != "script" and not path:
            continue
        unsafe = (
            not isinstance(path, str)
            or not path
            or path[0] in "/\\"
            or (len(path) >= 2 and path[1] == ":")
            or "\\" in path
            or not all(
                _SCRIPT_SEGMENT_RE.fullmatch(p) for p in path.split("/")
            )
        )
        if unsafe:
            out.append(ValidationError(
                check=CHECK_BINARY, code="binary.script_path",
                path=f"adapters[{i}].script",
                reason=f"script 路径含绝对路径或路径穿越: {path}",
                hint="script 必须是插件包内相对路径（禁止 .. 段、绝对路径、盘符与反斜杠）",
            ))
    return out


def check_binaries(plugin_dir: str, manifest: dict) -> list[ValidationError]:
    """② 禁二进制扫描：包内文件 magic bytes/后缀扫描 + 命令路径白名单核验。

    plugin_dir 为插件包目录（含 plugin.json 与随包产物）；返回 ValidationError 列表。
    """
    errors: list[ValidationError] = []
    if plugin_dir and os.path.isdir(plugin_dir):
        errors.extend(_scan_binaries(plugin_dir))
    errors.extend(_check_command_paths(manifest))
    errors.extend(_check_bundled_skill_paths(manifest))
    errors.extend(_check_script_paths(manifest))
    return errors


# ============================== ③ 免凭据命令探测 ==============================

CHECK_PROBE = "probe"
PROBE_OK = "ok"
PROBE_AUTH_REQUIRED = "auth_required"
PROBE_INVALID = "invalid"


@dataclass
class ProbeResult:
    """单次命令探测结果：ok=命令有效；auth_required=命令存在仅缺凭据（401 分层）；invalid=命令无效。"""

    status: str
    evidence: str = ""
    reason: str = ""
    hint: str = ""


def classify_probe_status(http_status: int) -> str:
    """HTTP 状态 → 探测结论（白名单式严格判定，401 分层保留）。

    2xx / 405 → ok（405=方法不被支持但端点存在）；
    401/403 → auth_required（命令存在仅缺凭据）；
    404 → invalid；其余（3xx/400/5xx 等）→ invalid（无法确认命令有效，
    3xx 重定向不跟随、5xx 不能证明命令存在）。
    """
    if http_status in (401, 403):
        return PROBE_AUTH_REQUIRED
    if http_status == 404:
        return PROBE_INVALID
    if 200 <= http_status < 300 or http_status == 405:
        return PROBE_OK
    return PROBE_INVALID


# —— 私网/环回防护（默认拒绝探测内网地址，防 SSRF）——
_PRIVATE_NETS = (
    ipaddress.ip_network("0.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"), ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _addr_is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _PRIVATE_NETS)


def probe_cli(command: str, args: list | None = None, cwd: str | None = None,
              timeout: float = 10.0) -> ProbeResult:
    """cli 方法名探测：`command --help`。

    命令能在超时内响应 --help → ok；进程能启动但超时无响应/不存在/不可执行 → invalid
    （挂死进程不能证明命令有效，与 http 探测白名单式严格判定一致）。
    """
    argv = [command, *(args or []), "--help"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        return ProbeResult(PROBE_INVALID, reason=f"命令不存在: {command}",
                           hint="确认 CLI 已安装或已在 PATH 中（免凭据探测以 --help 方法名探测）")
    except subprocess.TimeoutExpired:
        return ProbeResult(
            PROBE_INVALID, reason=f"--help 探测超时（{timeout}s 无响应）",
            hint="命令能启动但未在超时内响应 --help，不能证明命令有效；请检查命令是否挂起")
    except OSError as e:
        return ProbeResult(PROBE_INVALID, reason=f"命令不可执行: {e}",
                           hint="确认该命令对服务端运行用户可执行")
    first = (proc.stdout or proc.stderr or "").strip().splitlines()
    return ProbeResult(PROBE_OK, evidence=f"exit={proc.returncode} {first[0][:120] if first else ''}")


def _looks_like_jsonrpc(text: str) -> bool:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{") and ('"jsonrpc"' in line or '"result"' in line or '"error"' in line):
            return True
    return False


def probe_mcp_stdio(command: str, args: list | None = None, cwd: str | None = None,
                    timeout: float = 10.0) -> ProbeResult:
    """mcp-stdio 方法名探测：stdin 发送 `tools/list` JSON-RPC。

    有 JSON-RPC 响应 → ok；启动失败/异常退出无响应/超时无响应 → invalid
    （常驻但未响应 tools/list 不能证明命令可用，与 exit=0 无响应同口径）。
    """
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    try:
        proc = subprocess.Popen([command, *(args or [])], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=cwd)
    except FileNotFoundError:
        return ProbeResult(PROBE_INVALID, reason=f"命令不存在: {command}",
                           hint="确认 MCP server 命令已安装或已在 PATH 中")
    except OSError as e:
        return ProbeResult(PROBE_INVALID, reason=f"命令不可执行: {e}",
                           hint="确认该命令对服务端运行用户可执行")
    try:
        out, err = proc.communicate(req + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return ProbeResult(
            PROBE_INVALID, reason=f"tools/list 探测超时（{timeout}s 无 JSON-RPC 响应）",
            hint="确认 MCP server 能在超时内响应 tools/list（挂起/无响应视为命令无效）")
    if _looks_like_jsonrpc(out):
        return ProbeResult(PROBE_OK, evidence="tools/list JSON-RPC 响应")
    tail = (err or out or "").strip().replace("\n", " ")[:200]
    return ProbeResult(
        PROBE_INVALID, reason=f"无 JSON-RPC 响应: exit={proc.returncode} {tail}",
        hint="确认 MCP server 能启动并响应 tools/list（连接失败/无响应/非 JSON-RPC 输出视为命令无效）")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP 直连已解析 IP，TLS SNI/证书校验仍用原始主机名（防 DNS 重绑定 TOCTOU）。"""

    def __init__(self, pinned_ip: str, hostname: str, **kwargs):
        super().__init__(pinned_ip, **kwargs)
        self._pinned_ip = pinned_ip
        self._pinned_hostname = hostname

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._pinned_hostname)


@dataclass
class _ProbeTarget:
    """已解析并钉定的探测目标：连接用 pinned_ip，Host/TLS SNI 用原主机名。"""

    url: str
    scheme: str
    host: str
    port: int
    netloc: str
    pinned_ip: str
    request_path: str


def _resolve_probe_target(url: str, allow_private: bool) -> _ProbeTarget | ProbeResult:
    """URL 解析 + DNS 解析 + 私网比对 + IP 钉定；失败返回 ProbeResult(invalid)。

    SSRF 防护前两段在这里完成：
    ① getaddrinfo 解析后对全部解析结果做私网/环回比对（默认拒绝内网，
       十进制/IPv6 变体均在解析后拦截）；
    ② 解析结果 → 连接一一对应：直接连接已解析 IP（消除「校验后连接」之间的
       DNS 重绑定 TOCTOU 窗口；Host/SNI 仍用原主机名）。
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return ProbeResult(PROBE_INVALID, reason=f"不支持的 URL scheme: {parsed.scheme}",
                           hint="仅支持 http/https 端点探测")
    host = parsed.hostname
    if not host:
        return ProbeResult(PROBE_INVALID, reason=f"URL 缺少主机名: {url}",
                           hint="检查 adapter.url 是否完整")
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        return ProbeResult(PROBE_INVALID, reason=f"主机解析失败: {host} ({e})",
                           hint="确认端点主机名可解析且可达")
    if not infos:
        return ProbeResult(PROBE_INVALID, reason=f"主机解析失败: {host}",
                           hint="确认端点主机名可解析且可达")
    if not allow_private:
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            except ValueError:
                continue
            if _addr_is_private(addr):
                return ProbeResult(PROBE_INVALID, reason=f"禁止探测内网/环回地址: {host}",
                                   hint="服务端校验器默认不探测内网/环回地址")
    pinned_ip = infos[0][4][0].split("%", 1)[0]
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query
    return _ProbeTarget(url=url, scheme=scheme, host=host, port=port, netloc=parsed.netloc,
                        pinned_ip=pinned_ip, request_path=request_path)


def _probe_request_once(target: _ProbeTarget, method: str,
                        timeout: float) -> tuple[int | None, str]:
    """单方法探测请求：http.client 直连已钉定 IP，不跟随重定向；返回 (status, error)。"""
    try:
        if target.scheme == "https":
            conn = _PinnedHTTPSConnection(
                target.pinned_ip, target.host, port=target.port, timeout=timeout,
                context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(target.pinned_ip, port=target.port, timeout=timeout)
        try:
            conn.request(method, target.request_path, headers={
                "Host": target.netloc,
                "Connection": "close",
                "User-Agent": "grape-plugin-l0-probe",
            })
            resp = conn.getresponse()
            status = resp.status
            resp.read()
        finally:
            conn.close()
        return status, ""
    except (OSError, http.client.HTTPException, ValueError) as e:
        return None, str(e)


def _probe_verdict(target: _ProbeTarget, statuses: list[tuple[str, int]],
                   last_error: str) -> ProbeResult:
    """按 OPTIONS/HEAD 实测状态合并出探测结论（auth_required > ok > invalid）。"""
    if not statuses:
        return ProbeResult(PROBE_INVALID, reason=f"连接失败: {last_error}",
                           hint="确认端点可达（连接失败/404 视为命令无效）")
    evidence = ", ".join(f"HTTP {s} ({m} {target.url})" for m, s in statuses)
    verdicts = [classify_probe_status(s) for _, s in statuses]
    if PROBE_AUTH_REQUIRED in verdicts:
        return ProbeResult(PROBE_AUTH_REQUIRED, evidence=evidence,
                           reason=f"端点存在但需凭据（{evidence}）",
                           hint="401 分层：视为命令存在，上架后需在凭据库补齐凭据")
    if PROBE_OK in verdicts:
        return ProbeResult(PROBE_OK, evidence=evidence)
    if any(300 <= s < 400 for _, s in statuses):
        return ProbeResult(PROBE_INVALID, evidence=evidence,
                           reason=f"端点返回重定向，探测不跟随（3xx 视为命令无效）: {evidence}",
                           hint="端点应直接可达；重定向目标不做私网复核，请配置直达地址")
    return ProbeResult(PROBE_INVALID, evidence=evidence,
                       reason=f"端点未通过方法名探测（{evidence}）",
                       hint="确认 url 与命令路由已实现（404/4xx/5xx/重定向视为命令无效）")


def probe_http(url: str, timeout: float = 10.0, allow_private: bool = False) -> ProbeResult:
    """http/mcp-remote 方法名探测：OPTIONS·HEAD（端点存在性 + 401 分层）。

    SSRF 防护（三段）：
    ① getaddrinfo 解析后对全部解析结果做私网/环回比对（默认拒绝内网，
       十进制/IPv6 变体均在解析后拦截）；
    ② 解析结果 → 连接一一对应：直接连接已解析 IP（Host 头保留原主机名，
       https 下 TLS SNI/证书校验仍针对原主机名），消除「校验后连接」之间的
       DNS 重绑定 TOCTOU 窗口；
    ③ http.client 直连，不跟随 3xx 重定向（重定向目标不再隐式可达）。
    """
    resolved = _resolve_probe_target(url, allow_private)
    if isinstance(resolved, ProbeResult):
        return resolved
    statuses: list[tuple[str, int]] = []
    last_error = ""
    for method in ("OPTIONS", "HEAD"):
        status, err = _probe_request_once(resolved, method, timeout)
        if status is None:
            last_error = err
        else:
            statuses.append((method, status))
    return _probe_verdict(resolved, statuses, last_error)


def _make_default_prober(allow_private: bool = False, timeout: float = 10.0):
    """默认探测器：按 transport 方法名探测（cli --help / mcp tools list / http OPTIONS·HEAD）。

    同一 URL 结果在单次 check_commands 内缓存（多条命令共享端点不重复探测）。
    """
    url_cache: dict[str, ProbeResult] = {}

    def _prober(command: dict, adapter: dict) -> ProbeResult:
        transport = adapter.get("transport")
        if transport == "cli":
            return probe_cli(adapter.get("command", ""), adapter.get("args"), timeout=timeout)
        if transport == "mcp-stdio":
            return probe_mcp_stdio(adapter.get("command", ""), adapter.get("args"), timeout=timeout)
        url = adapter.get("url")
        if transport in ("http", "mcp-remote", "script") and url:
            # script（Issue #1311）：签名脚本产物交付端点，免凭据探测与 http 同口径
            if url not in url_cache:
                url_cache[url] = probe_http(url, timeout=timeout, allow_private=allow_private)
            return url_cache[url]
        return ProbeResult(PROBE_INVALID, reason=f"transport={transport} 缺少 command/url，无法探测",
                           hint="按 transport 补充 command（cli/mcp-stdio）或 url（mcp-remote/http）")

    return _prober


def check_commands(manifest: dict, prober=None, allow_private: bool = False,
                   timeout: float = 10.0) -> list[ValidationError]:
    """③ 命令有效性免凭据探测：对每个 command × adapter 组合做方法名探测。

    prober 可注入（上架前自检/测试用 stub）；缺省用默认探测器。
    安全门禁：cli/mcp-stdio 的 command 会被探测器以子进程真实执行，
    permissions.exec 白名单未声明或未覆盖该 command 时拒绝探测（不调用探测器）。
    401 分层：401/403 → 命令存在仅缺凭据 → 视为通过（warning 提示补凭据）；
    连接失败/404 → 命令无效 → 拒绝（error）。
    """
    if prober is None:
        prober = _make_default_prober(allow_private=allow_private, timeout=timeout)
    adapters = {a.get("id"): a for a in (manifest.get("adapters") or [])}
    exec_list = (manifest.get("permissions") or {}).get("exec") or []
    out: list[ValidationError] = []
    for i, cmd in enumerate(manifest.get("commands") or []):
        for j, ref in enumerate(cmd.get("adapters") or []):
            ad = adapters.get(ref)
            if not ad:
                continue  # 引用完整性已由 schema 校验报告
            path = f"commands[{i}].adapters[{j}]"
            transport = ad.get("transport")
            command = ad.get("command")
            if transport in ("cli", "mcp-stdio") and (not exec_list or command not in exec_list):
                out.append(ValidationError(
                    CHECK_PROBE, "probe.exec_whitelist", path,
                    reason=("命令探测已拒绝执行：permissions.exec 白名单未声明"
                            if not exec_list else
                            f"命令探测已拒绝执行：command 未落入 permissions.exec 白名单: {command}"),
                    hint="免凭据探测会以子进程真实执行 command；仅在 permissions.exec 声明白名单后探测",
                ))
                continue
            result = prober(cmd, ad)
            if result.status == PROBE_OK:
                continue
            if result.status == PROBE_AUTH_REQUIRED:
                cred = (ad.get("auth") or {}).get("credential", "对应凭据")
                out.append(ValidationError(
                    CHECK_PROBE, "probe.auth_required", path,
                    reason=result.reason or f"命令存在但需凭据（{result.evidence}）",
                    hint=result.hint or f"401 分层：视为命令存在，上架后需配置凭据 {cred}",
                    severity=SEVERITY_WARNING,
                ))
            else:
                out.append(ValidationError(
                    CHECK_PROBE, "probe.invalid", path,
                    reason=result.reason or f"命令探测失败（{result.evidence}）",
                    hint=result.hint or "连接失败/404 视为命令无效：确认命令已实现且免凭据可达",
                ))
    return out


# ============================== L0 validate 入口（发布门禁） ==============================

def _split_errors_warnings(
        findings: list[ValidationError]) -> tuple[list[ValidationError], list[ValidationError]]:
    """findings 按 severity 分流为 (errors, warnings)，validate 与 validate_manifest 共用。"""
    errors = [f for f in findings if f.severity == SEVERITY_ERROR]
    warnings = [f for f in findings if f.severity != SEVERITY_ERROR]
    return errors, warnings


def _report_dict(errors: list[ValidationError],
                 warnings: list[ValidationError]) -> dict:
    """结构化报告 dict（valid + as_dict 清单），ValidationReport.to_dict 与 validate_manifest 共用。"""
    return {
        "valid": not errors,
        "errors": [e.as_dict() for e in errors],
        "warnings": [w.as_dict() for w in warnings],
    }


@dataclass
class ValidationReport:
    """L0 校验报告：errors（error 级，拒绝）与 warnings（提示级）分开。"""

    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    manifest: dict | None = None

    def to_dict(self) -> dict:
        return _report_dict(self.errors, self.warnings)


def _load_manifest(plugin_dir: str) -> tuple[dict | None, ValidationError | None]:
    path = os.path.join(plugin_dir, "plugin.json")
    if not os.path.isfile(path):
        return None, ValidationError(
            CHECK_SCHEMA, "schema.missing_manifest", "(root)",
            reason=f"插件包缺少 plugin.json（查找路径: {path}）",
            hint="在插件包根目录放置 plugin.json（清单标准见 contracts/plugin.schema.json）",
        )
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, ValidationError(
            CHECK_SCHEMA, "schema.malformed_manifest", "(root)",
            reason=f"plugin.json 不是合法 JSON: {e}",
            hint="修正 JSON 语法后重试",
        )


def _make_skip_prober():
    """跳过探测的空探测器（probe=False 时使用，仅做静态校验）。"""

    def _prober(command: dict, adapter: dict) -> ProbeResult:
        return ProbeResult(PROBE_OK, evidence="探测未启用（仅静态校验）")

    return _prober


def validate(plugin_dir: str, prober=None, allow_private: bool = False,
             timeout: float = 10.0) -> ValidationReport:
    """L0 validate 入口：合并 ① schema ② 禁二进制 ③ 命令探测 三个子检查。

    plugin_dir 为插件包目录（根含 plugin.json）；prober 可注入（测试/自检 stub），
    None 时使用默认探测器（方法名探测 + 401 分层）。
    """
    manifest, err = _load_manifest(plugin_dir)
    if err is not None:
        return ValidationReport(valid=False, errors=[err])
    findings = check_schema(manifest)
    findings += check_binaries(plugin_dir, manifest)
    findings += check_commands(manifest, prober=prober, allow_private=allow_private, timeout=timeout)
    errors, warnings = _split_errors_warnings(findings)
    return ValidationReport(valid=not errors, errors=errors, warnings=warnings, manifest=manifest)


def validate_for_publish(plugin_dir: str, probe: bool = True,
                         allow_private: bool = False, timeout: float = 10.0) -> dict:
    """发布门禁（上传/上架前强校验）：返回结构化报告 dict。

    probe=False 时仅静态校验（schema + 禁二进制），适合上传阶段快速拒绝；
    上架/发布前建议 probe=True（方法名探测 + 401 分层，免凭据自检）。
    """
    prober = None if probe else _make_skip_prober()
    report = validate(plugin_dir, prober=prober, allow_private=allow_private, timeout=timeout)
    return report.to_dict()


def validate_manifest(manifest: dict, probe: bool = False,
                      allow_private: bool = False, timeout: float = 10.0) -> dict:
    """manifest 级门禁公开入口（上架前 manifest 自检/HTTP /validate 背后实现）。

    schema 结构校验；probe=true 时附带免凭据命令探测（exec 白名单强制，
    探测器会以子进程真实执行 command，见 check_commands）。
    返回 {"valid", "errors", "warnings"}，错误为 ValidationError.as_dict 结构。
    """
    findings = check_schema(manifest)
    if probe:
        findings += check_commands(manifest, allow_private=allow_private, timeout=timeout)
    errors, warnings = _split_errors_warnings(findings)
    return _report_dict(errors, warnings)
