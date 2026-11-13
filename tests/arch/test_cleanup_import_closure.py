"""导入闭包门禁（PR #1306 QA BLOCKED#1 整改）。

背景：rebase 融合曾把 cleanup.py 截断为 102 行（末行孤字恰好是合法
AST，py_compile 通过），而所有消费方都是函数内延迟 import，
`import src.api.main` 冒烟不报错，缺陷被静默掩盖，QA 架构审查
（评论 200376290）抓出。本门禁对 sweeper 全部公开符号做模块级
导入断言，任何同类截断/漂移在收集期即红。
"""
import importlib


def test_cleanup_module_import_closure():
    """sweeper 清收链全符号必须可从 cleanup 模块直接导入（消费方按名导入）。"""
    m = importlib.import_module("src.domains.runtime.engine.cleanup")
    # src/api/main.py -> sweep_loop
    # src/api/server_tasks.py -> sweep_events_loop / sweep_tasks_loop
    # src/api/git_repo_api.py -> ArtifactScope, precheck_issue_artifacts
    # src/domains/runtime/engine/agent.py -> sweep_loop
    for name in (
        "sweep_loop",
        "sweep_events_loop",
        "sweep_tasks_loop",
        "sweep_once",
        "sweep_stuck_events_once",
        "sweep_stuck_agent_tasks_once",
        "sweep_events_once",
        "sweep_tasks_once",
        "ArtifactScope",
        "precheck_issue_artifacts",
        "clean_issue_artifacts",
        # #2041：sweeper 侧 token 解析（_zombie_issue_dirs 删除守卫消费）
        "_resolve_repo_git_token",
    ):
        assert hasattr(m, name), f"cleanup.py 缺符号 {name}（疑似截断/融合丢失）"


def test_cleanup_source_not_truncated():
    """文件级防截断：行数下限 + 末条语句不得是裸表达式（末行孤字防御：
    历史截断形态为「孤字 'd'」——它恰好是合法 AST，py_compile 不报错，
    但作为模块最后一条语句语义上必为残片）。"""
    import ast
    import inspect
    src = inspect.getsource(
        importlib.import_module("src.domains.runtime.engine.cleanup"))
    lines = src.rstrip("\n").split("\n")
    assert len(lines) > 900, f"cleanup.py 仅 {len(lines)} 行，疑似被截断"
    tree = ast.parse(src)
    last = tree.body[-1]
    assert not isinstance(last, ast.Expr) or not isinstance(
        last.value, (ast.Name, ast.Constant)), \
        "cleanup.py 末条语句是裸表达式（疑似截断残片）"
