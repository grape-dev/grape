"""G4 防呆门禁：本链注册链与 weekly-10 基线（9c4c770b8）版本号撞号锁。

背景（2026-11-12 撞号根治）：本链曾有 4 处迁移与基线同号异名——
    202611080001 / 202611090001 / 202611090002 / 202611100001——
基线已盖章库（grape_dev_mgmt 派生库）按号跳过，本链 fn 静默不执行。
根治：4 处改号到本链独立号段 202611120001-202611120004（fn 与语义不变），
旧号从注册链移除。本测试锁住该不变式，防止 rebase/后续改动把撞号引回。

口径：
- 交集必须为空，或仅含「已裁定安全项」——即 rebase 从基线原样继承的
  同号同名迁移（同一条迁移，双方链都注册，语义一致，非撞号）；
- 任何「同号异名」= 撞号回归，红；
- 迁移链对账：每个版本号恰好注册一次（防重复盖章竞态）；每个 fn
  恰好注册一次（唯一豁免：_fn_issue_number_text_normalize 的
  归一→回改两段设计，202611090004 与 202611120004 有意共用）。

基线清单以硬编码为准（参照 `git show 9c4c770b8:src/api/db_setup.py`
的 register_migration(version=..., name=...) 字面量），不依赖运行时
import 基线对象——AST 静态解析本链 db_setup.py 注册链。
"""
import ast
import os
import sys
from collections import Counter

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

_DB_SETUP = os.path.join(ROOT, "src", "api", "db_setup.py")

# weekly-10 基线 tip 9c4c770b8 的 register_migration 版本→名字（git show 提取）
WEEKLY10_BASELINE_VERSIONS = {
    "202611020001": "create-patrol-findings-table",
    "202611020002": "rebuild-patrol-findings-unique-with-issue-id",
    "202611030001": "issue658_agent_runtimes_role_tags",
    "202611030003": "perf_obs_llm_usage_engine_index",
    "202611040001": "issue979_scene_nodes_skill_id_backfill",
    "202611040002": "perf_issue_repo_created_index",
    "202611040003": "perf_issue_status_priority_index",
    "202611060001": "create-branch-guard-state-table",
    "202611060002": "add-branch-guard-pending-columns",
    "202611060003": "backfill_events_terminal_completed_at",
    "202611070001": "issue865_agent_runtimes_model",
    "202611080001": "backfill_events_terminal_completed_at_1497",
    "202611080004": "task-liveness-revived-at",
    "202611090001": "add-autopilot-exec-agent-task-id",
    "202611090002": "repair-autopilot-exec-agent-task-id",
    "202611100001": "add-agent-tasks-trace-context",
    "202611100002": "create-merge-gate-log-table",
    "202611100003": "scene-pin-columns",
}

# 已裁定安全项：rebase 从基线原样继承的同号同名迁移（同一条迁移，非撞号）
_ADJUDICATED_SHARED = {
    v for v, n in WEEKLY10_BASELINE_VERSIONS.items()
    # 4 处历史撞号（同号异名）必须不在本链注册（G4 已改号 202611120001-4）
    if v not in ("202611080001", "202611090001", "202611090002", "202611100001")
}

# 唯一豁免的「一 fn 双注册」：无（G4 对账后每 fn 恰好一次注册）。
_FN_DOUBLE_REGISTER_ALLOWED: set = set()


def _registered_migrations():
    """AST 解析本链 db_setup.py 的全部 register_migration 字面量注册。

    返回 [(version, name, fn_name_or_None), ...]——不 import 模块（零副作用，
    不依赖 conftest 强制的 sqlite 环境）。
    """
    with open(_DB_SETUP, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_migration"):
            continue
        kw = {}
        for kwarg in node.keywords:
            if kwarg.arg in ("version", "name"):
                try:
                    kw[kwarg.arg] = ast.literal_eval(kwarg.value)
                except (ValueError, SyntaxError):
                    kw[kwarg.arg] = None
            elif kwarg.arg == "fn":
                if isinstance(kwarg.value, ast.Name):
                    kw["fn"] = kwarg.value.id
        out.append((kw.get("version"), kw.get("name"), kw.get("fn")))
    return out


def test_migration_no_baseline_version_collision():
    """撞号锁：本链与基线版本交集只允许「同号同名」的继承项，同号异名必红。"""
    ours = {v: n for v, n, _ in _registered_migrations() if v}
    collisions = []
    for ver, base_name in WEEKLY10_BASELINE_VERSIONS.items():
        if ver in ours and ours[ver] != base_name:
            collisions.append(
                f"{ver}: ours={ours[ver]!r} vs baseline={base_name!r}")
    assert not collisions, (
        "与 weekly-10 基线同号异名的迁移（撞号回归，基线已盖章库会按号跳过"
        "本链 fn）：\n" + "\n".join(collisions) +
        "\n请改号到本链独立号段（参照 G4：202611120001-4），并保证改号迁移幂等。")
    # 4 处历史撞号旧号必须已从注册链移除（G4 改号收口）
    for ver in ("202611080001", "202611090001", "202611090002", "202611100001"):
        assert ver not in ours, (
            f"历史撞号旧号 {ver} 不应再注册（G4 已改号 202611120001-0004）")
    # 交集若非空，必须全部落在已裁定安全项（继承的同号同名迁移）
    shared = set(ours) & set(WEEKLY10_BASELINE_VERSIONS)
    unadjudicated = shared - _ADJUDICATED_SHARED
    assert not unadjudicated, (
        f"与基线交集出现未裁定项 {sorted(unadjudicated)}——"
        "新增共享迁移必须先经撞号裁定（同名继承或改号）")


def test_migration_chain_reconciliation():
    """对账：每个版本号恰好注册一次；每个 fn 恰好注册一次（唯一豁免见常量）。"""
    regs = [r for r in _registered_migrations() if r[0] is not None]
    assert regs, "注册链为空——AST 解析失败？"
    versions = [v for v, _, _ in regs]
    dup_versions = [v for v, c in Counter(versions).items() if c > 1]
    assert not dup_versions, f"版本号重复注册（盖章竞态）：{dup_versions}"
    fns = [fn for _, _, fn in regs if fn]
    for fn, cnt in Counter(fns).items():
        if cnt > 1:
            assert fn in _FN_DOUBLE_REGISTER_ALLOWED, (
                f"fn {fn} 注册了 {cnt} 次——每个迁移 fn 恰好一次注册"
                "（新意图请写独立 fn）")
