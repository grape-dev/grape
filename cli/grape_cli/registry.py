"""契约 → click 命令树（Issue-45 M4 方案 B：operationId 驱动命令生成）。

- operationId 首段 → 组名（`gitrepo_*` → `git-repo`），其余段 → 嵌套动词链。
  例：`tasks_mine` → `grape tasks mine`；`gitrepo_issues_list` → `grape git-repo issues list`。
- query/path 参数 → `--<name>`；requestBody → `--json '<body>'`；`--json-out` 机器可读输出。
- security 进契约 → 自动携带认证头（ApiClient 统一）。
- **新增 OpenAPI 接口 → 重建契约 → CLI 自动获得命令，零 CLI 源码改动**（R7）。
"""
import json

import click

from grape_cli.client import ApiClient, CliError

GROUP_ALIAS = {"gitrepo": "git-repo"}


def group_name(g: str) -> str:
    return GROUP_ALIAS.get(g, g)


def parse_operations(schema: dict) -> list[dict]:
    ops = []
    for path, methods in (schema.get("paths") or {}).items():
        for method, op in methods.items():
            if not isinstance(op, dict) or not op.get("operationId"):
                continue
            oid = op["operationId"]
            parts = oid.split("_")
            verb = parts[1:] or ["list"]
            ops.append({
                "operationId": oid,
                "method": method.upper(),
                "path": path,
                "group": group_name(parts[0]),
                "verb": verb,
                "op": op,
            })
    return ops


def _params_by_in(op: dict, where: str) -> list[str]:
    return [p["name"] for p in (op.get("parameters") or []) if p.get("in") == where]


def build_command(meta: dict) -> click.Command:
    op, method, path = meta["op"], meta["method"], meta["path"]
    path_params = _params_by_in(op, "path")
    query_params = _params_by_in(op, "query")
    has_body = "requestBody" in op

    options = [click.Option([f"--{n}"], default=None, help=f"参数 {n}") for n in path_params]
    options += [click.Option([f"--{n}"], default=None, help=f"参数 {n}") for n in query_params]
    if has_body:
        options.append(click.Option(["--json"], default=None, help="JSON 字符串请求体"))
    options.append(click.Option(["--json-out"], is_flag=True, help="机器可读 JSON 输出"))

    def _run(**kw):
        api = ApiClient()
        url = path
        for name in path_params:
            v = kw.get(name)
            if v is None:
                raise CliError(f"缺少必填参数 --{name}")
            url = url.replace("{" + name + "}", str(v))
        params = {n: kw.get(n) for n in query_params if kw.get(n) is not None}
        body = None
        raw = kw.get("json")
        if has_body and raw:
            try:
                body = json.loads(raw)
            except Exception:
                raise CliError("--json 不是合法 JSON 字符串")
        _status, data = api.request(method, url, params=params, json_body=body)
        click.echo(json.dumps(data, ensure_ascii=False,
                              indent=None if kw.get("json_out") else 2))

    summary = (op.get("summary") or "").strip()
    desc = (op.get("description") or "").strip()
    help_text = f"Invoke grape API: {method} {path}"
    if summary:
        help_text += f"\n\nSummary: {summary}"
    if desc and desc != summary:
        help_text += f"\n\n{desc}"
    return click.Command(name="", callback=_run, params=options, help=help_text)


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


def _materialize(subtree: dict, name: str) -> click.Group:
    grp = click.Group(name=name, help=f"Contract-driven commands under group '{name}'.")
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