"""Issue #4203：部署配置断言——两条 worker 启动路径的内存/重启参数 + alerts.yml。

防止后续改单元/告警/启动脚本时把 Issue #4203 的显式化参数（Memory*/Restart*）或
OOM/内存告警规则无声删掉，或只改其中一条启动路径（QA 复审 Critical：
生产 worker 走 grape@worker.service → scripts/grape-systemd-run.sh，静态单元不一定生效）。
"""
import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
UNIT = os.path.join(ROOT, "deploy", "systemd", "grape-worker-server.service")
DROPIN = os.path.join(ROOT, "deploy", "systemd", "grape-worker-oom.conf")
RUNNER = os.path.join(ROOT, "scripts", "grape-systemd-run.sh")
INSTALLER = os.path.join(ROOT, "scripts", "install-grape-systemd.sh")
LAUNCHER = os.path.join(ROOT, "scripts", "start-worker.py")
ALERTS = os.path.join(ROOT, "deploy", "alerts.yml")


def _read(path):
    with open(path) as f:
        return f.read()


def _directives(text):
    """取形如 `MemoryMax=8G` 的赋值行（含 drop-in 的 [Service] 段）。"""
    return {m.group(1): m.group(2) for m in re.finditer(
        r"^(MemoryAccounting|MemoryHigh|MemoryMax|Restart|RestartSec|RestartSteps|"
        r"RestartMaxDelaySec)=(\S+)$", text, re.M)}


class TestWorkerUnit:
    def _unit(self):
        return _read(UNIT)

    def test_memory_directives_explicit(self):
        unit = self._unit()
        assert "MemoryAccounting=yes" in unit
        assert "MemoryHigh=" in unit and "MemoryHigh=6G" in unit
        assert "MemoryMax=" in unit and "MemoryMax=8G" in unit

    def test_restart_convergence_params(self):
        unit = self._unit()
        assert "RestartSec=1" in unit
        assert "Restart=always" in unit
        assert "RestartSteps=5" in unit
        assert "RestartMaxDelaySec=30" in unit

    def test_execstart_uses_prebind_launcher(self):
        """ExecStart 走预 bind launcher（先监听再 lifespan），不再直接 uvicorn bind。"""
        unit = self._unit()
        exec_start = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        assert exec_start and "start-worker.py" in exec_start[0]
        assert "--port" not in exec_start[0]

    def test_existing_drain_and_ready_kept(self):
        """#3969 的摘流/readyiness 门禁不因本次改动被删。"""
        unit = self._unit()
        assert "ExecStop=/opt/grape/deploy/systemd/grape-worker-drain.sh" in unit
        assert "grape-worker-ready.sh" in unit
        assert "GRAPE_DRAIN_FILE=" in unit


class TestGraptAtWorkerPath:
    """生产实际入口：grape@worker.service → grape-systemd-run.sh（QA 复审 Critical）。"""

    def _runner(self):
        return _read(RUNNER)

    def test_worker_role_execs_prebind_launcher(self):
        script = self._runner()
        worker_branch = script.split('if [ "$role" = "worker" ]', 1)
        assert len(worker_branch) == 2, "worker 角色分支缺失：预 bind 未覆盖 grape@ 路径"
        branch_body = worker_branch[1].split("\nfi", 1)[0]
        assert "start-worker.py" in branch_body
        # 其它角色仍走原 uvicorn exec（mgmt/event/insight 行为不变）
        assert '-m uvicorn' in script

    def test_worker_branch_passes_port_and_single_process(self):
        branch_body = self._runner().split('if [ "$role" = "worker" ]', 1)[1].split("\nfi", 1)[0]
        assert "GRAPE_WORKER_PORT" in branch_body
        # 生产既有语义是单进程：显式 1，避免换成多 worker 改变信号/生命周期行为
        assert "GRAPE_UVICORN_WORKERS" in branch_body and ':-1' in branch_body

    def test_worker_port_fact_source_is_role_mapping(self):
        """QA 复审 Optional：端口以角色映射为唯一事实源，外部旧值 fail-fast 不静默生效。"""
        branch_body = self._runner().split('if [ "$role" = "worker" ]', 1)[1].split("\nfi", 1)[0]
        assert 'export GRAPE_WORKER_PORT="$port"' in branch_body
        assert "exit 6" in branch_body, "外部端口与事实源不一致时应 fail-fast"

    def test_runner_exports_process_role_for_all_roles(self):
        """QA 复审 Optional：process 标签必须绑定部署角色（否则 grape@ 路径是 runtime）。"""
        script = self._runner()
        assert 'export GRAPE_PROCESS_ROLE="$role"' in script
        # 必须在 exec 之前导出（Python 只在进程启动时读一次）
        assert script.index('export GRAPE_PROCESS_ROLE="$role"') < script.index("exec \"$PY\"")

    def test_runner_worker_branch_execs_launcher_by_script_path(self):
        """SRE 部署复核 Critical：grape@worker 分支以脚本路径 exec（与静态单元同形态）。"""
        branch_body = self._runner().split('if [ "$role" = "worker" ]', 1)[1].split("\nfi", 1)[0]
        assert 'exec "$PY" "$REPO_ROOT/scripts/start-worker.py"' in branch_body
        assert "-m uvicorn" not in branch_body, "worker 分支回退 -m 形态会绕开预 bind launcher"


class TestExecStartImportGuarantee:
    """SRE 部署复核 Critical（PR #2092）：ExecStart 以「脚本路径」启动 launcher 时，
    ``python -m`` 把 cwd 加进 sys.path 的隐式行为消失，``src.api.*`` 的导入必须有
    独立保障，否则 uvicorn config.load 阶段 ModuleNotFoundError → exit 1 →
    systemd 反复重启（生产 4 分钟 worker 不可用的根因）。断言该保障锚在 launcher
    自身（sys.path 自举），且两条路径都不依赖外部 PYTHONPATH——SRE 现场曾以
    drop-in ``40-pythonpath.conf`` 临时救急，env 侧兜底会把 launcher 缺自举的回归
    掩盖成「生产能跑、CI/新机器必炸」，故仓库内保障必须落在脚本里。真启动级验证
    见 tests/scripts/test_issue_4203_start_worker.py::TestRealStartupSeam。"""

    def _launcher_tree(self):
        return ast.parse(_read(LAUNCHER))

    def test_static_unit_execstart_is_script_path_not_module_flag(self):
        """静态单元 ExecStart 是脚本路径形态（非 ``-m``，cwd 不隐式进 sys.path）。"""
        exec_start = [ln for ln in _read(UNIT).splitlines() if ln.startswith("ExecStart=")]
        assert exec_start and "start-worker.py" in exec_start[0]
        assert " -m " not in exec_start[0], (
            "-m 形态依赖 cwd 进 sys.path 的隐式行为，脚本路径形态的 sys.path 保障成为死代码")

    def test_launcher_bootstraps_repo_root_into_sys_path(self):
        """launcher 顶层自带仓库根 sys.path 自举（修复本体，AST 级锚定）。"""
        tree = self._launcher_tree()
        src = _read(LAUNCHER)
        inserts = []

        def _scan(stmts):
            for n in stmts:
                if isinstance(n, ast.If):
                    _scan(n.body)
                elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                    func = n.value.func
                    if (isinstance(func, ast.Attribute) and func.attr == "insert"
                            and isinstance(func.value, ast.Attribute)
                            and func.value.attr == "path"):
                        inserts.append(n.value)

        _scan(tree.body)
        assert inserts, "start-worker.py 缺顶层 sys.path.insert：脚本路径启动时仓库根不可导入"
        assert any(isinstance(c.args[0], ast.Constant) and c.args[0].value == 0
                   for c in inserts), "必须 insert(0, ...)（优先于第三方同名 src 包）"
        run_defs = [n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_run"]
        assert run_defs, "start-worker.py 缺 _run（uvicorn 导入入口）"
        assert max(c.lineno for c in inserts) < run_defs[0].lineno, (
            "sys.path 自举必须在模块顶层、先于任何 uvicorn/app 导入路径执行")
        # 仓库根推导锚定：本文件上级目录（跟随部署位置，不写死绝对路径）
        assert "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))" in src

    def test_no_path_side_pythonpath_dependency(self):
        """两条启动路径均不写 PYTHONPATH（env 侧兜底会掩盖 launcher 缺自举的回归）。"""
        assert "PYTHONPATH" not in _read(UNIT), (
            "静态单元不应靠 Environment=PYTHONPATH 保 app 导入——真启动回归会被 env 掩盖")
        branch_body = (_read(RUNNER).split('if [ "$role" = "worker" ]', 1)[1]
                       .split("\nfi", 1)[0])
        assert "PYTHONPATH" not in branch_body, (
            "grape@worker 分支不应 export PYTHONPATH——真启动回归会被 env 掩盖")

    def test_dropin_params_match_static_unit(self):
        """两条路径 Memory*/Restart* 字面值必须相等（否则同一单在不同机器行为不同）。"""
        unit_dirs = _directives(_read(UNIT))
        dropin_dirs = _directives(_read(DROPIN))
        for key in ("MemoryAccounting", "MemoryHigh", "MemoryMax",
                    "RestartSec", "RestartSteps", "RestartMaxDelaySec"):
            assert key in dropin_dirs, f"drop-in 缺 {key}"
            assert dropin_dirs[key] == unit_dirs.get(key), (
                f"{key} 漂移：drop-in={dropin_dirs[key]} unit={unit_dirs.get(key)}")

    def test_installer_ships_dropin_for_worker(self):
        installer = _read(INSTALLER)
        assert "grape-worker-oom.conf" in installer
        assert "30-worker-oom.conf" in installer
        # 只装 worker 角色（mgmt/event/insight 不需要内存限额）
        assert 'if [ "$role" = "worker" ]' in installer

    def test_runbook_documents_both_paths(self):
        runbook = _read(os.path.join(ROOT, "docs", "weekly-11", "deployment-runbook.md"))
        assert "grape-systemd-run.sh" in runbook
        assert "grape-worker-oom.conf" in runbook
        assert "MemoryMax" in runbook


class TestAlerts:
    def _alerts(self):
        pytest.importorskip("yaml")
        import yaml
        with open(ALERTS) as f:
            return yaml.safe_load(f)

    def test_oom_alert_is_critical_and_uses_node_vmstat(self):
        rules = {r["alert"]: r for g in self._alerts()["groups"] for r in g["rules"]}
        rule = rules["GrapeWorkerOOMKilled"]
        assert rule["labels"]["severity"] == "critical"
        assert "node_vmstat_oom_kill" in rule["expr"]

    def test_near_limit_alert(self):
        rules = {r["alert"]: r for g in self._alerts()["groups"] for r in g["rules"]}
        rule = rules["GrapeProcessMemoryNearLimit"]
        assert rule["labels"]["severity"] == "warning"
        assert "grape_process_memory_ratio" in rule["expr"]
        assert 'process="worker"' in rule["expr"]
        assert rule["for"] == "10m"

    def test_rss_growth_alert(self):
        rules = {r["alert"]: r for g in self._alerts()["groups"] for r in g["rules"]}
        rule = rules["GrapeWorkerRSSGrowth"]
        assert "grape_process_memory_rss_bytes" in rule["expr"]
        assert "deriv(" in rule["expr"] and "increase(" in rule["expr"]
        assert rule["expr"].count('process="worker"') == 2, "两个子查询都要按 worker 过滤"

    def test_alert_metric_names_match_module(self):
        """规则里的指标名与 process_memory.py 定义的 gauge 一致（防漂移）。"""
        from src.domains.insight.observability import process_memory as pm
        from prometheus_client import CollectorRegistry
        reg = CollectorRegistry()
        saved = (pm._rss_gauge, pm._limit_gauge, pm._ratio_gauge,
                 pm._unbounded_gauge, pm._engine_child_gauge)
        try:
            pm._rss_gauge = pm._limit_gauge = pm._ratio_gauge = None
            pm._unbounded_gauge = pm._engine_child_gauge = None
            pm.install(reg)
            names = set()
            for collector in reg._names_to_collectors.values() if hasattr(reg, "_names_to_collectors") else []:
                names.update(getattr(collector, "_names", set()))
            from prometheus_client import generate_latest
            body = generate_latest(reg).decode()
            for metric in ("grape_process_memory_rss_bytes",
                           "grape_process_memory_ratio",
                           "grape_process_memory_limit_bytes",
                           "grape_engine_child_rss_bytes"):
                assert metric in body
        finally:
            (pm._rss_gauge, pm._limit_gauge, pm._ratio_gauge,
             pm._unbounded_gauge, pm._engine_child_gauge) = saved
