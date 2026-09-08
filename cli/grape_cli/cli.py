"""grape CLI 入口：契约驱动命令 + 内置（git/api/refresh/commands/wait）。"""
import json
import sys
import time

import click

from grape_cli import __version__, config
from grape_cli.client import ApiClient, CliError
from grape_cli.commands.api import api_command
from grape_cli.commands.git import git_command
from grape_cli.contracts import load_contract
from grape_cli.registry import build_groups, command_tree


def wait_command() -> click.Command:
    """grape tasks wait：轮询任务状态直至终态（复用 GET /api/tasks/{id}/status，契约驱动）。"""
    @click.command(name="wait",
                   help="Wait until a task reaches a terminal state (completed / failed / cancelled)")
    @click.option("--task-id", required=True, help="task id")
    @click.option("--timeout", default=120, help="max wait seconds (default 120)")
    @click.option("--interval", default=3, help="poll interval seconds (default 3)")
    @click.option("--json-out", is_flag=True, help="machine-readable JSON output")
    def _wait(task_id: str, timeout: int, interval: int, json_out: bool):
        api = ApiClient()
        _terminal = {"completed", "failed", "cancelled"}
        deadline = time.time() + max(0, timeout)
        data = {}
        while True:
            _s, data = api.request("GET", f"/api/tasks/{task_id}/status")
            st = data.get("status", "")
            if st in _terminal or time.time() >= deadline:
                break
            time.sleep(max(0, interval))
        out = json.dumps(data, ensure_ascii=False, indent=None if json_out else 2)
        click.echo(out)
        if data.get("status") not in _terminal:
            raise CliError(f"等待超时（>{timeout}s），当前状态 {st or '未知'}")

    return _wait


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
            # 注入静态 wait 子命令（非契约）：grape tasks wait --task-id x
            _tasks = self._groups.get("tasks")
            if _tasks is not None:
                try:
                    _tasks.add_command(wait_command(), name="wait")
                except Exception:
                    pass
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
@click.option("--base-url", default=None, help="server base URL (one-shot, highest priority)")
@click.option("--token", "--grape-api-token", "--grape_api_token", default=None,
              help="token (one-shot, highest priority; tk_ session / grape_ user key)")
@click.option("--timeout", default=None, type=int,
              help="request timeout seconds (default 30; not applied to git)")
@click.pass_context
def cli(ctx, base_url: str | None, token: str | None, timeout: int | None):
    """Grape CLI: contract-driven commands (new API endpoint -> new command automatically)
    plus local git passthrough (platform token stays in-process).

    \b
    One-shot usage:
      grape --token tk_xxx tasks mine --status active
      grape --base-url https://grape.example.com --grape-api-token grape_xxx skills list --json-out
      grape --timeout 120 tasks wait --task-id 56
    """
    from grape_cli.config import set_cli_params
    set_cli_params(base_url=base_url or "", token=token or "", timeout=timeout or 0)
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
    """Force re-fetch the latest OpenAPI contract and cache it."""
    schema = load_contract(refresh=True)
    cli._groups = build_groups(schema)
    cli._loaded = True
    cli.contract_version = str(schema.get("info", {}).get("version", ""))
    click.echo(f"契约已刷新：v{schema.get('info', {}).get('version', '?')}（{len(schema.get('paths', {}))} 端点），组名见 `grape --help`")


@cli.command(name="commands")
@click.argument("pattern", default="")
def commands_cmd(pattern: str):
    """List all contract commands (pipeable filter)."""
    lines = _tree_lines(pattern)
    for line in lines:
        click.echo(line)
    if not lines:
        click.echo("未匹配到契约命令（grape refresh 或检查 base_url/token）")


api_cmd = api_command()
cli.add_command(api_cmd)

git_cmd = git_command()
cli.add_command(git_cmd)


def main(argv=None, *args, **kw):
    """入口：返回退出码（console script 中 sys.exit(main()) 消费）。

    - 0                      成功
    - 1                      通用/配置错误（缺 token、网络、超时等）
    - 2                      click 用法错误
    - =HTTP(≥400)            CliError 透传（401 等）
    - 130                    Ctrl+C 用户中断
    """
    # Windows GBK 控制台兜底：统一 UTF-8 输出，非 ASCII（如 ✅）逐字降级而非抛异常
    try:
        for _s in (sys.stdout, sys.stderr):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _argv = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        rv = cli(_argv, standalone_mode=False, *args, **kw)
        return 0 if rv is None else int(rv)
    except click.exceptions.Abort:
        # Ctrl+C（click 把 KeyboardInterrupt 转 Abort）→ 规范化 130
        click.echo("Error: interrupted", err=True)
        return 130
    except KeyboardInterrupt:
        click.echo("Error: interrupted", err=True)
        return 130
    except CliError as e:
        click.echo(f"Error: {e}", err=True)
        return e.code or 1
    except click.exceptions.ClickException as e:
        e.show()
        return int(getattr(e, "exit_code", 1)) or 1


if __name__ == "__main__":
    sys.exit(main(prog_name="grape"))