"""grape api <method> <path> [-d json]：契约未覆盖/特殊场景的直连兜底。"""
import json

import click

from grape_cli.client import ApiClient


def api_command() -> click.Command:
    @click.command(name="api", help="Generic fallback: call any API (method/path from contract whitelist)")
    @click.argument("method")
    @click.argument("path")
    @click.option("-d", "--data", default="", help="JSON request body")
    @click.option("--json-out", is_flag=True, help="machine-readable JSON output")
    def api_cmd(method: str, path: str, data: str, json_out: bool):
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise click.ClickException(f"非法方法: {method}")
        api = ApiClient()
        body = json.loads(data) if data else None
        _status, resp = api.request(method, path, json_body=body)
        click.echo(json.dumps(resp, ensure_ascii=False, indent=None if json_out else 2))

    return api_cmd