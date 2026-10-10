"""契约 → click 命令树（Issue-45 M4 方案 B：operationId 驱动命令生成）。

- operationId 首段 → 组名（`gitrepo_*` → `git-repo`），其余段 → 嵌套动词链。
  例：`tasks_mine` → `grape tasks mine`；`gitrepo_issues_list` → `grape git-repo issues list`。
- query/path 参数 → `--<name>`；requestBody → `--json '<body>'`；`--json-out` 机器可读输出。
- security 进契约 → 自动携带认证头（ApiClient 统一）。
- **新增 OpenAPI 接口 → 重建契约 → CLI 自动获得命令，零 CLI 源码改动**（R7）。
- 帮助语义：优先 `src/data/openapi.en.json` 的 operationId → 英文含义，回退契约 summary。
"""
import base64
import difflib
import json
import os
import re
from importlib.resources import files

import click

from src.client import ApiClient, CliError

GROUP_ALIAS = {"gitrepo": "git-repo"}

# 组的固定引导行（首行=短帮助；帮助 agent 区分 demo 组语义）
_GROUP_INTRO = {
    "git-repo": (
        "Platform API by grape (gitcode / github / gitlab): branches, issues, "
        "PRs, comments, merge & review, checks. No extra token setup - "
        "use the grape session token."
    ),
}

_EN_SUMMARIES: dict = {}


def _en_summary(op: dict) -> str:
    """命令摘要：直接用服务端契约 summary（服务端已统一英文，单一事实源）。"""
    return (op.get("summary") or "").strip()


def group_name(g: str) -> str:
    return GROUP_ALIAS.get(g, g)


def parse_operations(schema: dict) -> list[dict]:
    ops = []
    schemas = schema.get("components", {}).get("schemas", {})
    for path, methods in (schema.get("paths") or {}).items():
        for method, op in methods.items():
            if not isinstance(op, dict) or not op.get("operationId"):
                continue
            oid = op["operationId"]
            # 过滤服务端 autogen 丑名（路径内嵌 "api_"），避免命令树混入 create/get 等噪音组
            if "api_" in oid:
                continue
            parts = oid.split("_")
            verb = parts[1:] or ["list"]
            ops.append({
                "operationId": oid,
                "method": method.upper(),
                "path": path,
                "group": group_name(parts[0]),
                "verb": verb,
                "op": op,
                "schemas": schemas,
            })
    return ops


def _params_by_in(op: dict, where: str) -> list[str]:
    return [p["name"] for p in (op.get("parameters") or []) if p.get("in") == where]


def _body_schema(meta: dict) -> dict:
    """解引用 requestBody schema 到 components（兼容 application/json 与 multipart）。"""
    op = meta["op"]
    content = (op.get("requestBody") or {}).get("content") or {}
    ct = content.get("application/json") or content.get("multipart/form-data") or {}
    sch = ct.get("schema") or {}
    name = (sch.get("$ref") or "").rsplit("/", 1)[-1]
    return meta.get("schemas", {}).get(name, {}) if name else (sch or {})


def _body_fields(meta: dict) -> list[str]:
    """解析 requestBody schema 的字段清单（$ref 解引用到 components）。

    兼容 application/json 与 multipart/form-data（上传类）。
    """
    return sorted(_body_schema(meta).get("properties") or {})


def _body_field_meta(meta: dict) -> list[dict]:
    """字段元信息 [{name, required, type}]：required 读 schema.required（契约权威）。"""
    schema = _body_schema(meta)
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    return [
        {
            "name": k,
            "required": k in required,
            "type": (v.get("type") if isinstance(v, dict) else "") or "string",
        }
        for k, v in sorted(props.items())
    ]


def _body_fields_help(meta: dict) -> str:
    """Body fields 帮助行：`title* (required string), head* (required string), ...`（英文）。"""
    out = ", ".join(
        f"{f['name']}* (required {f['type']})" if f["required"] else f"{f['name']} ({f['type']})"
        for f in _body_field_meta(meta)
    )
    return out or "(free-form JSON)"


_EXAMPLES: dict = {}


def _load_examples() -> dict:
    global _EXAMPLES
    if not _EXAMPLES:
        try:
            _EXAMPLES = json.loads(
                files("src").joinpath("data", "examples.json").read_text(encoding="utf-8")
            )
        except Exception:
            _EXAMPLES = {}
    return _EXAMPLES


def _example_args(meta: dict, path_params: list[str], query_params: list[str],
                  body_fields: list[str]) -> list[str]:
    """命令示例参数列表：优先 examples.json（str 或 list），否则自动生成占位示例。"""
    raw = _load_examples().get(meta["operationId"])
    if raw:
        return raw if isinstance(raw, list) else [raw]
    parts = [f"--{n} <{n}>" for n in path_params]
    if body_fields:
        parts.append(f"--json '{{\"{body_fields[0]}\":\"...\"}}'")
    else:
        parts += [f"--{n} <{n}>" for n in query_params[:2]]
    return [" ".join(parts)]


# ---- token 作用域（docs/grape-token-scope-help.md）----
_TOKEN_SCOPE: dict | None = None


def _load_token_scope() -> dict:
    """懒加载 token-scope.json：{user_only: [...], session_only: [...]}。"""
    global _TOKEN_SCOPE
    if _TOKEN_SCOPE is None:
        try:
            _TOKEN_SCOPE = json.loads(
                files("src").joinpath("data", "token-scope.json").read_text(encoding="utf-8")
            )
        except Exception:
            _TOKEN_SCOPE = {}
    return _TOKEN_SCOPE


def _token_scope(meta: dict) -> str:
    """operationId/op 的作用域：dual / user_only(D, session→403) / session_only(C)。

    优先读契约 `op.x-token-scope`（服务端权威），缺省回落静态表与 `gitrepo_*` 前缀。
    """
    _op = meta.get("op") or {}
    xs = _op.get("x-token-scope")
    if xs in ("dual", "user_only", "session_only"):
        return xs
    oid = meta.get("operationId") or ""
    m = _load_token_scope()
    if oid in m.get("user_only", []):
        return "user_only"
    if oid in m.get("session_only", []) or oid.startswith("gitrepo_"):
        return "session_only"
    return "dual"


def cli_mode() -> str:
    """GRAPE_CLI_MODE：WORKER / USER / ALL（缺省/非法 → ALL，向后兼容）。"""
    mode = (os.environ.get("GRAPE_CLI_MODE") or "ALL").strip().upper()
    return mode if mode in ("WORKER", "USER", "ALL") else "ALL"


def allowed_by_mode(mode: str, scope: str) -> bool:
    """scope 在当前模式下是否允许（用于过滤帮助/commands 与守卫）。"""
    if mode == "ALL" or scope == "dual":
        return True
    if mode == "WORKER":
        return scope in ("dual", "session_only")
    if mode == "USER":
        return scope in ("dual", "user_only")
    return True


def _cmd_allowed(cmd) -> bool:
    """命令/组在当前模式下是否至少有一个允许的叶子命令。"""
    mode = cli_mode()
    if isinstance(cmd, click.Group):
        return any(_cmd_allowed(c) for c in cmd.commands.values())
    scope = getattr(cmd, "token_scope", None) or "dual"
    return allowed_by_mode(mode, scope)


def _read_json_arg(raw) -> str:
    """--json 入参读取：`@file.json` 读文件、`@-` 读 stdin，其余原样返回。"""
    if isinstance(raw, str) and raw.startswith("@"):
        path = raw[1:]
        try:
            if path == "-":
                import sys
                return sys.stdin.read()
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise CliError(f"cannot read --json file '{path}': {e}")
    return raw


# 已知「能力位缺失」白名单：字段契约不支持时给出人读指引，避免 agent 原地试错（见 docs/grape-cli-help-and-hints-optimization.md §3.3）。
# 键：`group verb...`（空格分隔），值：{不支持的字段: 英文行动指引}。
_UNSUPPORTED_FIELD_HINTS: dict[str, dict[str, str]] = {
    "git-repo pulls update": {
        "state": "'state' is not supported here; closing/deprecating a PR is not available via this command",
    },
}


def _validate_body(meta: dict, body, body_fields: list[str]) -> None:
    """请求体守卫（通用）：可枚举 schema 时，--json 内键必须都在允许集内。

    不支持的字段及时报错并列出允许字段，避免服务端 200 静默空转；服务端后续补字段后自动放行。
    追加：缺失必填字段（schema.required）报错 + 已知能力位缺失给行动指引。
    """
    if not body_fields or not isinstance(body, dict):
        return
    cmd_path = f"grape {meta['group']} {' '.join(meta['verb'])}"
    allowed = set(body_fields)
    unknown = sorted(set(body) - allowed)
    # 能力位缺失 → 优先给行动指引（白名单），替代冷冰冰 unknown field
    hint_key = f"{meta['group']} {' '.join(meta['verb'])}"
    for f in unknown:
        hint = _UNSUPPORTED_FIELD_HINTS.get(hint_key, {}).get(f)
        if hint:
            raise CliError(hint)
    if unknown:
        raise CliError(
            "unknown request field(s): " + ", ".join(unknown) + "\n"
            f"allowed for {meta['method']} {meta['path']}: " + ", ".join(body_fields) + "\n"
            f"see `{cmd_path} --help` for the JSON request schema"
        )
    # 缺失必填字段（schema.required 契约权威）
    required_meta = [f for f in _body_field_meta(meta) if f["required"]]
    missing = sorted(f["name"] for f in required_meta if f["name"] not in body)
    if missing:
        raise CliError(
            "missing required request field(s): " + ", ".join(missing) + "\n"
            f"required for {meta['method']} {meta['path']}: " + ", ".join(f["name"] for f in required_meta) + "\n"
            f"see `{cmd_path} --help` for the JSON request schema"
        )


def build_command(meta: dict) -> click.Command:
    op, method, path = meta["op"], meta["method"], meta["path"]
    path_params = _params_by_in(op, "path")
    query_params = _params_by_in(op, "query")
    has_body = "requestBody" in op
    # multipart/form-data 上传类（如 skills upload）：--file + 表单字段，不走 --json
    _content = ((op.get("requestBody") or {}).get("content") or {})
    is_multipart = bool(_content.get("multipart/form-data"))
    multipart_fields = []
    if is_multipart:
        _sch = _content["multipart/form-data"].get("schema") or {}
        _ref = (_sch.get("$ref") or "").rsplit("/", 1)[-1]
        _props = (meta.get("schemas", {}).get(_ref, {}) or {}).get("properties", {}) if _ref else (_sch.get("properties") or {})
        multipart_fields = [k for k in sorted(_props) if k != "file"]

    options = [click.Option([f"--{n}"], default=None, help=f"parameter {n}") for n in path_params]
    options += [click.Option([f"--{n}"], default=None, help=f"parameter {n}") for n in query_params]
    if is_multipart:
        options.append(click.Option(["--file"], default=None, help="upload file path (multipart)"))
        options += [click.Option([f"--{f}"], default="", help=f"form field {f}") for f in multipart_fields]
    elif has_body:
        options.append(click.Option(["--json"], default=None,
                                     help="JSON string request body (or @file.json)"))
    options.append(click.Option(["--json-out"], is_flag=True, help="machine-readable JSON output"))
    if meta["operationId"] == "gitrepo_contents":
        options.append(click.Option(
            ["--raw"], is_flag=True,
            help="base64-decode single-file content to raw text (dir listing falls back to JSON)"))

    def _run(**kw):
        api = ApiClient()
        url = path
        for name in path_params:
            v = kw.get(name)
            if v is None:
                if _OPTIONAL_EMPTY_PATH.get(meta["operationId"]) == name:
                    v = ""
                else:
                    raise CliError(f"Missing required parameter --{name}")
            url = url.replace("{" + name + "}", str(v))
        # contents 缺省空 path → 规整到根路由（/contents/ 去尾斜杠 → /contents）
        if meta["operationId"] == "gitrepo_contents" and url.endswith("/contents/"):
            url = url[:-1]
        params = {n: kw.get(n) for n in query_params if kw.get(n) is not None}
        if not allowed_by_mode(cli_mode(), _token_scope(meta)):
            _scope = _token_scope(meta)
            _mode = cli_mode()
            _hint = ("set GRAPE_CLI_MODE=USER or ALL (or provide GRAPE_API_TOKEN)"
                     if _scope == "user_only" else
                     "set GRAPE_CLI_MODE=WORKER or ALL (or provide GRAPE_SESSION_TOKEN)")
            raise CliError(
                f"command requires token scope '{_scope}', not available in "
                f"GRAPE_CLI_MODE={_mode}; {_hint}"
            )
        body = None
        files = None
        form_data = None
        if is_multipart:
            _f = kw.get("file")
            if not _f:
                raise CliError("Missing required --file")
            try:
                with open(_f, "rb") as fh:
                    fb = fh.read()
            except OSError as e:
                raise CliError(f"cannot read --file '{_f}': {e}")
            fname = _f.replace("\\", "/").rsplit("/", 1)[-1]
            files = {"file": (fname, fb, "application/zip")}
            form_data = {f: kw.get(f) for f in multipart_fields if kw.get(f)}
        else:
            raw = kw.get("json")
            if has_body and raw:
                try:
                    body = json.loads(_read_json_arg(raw))
                except CliError:
                    raise
                except Exception:
                    raise CliError("--json is not a valid JSON string")
                if body_fields and not isinstance(body, dict):
                    raise CliError(
                        f"--json must be a JSON object for this endpoint "
                        f"(allowed fields: {', '.join(body_fields)})"
                    )
                _validate_body(meta, body, body_fields)
        try:
            _status, data = api.request(method, url, params=params, json_body=body,
                                        files=files, data=form_data)
        except CliError as e:
            # D 类（user_only）：会话 token 403 时追加英文行动指引，帮 agent 立即换钥
            if e.code == 403 and _token_scope(meta) == "user_only":
                raise CliError(
                    f"{e}; use GRAPE_API_TOKEN / Web JWT — "
                    "session token (tk_) is rejected for this scope",
                    code=403,
                )
            raise
        if meta["operationId"] == "gitrepo_contents" and kw.get("raw") and isinstance(data, dict):
            _c = data.get("content")
            if _c:
                try:
                    click.echo(base64.b64decode(_c).decode("utf-8", "replace"))
                    return
                except Exception:
                    pass
        click.echo(json.dumps(data, ensure_ascii=False,
                              indent=None if kw.get("json_out") else 2))

    summary = _en_summary(op)
    desc = (op.get("description") or "").strip()
    # 描述仅保留 ASCII（契约描述多为中文，帮助统一英文）
    if desc and not desc.isascii():
        desc = ""
    has_body_flag = "yes" if has_body else "no"
    method_path = f"Invoke grape API: {method} {path}"
    scope = _token_scope(meta)
    scope_suffix = {"user_only": " (session 403 → grape_/JWT)",
                    "session_only": " (session-only)"}.get(scope, "")
    # 首行=实际含义 + 方法与是否需要 body + token 作用域（短帮助/命令列表直接展示）
    first = f"{summary or f'{method} {path}'} ({method}, body: {has_body_flag}){scope_suffix}"
    help_lines = [first, "", method_path]
    if scope == "user_only":
        help_lines.append("Token scope: session (tk_) → 403; use GRAPE_API_TOKEN / Web JWT")
    elif scope == "session_only":
        help_lines.append("Token scope: session (tk_) only; API key / JWT → 403")
    body_fields = _body_fields(meta)
    if body_fields:
        help_lines.append(f"Body fields: {_body_fields_help(meta)}")
        help_lines.append("Long JSON body hint: use --json @file.json (multi-line, no escaping)")
    elif has_body:
        help_lines.append("Body fields: (free-form JSON)")
        help_lines.append("Long JSON body hint: use --json @file.json (multi-line, no escaping)")
    example_lines = _example_args(meta, path_params, query_params, body_fields)
    if example_lines:
        help_lines += ["", "Examples:"]
        command_path = f"grape {meta['group']} {' '.join(meta['verb'])}"
        for e in example_lines:
            help_lines.append(f"  {command_path} {e}")
    if desc and desc != summary:
        help_lines += ["", desc]
    help_text = "\n".join(help_lines)
    cmd = click.Command(name="", callback=_run, params=options, help=help_text)
    cmd.token_scope = _token_scope(meta)
    return cmd


def build_groups(schema: dict) -> dict[str, click.Group]:
    """返回 {组名: click.Group}，嵌套动词链按段建子组。"""
    holder: dict = {}
    for meta in parse_operations(schema):
        _insert(holder, meta)

    groups = {}
    for gname, subtree in sorted(holder.items()):
        groups[gname] = _materialize(subtree, gname)
    return groups


def _insert(holder: dict, meta: dict):
    node = holder.setdefault(meta["group"], {})
    for seg in meta["verb"][:-1]:
        node = node.setdefault(seg, {})
    node[meta["verb"][-1]] = meta  # 叶子：命令


def _is_meta(v) -> bool:
    return isinstance(v, dict) and "operationId" in v


# 常见 git 概念 → 契约动词 的别名建议（agent 猜名时的兜底）
_ALIAS_SUGGESTIONS = {
    "files": ["tree", "contents"],
    "file": ["contents"],
    "branch": ["branches"],
    "branches": ["branches"],
    "commit": ["commits"],
    "commits": ["commits"],
    "log": ["commits"],
    "repo": ["repo"],
}

# 可省略的 path 参数：缺省按服务端空 path 语义（如 contents 缺省 → 根目录列表）
_OPTIONAL_EMPTY_PATH = {
    "gitrepo_contents": "path",
}


class _ContractGroup(click.Group):
    """契约组：agent 友好。

    - 未知子命令 → difflib/别名给出 `Did you mean: ...`；
    - 组被带未知选项调用（无首个子命令令牌）→ 打印组帮助而非报错。
    """

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as e:
            msg = e.message or ""
            if msg.startswith("No such command"):
                m = re.match(r"No such command ['\"]([^'\"]+)['\"]", msg)
                if m:
                    name = m.group(1)
                    cands = list(set(self.commands) | set(self.list_commands(ctx)))
                    sugg = difflib.get_close_matches(name, cands, n=3, cutoff=0.55)
                    sugg += [s for s in _ALIAS_SUGGESTIONS.get(name, []) if s not in sugg and s in cands]
                    if sugg:
                        raise click.UsageError(
                            f"No such command '{name}'. Did you mean: " + ", ".join(sugg) + "?",
                            ctx,
                        )
            raise

    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as e:
            msg = e.message or ""
            if msg.startswith(("No such option", "Got unexpected extra argument")):
                click.echo(ctx.get_help())
                ctx.exit(0)
            raise

    def format_commands(self, ctx, formatter):
        """按 GRAPE_CLI_MODE 过滤：只列出当前模式允许的动词（子组递归判定）。"""
        rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            if not _cmd_allowed(cmd):
                continue
            rows.append((name, cmd))
        if not rows:
            return
        max_name = max(len(n) for n, _ in rows)
        limit = formatter.width - 10 - max_name
        with formatter.section("Commands"):
            formatter.write_dl([(n, c.get_short_help_str(limit)) for n, c in rows])


def _subtree_allowed(subtree) -> bool:
    """子树叶子中是否有任何一个在当前 GRAPE_CLI_MODE 下允许（用于组摘要过滤）。"""
    if _is_meta(subtree):
        return allowed_by_mode(cli_mode(), _token_scope(subtree))
    return any(_subtree_allowed(v) for v in subtree.values())


def _materialize(subtree: dict, name: str) -> click.Group:
    """按子命令摘要聚合出“实际含义”，作为组帮助首行（方便 agent 快速理解）。

    摘要按 GRAPE_CLI_MODE 过滤：只列当前模式允许的动词，避免 user_only 命令在
    WORKER 帮助下泄漏（commands 列表已过滤，首行摘要需同步）。
    """
    meaning = []
    for key, val in sorted(subtree.items()):
        if not _subtree_allowed(val):
            continue
        if _is_meta(val):
            s = _en_summary(val["op"])
            meaning.append(f"{key}: {s}" if s else key)
        else:
            meaning.append(key)
    lines = []
    intro = _GROUP_INTRO.get(name)
    if intro:
        lines += [intro, ""]
    lines += [
        ", ".join(meaning),
        "",
        "Invoke a subcommand (verb) of this group: "
        f"`grape <group> {name} <verb>`; bare invocation shows this help.",
    ]
    grp = _ContractGroup(
        name=name,
        help="\n".join(lines),
        context_settings=dict(help_option_names=["-h", "--help"]),
    )
    for key, val in sorted(subtree.items()):
        if _is_meta(val):
            cmd = build_command(val)
            cmd.name = key
            grp.add_command(cmd, name=key)
        else:
            grp.add_command(_materialize(val, key), name=key)
    return grp


def command_tree(schema: dict) -> dict[str, list[str]]:
    """{组: [动词链]} 便于 --list / commands 展示。"""
    tree: dict[str, set] = {}
    for meta in parse_operations(schema):
        tree.setdefault(meta["group"], set()).add(meta["operationId"].split("_", 1)[1] or "list")
    return {g: sorted(v) for g, v in sorted(tree.items())}