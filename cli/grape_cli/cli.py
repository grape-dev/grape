"""grape CLI 入口：契约驱动命令 + 内置（git/api/refresh/commands/list）。"""
import sys

import click

from grape_cli import __version__, config
from grape_cli.commands.api import api_command
from grape_cli.commands.git import git_command
from grape_cli.contracts import load_contract
from grape_cli.registry import build_groups, command_tree


class ContractGroup(click.Group):
    """加载 /openapi.json 动态生成命令组（方案 B）：新增契约 → 自动获得命令。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._groups: dict = {}
        self._loaded = False
        self._load_error = ""
        self.contract_version = ""

    def _ensure(self, refresh: bool = False):
        if self._loaded and not refresh:
            return
        try:
            schema = load_contract(refresh=refresh)
            self.contract_version = str(schema.get("info", {}).get("version", ""))
            self._groups = build_groups(schema)
            self._loaded = True
        except Exception as e:
            self._load_error = str(e)
            self._groups = {}
            self._loaded = True

    def get_command(self, ctx, cmd_name):
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        if not cmd_name.startswith(("-", "_")):
            self._ensure()
        return self._groups.get(cmd_name)

    def list_commands(self, ctx):
        cmds = list(super().list_commands(ctx))
        try:
            self._ensure()
            cmds += list(self._groups.keys())
        except Exception:
            pass
        return sorted(set(cmds))


@click.group(cls=ContractGroup, invoke_without_command=True)
@click.version_option(__version__, "--version", prog_name="grape",
                      message="%(prog)s %(version)s")
@click.pass_context
def cli(ctx):
    """Agentic DevOps CLI：契约驱动命令（新增接口零 CLI 改动）+ git 本地传输。"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _tree_lines(pattern: str = "") -> list[str]:
    try:
        schema = load_contract()
    except Exception as e:
        return [f"契约加载失败：{e}（检查 base_url/token 或 grape refresh）"]
    lines = []
    for group, verbs in command_tree(schema).items():
        for v in verbs:
            name = f"{group} {v}"
            if pattern and pattern not in name:
                continue
            lines.append(name)
    return sorted(lines)


@cli.command("refresh")
def refresh_cmd():
    """强制拉取最新 OpenAPI 契约并缓存。"""
    schema = load_contract(refresh=True)
    cli._groups = build_groups(schema)
    cli._loaded = True
    cli.contract_version = str(schema.get("info", {}).get("version", ""))
    click.echo(f"契约已刷新：v{schema.get('info', {}).get('version', '?')}（{len(schema.get('paths', {}))} 端点），组名见 `grape --help`")


@cli.command(name="commands")
@click.argument("pattern", default="")
def commands_cmd(pattern: str):
    """列出全部契约命令（可管道过滤）。"""
    lines = _tree_lines(pattern)
    for line in lines:
        click.echo(line)
    if not lines:
        click.echo("未匹配到契约命令（grape refresh 或检查 base_url/token）")


api_cmd = api_command()
cli.add_command(api_cmd)

git_cmd = git_command()
cli.add_command(git_cmd)


def main(*args, **kw):
    # Windows GBK 控制台兜底：统一 UTF-8 输出，非 ASCII（如 ✅）逐字降级而非抛异常
    try:
        for _s in (sys.stdout, sys.stderr):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cli(*args, **kw)


if __name__ == "__main__":
    main(prog_name="grape")