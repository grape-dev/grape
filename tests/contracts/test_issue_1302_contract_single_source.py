"""Issue #1302（#2237 移植）：KNOWN_LOCAL_CLI_COMMANDS 契约单源锁定测试。

Port from weekly-1102-zrr tests/test_api_docs_sync.py 的 #2237 系断言（arch11 无
该文件，独立成 tests/contracts/test_issue_1302_contract_single_source.py）：
- 单一事实源：常量仅定义于 src/kernel/help_docs_sync.py 一处（全仓扫描）；
- 与契约 operation 集不相交（本地命令无 HTTP 语义，不进契约）；
- /openapi.json info["x-grape-local-cli-commands"] 与常量完全一致（发布面锁定）；
- 两份 README「CLI 命令映射」按新口径声明本地命令面且双向一致。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.help_docs_sync import (  # noqa: E402
    KNOWN_LOCAL_CLI_COMMANDS,
    cli_local_command_to_operation_id,
    load_contract_ops,
    _load_app_schema,
)

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_HELP_DIR = os.path.join(_REPO_ROOT, "help", "api")


def test_local_cli_commands_declared_non_empty():
    """本地命令清单非空且不重复——空清单会让契约边界断言退化为无守护（Issue #2237 条件 A）。"""
    assert KNOWN_LOCAL_CLI_COMMANDS, "KNOWN_LOCAL_CLI_COMMANDS 不得为空"
    assert len(set(KNOWN_LOCAL_CLI_COMMANDS)) == len(KNOWN_LOCAL_CLI_COMMANDS)


def test_local_cli_commands_not_in_contract():
    """声明的本地命令不得出现在契约 operation 集合——本地命令无 HTTP 语义，
    进契约会污染 SDK/文档条目（Issue #2237：grape commands 与各组 --help 口径不一致）。"""
    operation_ids = {op_id for op_id in load_contract_ops().values() if op_id}
    leaked = sorted(
        f"`grape {cmd}` (operation_id: {cli_local_command_to_operation_id(cmd)})"
        for cmd in KNOWN_LOCAL_CLI_COMMANDS
        if cli_local_command_to_operation_id(cmd) in operation_ids
    )
    assert not leaked, "本地命令混入契约: " + ", ".join(leaked)


def test_known_local_cli_commands_single_source():
    """单一事实源锁定：常量仅定义于 src/kernel/help_docs_sync.py 一处——禁止 shim/副本；
    扫描范围 = src/ + tests/ + scripts/ 全仓口径（QA 复审 P2：仅扫 src/ 时
    tests/scripts/ 再定义不拦）。"""
    import re as _re

    scan_roots = ("src", "tests", "scripts")
    defining = sorted(
        os.path.relpath(py, _REPO_ROOT).replace(os.sep, "/")
        for root in scan_roots
        for dirpath, _, files in os.walk(os.path.join(_REPO_ROOT, root))
        if "__pycache__" not in dirpath
        for fn in files if fn.endswith(".py")
        for py in [os.path.join(dirpath, fn)]
        if _re.search(r"^\s*KNOWN_LOCAL_CLI_COMMANDS\s*[:=]", open(py, encoding="utf-8").read(),
                      _re.MULTILINE)
    )
    assert defining == ["src/kernel/help_docs_sync.py"], (
        "KNOWN_LOCAL_CLI_COMMANDS 只允许在 src/kernel/help_docs_sync.py 定义一处"
        "（src/tests/scripts 全仓扫描），实际定义于: "
        + ", ".join(defining))


def test_openapi_info_publishes_local_cli_commands():
    """契约 info["x-grape-local-cli-commands"] 与常量完全一致——单一事实源经
    /openapi.json 发布，grape-cli 反向消费渲染 [local] 列示（Issue #2237 条件 A：
    值取自 KNOWN_LOCAL_CLI_COMMANDS，两处漂移必被本断言拦截）。"""
    schema = _load_app_schema()
    published = schema.get("info", {}).get("x-grape-local-cli-commands")
    assert published == list(KNOWN_LOCAL_CLI_COMMANDS), (
        f"/openapi.json info 字段与常量漂移: 发布={published!r}, "
        f"常量={list(KNOWN_LOCAL_CLI_COMMANDS)!r}")


def _readme_local_cli_enumeration(readme: str) -> list[str]:
    """提取「CLI 命令映射」行内声明的本地命令清单（`grape X` 反引号片段）。

    只解析「派生列示本地命令（…行尾带」声明段——该段即本地命令面的 README 枚举；
    行内其余 `grape` 提及（契约派生示例、`--help` 占位）不属本地命令声明，不作比对。
    """
    import re as _re

    commands: list[str] = []
    for line in readme.splitlines():
        if "**CLI 命令映射**" not in line:
            continue
        for seg in _re.findall(r"派生列示本地命令（(.*?)行尾带", line):
            commands += _re.findall(r"`grape ([a-z][a-z0-9 _-]*?)`", seg)
    return commands


def test_readme_declares_local_cli_commands_boundary():
    """两份 README「CLI 命令映射」须按新口径声明本地命令面，且清单与常量双向一致——
    正向：常量 ⊆ README；反向：README 声明段不得列常量之外的本地命令（陈旧命令
    日后常量删项未同步必红，QA 复审 P1）；本地命令经契约 info 字段派生列示、带
    [local]，以 `grape <group> --help` 为准（Issue #2237；防文档口径与
    KNOWN_LOCAL_CLI_COMMANDS 漂移）。"""
    for readme_path in (os.path.join(_HELP_DIR, "README.md"),
                        os.path.join(_REPO_ROOT, "docs", "openapi", "README.md")):
        with open(readme_path, encoding="utf-8") as f:
            readme = f.read()
        missing = [cmd for cmd in KNOWN_LOCAL_CLI_COMMANDS if f"`grape {cmd}`" not in readme]
        assert not missing, (
            f"{readme_path} 本地命令声明与常量不一致，缺: "
            + ", ".join(f"`grape {c}`" for c in missing))
        listed = _readme_local_cli_enumeration(readme)
        assert listed, (
            f"{readme_path} 「CLI 命令映射」缺「派生列示本地命令（…行尾带 [local]）」"
            "本地命令声明段，反向断言无从比对（口径标记漂移）")
        stale = sorted({cmd for cmd in listed if cmd not in KNOWN_LOCAL_CLI_COMMANDS})
        assert not stale, (
            f"{readme_path} 「CLI 命令映射」列出了 KNOWN_LOCAL_CLI_COMMANDS 之外的"
            "本地命令（陈旧清单未随常量收敛）: "
            + ", ".join(f"`grape {c}`" for c in stale))
        assert "x-grape-local-cli-commands" in readme, \
            f"{readme_path} 缺「本地命令经契约 info 字段派生列示」口径"
