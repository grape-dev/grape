# -*- coding: utf-8 -*-
"""Issue #3482（A/C）：direct 与 worker 两条执行路径的建树时序 / fail-closed 回归。

根因不是「目录被 .skills 占住」，而是 **direct 路径物化早于建树**（顺序缺陷）：
- direct（direct_executor._execute_agent_inner）此前 `_materialize_direct_workdir`
  （写 .skills/.project-info/.knowledge）先于 `ensure_issue_worktree`；
- worker（agent._execute_task）被写死为「必须先建 worktree 再落地技能」（#711 方案 A）。

顺序不一致 → 目标目录先被物化占住（无 `.git`）→ `git worktree add` 报 already exists。
本用例对两条路径做源码级顺序看护（顺序回归会直接失败），并看护 C 的 fail-closed：
建树失败必须显式 failed，不得 fail-open 跑成「只读 completed」。
"""
import inspect
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine import agent, direct_executor


def test_direct_materialize_runs_after_worktree_create():
    src = inspect.getsource(direct_executor._execute_agent_inner)
    i_wt = src.index("ensure_issue_worktree(")
    i_mat = src.index("_materialize_workdir_and_skill_gate(")
    assert i_wt < i_mat, (
        "Issue #3482（A）：direct 端必须先建 worktree 再物化，"
        "否则目标目录被 .skills 占住 → worktree add 报 already exists")


def test_materialize_gate_moves_skills_and_consistency_together():
    """物化需整体下移，不能只移一半：技能/知识/project-info 物化与 _skill_consistency
    校验必须在同一函数内（校验依赖物化结果）。"""
    src = inspect.getsource(direct_executor._materialize_workdir_and_skill_gate)
    assert "_materialize_direct_workdir" in src
    assert "SKILL_CONSISTENCY_KEY" in src or "_skill_consistency" in src
    assert "prompt writeback" in src


def test_direct_worktree_failure_is_fail_closed():
    """验收 3：建树失败 → 任务 failed（workspace 不可用），不得 fail-open 只读 completed。"""
    src = inspect.getsource(direct_executor._execute_agent_inner)
    assert "_wt_wanted and not _wt" in src, "建树失败必须有显式分支"
    assert "workspace_unavailable" in src, "失败原因需机器可读（completed_reason/result_text）"
    assert "fail_task" in src, "任务需置 failed（含 completed_reason），不是仅 log.warning"


def test_worker_worktree_failure_is_fail_closed():
    """验收 4：direct 与 worker 两条路径同语义——worker 建树失败同样显式失败。"""
    src = inspect.getsource(agent._execute_task_impl)
    assert "_wt_wanted" in src and "workspace_unavailable" in src
    assert '"_started": False' in src
    # worker 侧仍保持「先建 worktree 再落地技能」的既有顺序（#711 方案 A）
    i_wt = src.index("ensure_issue_worktree(")
    i_skill = src.index("skill_dir = _os.path.join(cwd", i_wt)
    assert i_wt < i_skill, "worker 端顺序不得回归为「先技能后 worktree」"