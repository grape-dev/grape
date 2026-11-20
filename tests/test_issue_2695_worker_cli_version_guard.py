"""Issue #2695：worker 侧 grape CLI 版本核对（grape-cli #1634 本地拦截是否在位）。

grape-cli #1634 在 CLI 契约缓存侧给 operationId=issues_pending-review 加了本地拦截：
非 `[0-9]+` 的 `--issue_id` 直接 CliError、不发请求，并提示「用 issues detail 的 issue_id 字段」。
该修复随 grape-cli 发版生效（外部依赖），worker 只做版本核对 + 可读升级提示，不阻断启动。

测试 seam（公开函数/模块常量）：
- `src.cli_version`：版本线单一事实源（常量 + 版本比较），打包侧/worker 侧复用同一对象
- `src.runtime.cli_version_guard.issue_id_intercept_hint(version, floor=...)`：低于生效门禁 → 可读提示；达标/未知/门禁未发布 → None
- `src.runtime.cli_version_guard.check(...)`：启动钩子结果可判读，未知版本不误报
- `src.domains.runtime.engine.agent._start_cli_version_check()`：启动期核对走后台线程（不阻塞 worker 启动）

版本门与降噪（QA 复审 201047685）：含 #1634 的 v0.3.4 已于 2026-11-14 发布（PR !16），
`src/cli_version.py:GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION = (0, 3, 4)`；标准 worker（随包 0.3.3）
启动会输出一次升级提示（预期，warn-only 不阻断），升级到 >= 0.3.4 后即静默。门禁值仍可由
环境变量 `GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION` 覆盖。「门禁未发布 → 静默」用例保留，用于钉住
降噪语义（monkeypatch 模拟为**场景假设**，非当前发布状态）。
"""
import pathlib

from src.kernel.cli_version import GRAPE_CLI_MIN_VERSION, parse_version, version_below
from src.domains.runtime.engine import cli_version_guard as guard

REPO = pathlib.Path(__file__).parent.parent


def test_parse_version_handles_probe_output_forms():
    assert parse_version("grape 0.3.3") == (0, 3, 3)
    assert parse_version("0.3.4") == (0, 3, 4)
    assert parse_version("0.4") == (0, 4)
    assert parse_version("") == ()
    assert parse_version("garbage") == ()


def test_version_below_is_numeric_not_lexical():
    assert version_below("0.3.3", "0.3.4") is True
    assert version_below("0.3.10", "0.3.9") is False   # 字典序会误判
    assert version_below("1.0", "0.9.9") is False
    assert version_below("0.3.3", "0.3.3") is False


def test_version_gate_is_single_source_of_truth():
    """打包侧（worker_package.py）与安装脚本必须共用同一门禁值，不得各自硬编码（QA 判断项 1）。"""
    from src.kernel import worker_package as wp  # arch11 打包器落位

    assert wp.GRAPE_CLI_MIN_VERSION is GRAPE_CLI_MIN_VERSION, \
        "worker_package 应复用 src.cli_version 的同一常量对象"
    assert guard.GRAPE_CLI_MIN_VERSION is GRAPE_CLI_MIN_VERSION, \
        "worker 侧版本核对应复用 src.cli_version 的同一常量对象（含同一比较函数）"
    assert guard.version_below is version_below, "版本比较函数须复用，不得二次实现"

    # arch11 适配：部署门常量由 worker_package 复用 kernel.cli_version（Python 单一事实源），
    # arch11 安装脚本不带门禁字面量（脚本只装轮子；版本核对在 worker 启动后台线程做）——
    # 脚本门禁字面量断言（sh/ps1）随形态差异豁免，以 Python 侧同值断言替代。
    assert wp.GRAPE_CLI_MIN_VERSION == GRAPE_CLI_MIN_VERSION, \
        "打包侧部署门常量与单一事实源同值（arch11 脚本不携带字面量为正当形态）"


def test_cli_version_module_is_packaged_for_worker():
    """worker 侧是 lazy import（agent.main），MANIFEST 漏项只会在 worker 上炸 → 显式钉住。"""
    from src.kernel import worker_package as wp  # arch11 打包器落位

    # arch11 适配：MANIFEST 以目录整包（src/kernel/ 已含 cli_version.py）。
    assert any(m in ("src/kernel/", "src/cli_version.py") for m in wp.MANIFEST), \
        "src/kernel/（含 cli_version.py）必须随 worker 包（否则启动核对 ModuleNotFoundError）"


def test_hint_when_cli_lacks_issue_id_interception():
    """门禁已发布且实测版本低于门禁 → 可读升级提示（含 issue_id 来源口径）。"""
    hint = guard.issue_id_intercept_hint("grape 0.3.3", floor=(0, 3, 4))
    assert hint, "低于门禁必须给出升级提示"
    assert "0.3.3" in hint and "0.3.4" in hint, hint          # 须同时点名实测版本与目标版本
    assert "issues detail" in hint and "issue_id" in hint    # 自诊断依据：issue_id 来源口径
    assert "#1634" in hint


def test_no_hint_when_cli_is_new_enough():
    assert guard.issue_id_intercept_hint("0.3.4", floor=(0, 3, 4)) is None
    assert guard.issue_id_intercept_hint("0.4.0", floor=(0, 3, 4)) is None


def test_no_hint_when_version_unknown():
    """探活失败/未装 CLI 时不误报（版本未知 ≠ 版本陈旧）。"""
    assert guard.issue_id_intercept_hint("", floor=(0, 3, 4)) is None
    assert guard.issue_id_intercept_hint("unknown", floor=(0, 3, 4)) is None


def test_check_reports_and_logs_hint_without_raising(monkeypatch):
    monkeypatch.setenv("GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION", "0.3.4")  # 显式固定门禁，隔离部署机环境变量
    lines = []
    out = guard.check(log=lines.append, version="0.3.3")
    assert out["status"] == "stale"
    assert any("issues detail" in ln for ln in lines), lines

    lines2 = []
    ok = guard.check(log=lines2.append, version="0.3.4")
    assert ok["status"] == "ok"
    assert lines2 == []


def test_check_stays_quiet_while_intercept_release_unpublished(monkeypatch):
    """降噪语义（**场景假设**：模拟含 #1634 的发版尚未发布）→ 不判定、不告警。

    当前发布状态为 v0.3.4 已发布（常量非 None）；此用例仍保留以钉住「未知 ≠ 陈旧」。
    """
    monkeypatch.delenv("GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION", raising=False)
    monkeypatch.setattr(guard, "intercept_floor", lambda: None)   # 语义 = 含 #1634 的发版尚未发布

    lines = []
    out = guard.check(log=lines.append, version="0.3.3")
    assert out["status"] == "ok", out
    assert lines == [], "门禁未发布时不得输出升级告警"


def test_check_never_raises_when_probe_fails():
    """probe 抛异常（未装 CLI / 超时）→ status=unknown，不阻断 worker 启动。"""
    def _boom(*a, **k):
        raise OSError("no grape binary")

    out = guard.check(probe=_boom)
    assert out["status"] == "unknown"


def test_worker_startup_runs_check_off_the_critical_path(monkeypatch):
    """启动期核对必须走后台线程：main 关键路径不得同步等待 `grape --version`（QA 判断项 2）。"""
    from src.domains.runtime.engine import agent as ra  # arch11 引擎落位

    calls = []
    monkeypatch.setattr(guard, "check", lambda **k: calls.append(k) or {"status": "ok", "version": "0.3.3"})

    t = ra._start_cli_version_check(log_sink=lambda m: None)
    assert t is not None and t.daemon is True, "版本核对须为 daemon 线程（不阻塞/不持住退出）"
    t.join(timeout=5)
    assert calls, "后台线程必须实际触发版本核对"
