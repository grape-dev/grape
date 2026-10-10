"""grape git 本地执行（方案③）：git 原生命令透传 + 进程内凭据注入（token 零暴露）。

- 裸用自动识别仓库（docs/grape-git-auto-detect-design.md）：
  - clone：从 URL 参数解析 owner/repo（此时 cwd 尚无 .git）；
  - 其他：从当前目录 `remote.origin.url` 解析。
- 识别到 gitcode 仓库 → `GET /api/repos/{o}/{r}/git-credential` 换券，仅以 GIT_CONFIG env
  注入 git 子进程（不进命令行、不落磁盘、宿主非 gitcode 一律不注入）。
- 换券失败**降级**：仅 stderr 警告，不阻塞本地命令；远端操作按 git 原生结果。
- `--help`/`-h`/`help`：直接透传 git（与 `git --help` 一致，docs/grape-git-help-design.md）。
"""
import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

import click

from src.client import ApiClient, CliError, basic_auth

_GITCODE_HOSTS = frozenset({"gitcode.com", "www.gitcode.com"})

_EXTRA_HEADER = "http.extraheader"

# 无需凭据的顶层参数（帮助/版本类），跳过注入
_NO_CRED_CMD = frozenset({"help", "--help", "-h"})


def _repo_from_url(url: str) -> str:
    """从 git URL 解析 owner/repo（gitcode 形态），不匹配返回空串。

    支持：https://gitcode.com/o/r.git / git@gitcode.com:o/r.git / ssh://git@gitcode.com/o/r.git
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url and ":" in url and "/" not in url.split(":", 1)[0]:
        # scp-like：git@host:o/r.git
        host = url.split(":", 1)[0].split("@")[-1]
        path = url.split(":", 1)[1]
    else:
        u = urlsplit(url)
        host = u.hostname or ""
        path = u.path
    if host.lower() not in _GITCODE_HOSTS:
        return ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _repo_from_cwd(cwd: str | None = None) -> str:
    """读当前仓库 remote.origin.url 解析 owner/repo（子目录内也能取到）。"""
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except Exception:
        return ""
    return _repo_from_url(out.strip())


def _repo_from_args(args: list[str], cwd: str | None = None) -> str:
    """决定换券目标：clone 由 URL 解析，其他由 cwd origin 解析。"""
    if args and args[0] == "clone":
        for a in args[1:]:
            r = _repo_from_url(a)
            if r:
                return r
        return ""
    return _repo_from_cwd(cwd)


def _inject_git_credentials(owner_repo: str, env: dict) -> None:
    """换券并注入 http.extraheader 到子进程 env；凭据缺字段抛 CliError。"""
    api = ApiClient()
    _s, cred = api.request("GET", f"/api/repos/{owner_repo}/git-credential")
    username = cred.get("username")
    password = cred.get("password")
    if not username or not password:
        raise CliError(
            f"server git-credential response missing username/password (repo {owner_repo})"
        )
    idx = int(env.get("GIT_CONFIG_COUNT") or 0)
    env["GIT_CONFIG_COUNT"] = str(idx + 1)
    env[f"GIT_CONFIG_KEY_{idx}"] = _EXTRA_HEADER
    env[f"GIT_CONFIG_VALUE_{idx}"] = f"Authorization: {basic_auth(username, password)}"


def git_command() -> click.Command:
    @click.command(
        name="git",
        help="Run git locally: passthrough any native git command (credentials auto-injected in-process)",
        context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
        add_help_option=False,
    )
    @click.pass_context
    def git_cmd(ctx):
        if not shutil.which("git"):
            raise click.ClickException("git executable not found")

        env = dict(os.environ)
        args = list(ctx.args)
        # 帮助/版本类：纯透传，不做换券注入
        if not (args and args[0] in _NO_CRED_CMD):
            owner_repo = _repo_from_args(args)
            if owner_repo:
                try:
                    _inject_git_credentials(owner_repo, env)
                except CliError as e:
                    click.echo(
                        f"warning: grape git credential injection failed ({e}); "
                        "falling back to native git behavior",
                        err=True,
                    )

        cmd = ["git"] + args
        try:
            r = subprocess.run(cmd, env=env)
        except KeyboardInterrupt:
            # Ctrl+C 时 git 与 CLI 同控制组已收到中断；拉起主入口统一 130 收口
            raise
        except OSError as e:
            raise click.ClickException(f"git execution failed: {e}")
        sys.exit(r.returncode)

    return git_cmd