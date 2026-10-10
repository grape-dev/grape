"""grape CLI 入口：契约驱动命令 + 内置（git/refresh/commands/wait）。"""
import json
import sys
import time

import click

from src import __version__, config
from src.client import ApiClient, CliError
from src.commands.git import git_command
from src.contracts import load_contract
from src.registry import _cmd_allowed, build_groups, command_tree


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
            raise CliError(
                f"wait timeout (> {timeout}s), current status {st or 'unknown'}"
            )

    return _wait


class ContractGroup(click.Group):
    """加载 /openapi.json 动态生成命令组（方案 B）：新增契约 → 自动获得命令。"""

    # ---- 帮助类别分组（按意图 × 读写角色，docs/grape-help-category-redesign.md）----
    CATEGORY_MAP = {
        # Core workflows：agent 驱动的流程（会变更）
        "tasks": "Core workflows",
        "a2a": "Core workflows",
        # Agent Context：执行 agent 只读上下文（改状态属 user 层）
        "issues": "Agent Context",
        "events": "Agent Context",
        "knowledge": "Agent Context",
        # Code & repositories
        "git": "Code & repositories",
        "git-repo": "Code & repositories",
        # Account & platform：用户级平台资源（管理导向，含 skills/scenes）
        "agents": "Account & platform",
        "auth": "Account & platform",
        "chat": "Account & platform",
        "repos": "Account & platform",
        "scenes": "Account & platform",
        "skills": "Account & platform",
        # CLI utilities：元命令
        "commands": "CLI utilities",
        "refresh": "CLI utilities",
    }
    CATEGORY_ORDER = [
        "Core workflows",
        "Code & repositories",
        "Agent Context",
        "Account & platform",
        "CLI utilities",
    ]
    DEFAULT_CATEGORY = "Account & platform"
    CATEGORY_INTRO = {
        "Core workflows": "task actions & agent handoffs",
        "Agent Context": "issue / events / knowledge",
        "Code & repositories": "run git locally / platform repo API",
        "Account & platform": "user-level platform resources",
        "CLI utilities": "manage the contract & commands",
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._groups: dict = {}
        self._loaded = False
        self._load_error = ""
        self.contract_version = ""

    def format_commands(self, ctx, formatter):
        """按类别渲染 Commands，便于 agent/人扫读。"""
        rows_by_cat: dict[str, list] = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            if not _cmd_allowed(cmd):
                continue
            cat = self.CATEGORY_MAP.get(name, self.DEFAULT_CATEGORY)
            rows_by_cat.setdefault(cat, []).append((name, cmd))
        if not rows_by_cat:
            return
        with formatter.section("Commands"):
            for cat in self.CATEGORY_ORDER:
                items = rows_by_cat.get(cat)
                if not items:
                    continue
                max_name = max(len(n) for n, _ in items)
                limit = formatter.width - 10 - max_name
                rows = [(n, c.get_short_help_str(limit)) for n, c in items]
                intro = self.CATEGORY_INTRO.get(cat)
                header = f"{cat} ({intro}):" if intro else f"{cat}:"
                # git 风格：分类说明单独一段，其下命令名对齐成列并缩进一级
                formatter.write_text(header)
                with formatter.indentation():
                    formatter.write_dl(rows)
                formatter.write_paragraph()
                formatter.write_paragraph()

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


@click.group(cls=ContractGroup, invoke_without_command=True,
             context_settings=dict(help_option_names=["-h", "--help"]))
@click.version_option(__version__, "--version", "-v", prog_name="grape",
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
    from src.config import set_cli_params
    set_cli_params(base_url=base_url or "", token=token or "", timeout=timeout or 0)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _walk_commands(grp, prefix, out):
    for name in sorted(grp.commands):
        cmd = grp.commands[name]
        full = f"{prefix} {name}"
        if isinstance(cmd, click.Group):
            if _cmd_allowed(cmd):
                _walk_commands(cmd, full, out)
        elif _cmd_allowed(cmd):
            out.append(full)


def _tree_lines(pattern: str = "") -> list[str]:
    """列出【真实可调用】的契约命令（空格分隔、叶子展开），供 grape commands。"""
    try:
        schema = load_contract()
        groups = build_groups(schema)
    except Exception as e:
        return [f"contract load failed: {e} (check base_url/token or run grape refresh)"]
    lines = []
    for gname in sorted(groups):
        _walk_commands(groups[gname], gname, lines)
    lines = sorted(set(lines))
    if pattern:
        lines = [n for n in lines if pattern in n]
    return lines


@cli.command("refresh")
def refresh_cmd():
    """Force re-fetch the latest OpenAPI contract and cache it."""
    schema = load_contract(refresh=True)
    cli._groups = build_groups(schema)
    cli._loaded = True
    cli.contract_version = str(schema.get("info", {}).get("version", ""))
    from src.contracts import contract_source

    src_info = contract_source()
    src_note = f" (source: {src_info['kind']} {src_info['detail']})" if src_info.get("kind") else ""
    click.echo(
        f"contract refreshed: v{schema.get('info', {}).get('version', '?')} "
        f"({len(schema.get('paths', {}))} endpoints){src_note}"
    )


@cli.command(name="commands")
@click.argument("pattern", default="")
def commands_cmd(pattern: str):
    """List contract commands; filter with a pattern, e.g. 'grape commands issue'."""
    lines = _tree_lines(pattern)
    for line in lines:
        click.echo(line)
    if not pattern:
        click.echo("hint: filter commands with: grape commands <pattern>", err=True)
    if not lines:
        click.echo("no contract commands matched (run grape refresh or check base_url/token)")


refresh_cmd.token_scope = "dual"
commands_cmd.token_scope = "dual"

git_cmd = git_command()
git_cmd.token_scope = "session_only"
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