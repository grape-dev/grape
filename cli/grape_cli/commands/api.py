"""grape api <method> <path> [-d json]：契约未覆盖/特殊场景的直连兜底。"""
import json

import click

from grape_cli.client import ApiClient


def api_command() -> click.Command:
    @click.command(name="api", help="通用兜底：直连任意 API（方法/路径来自契约白名单）")
    @click.argument("method")
    @click.argument("path")
    @click.option("-d", "--data", default="", help="JSON 请求体")
    @click.option("--json-out", is_flag=True, help="机器可读 JSON 输出")
    def api_cmd(method: str, path: str, data: str, json_out: bool):
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise click.ClickException(f"非法方法: {method}")
        api = ApiClient()
        body = json.loads(data) if data else None
        _status, resp = api.request(method, path, json_body=body)
        click.echo(json.dumps(resp, ensure_ascii=False, indent=None if json_out else 2))

    return api_cmd