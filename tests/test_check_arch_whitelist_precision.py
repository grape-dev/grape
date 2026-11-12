"""check-arch 白名单精度守卫（QA 复审 C-1 的负向用例）。

回归背景：13 条豁免曾按「文件 × 规则」登记（文件级），同文件今后新增的
kernel→domains / shim import 会被静默放行；已改为 (文件 × 导入目标) dict
targets 精确形态。本守卫双向锁死：
1. 每条 dict 形态登记的 targets == 该文件实际存在的对应违规 import 集合
   （多登记=陈旧豁免要删；少登记=新增依赖立刻红，不被吞掉）；
2. 字符串形态（整规则豁免）只允许存量条目，新增必须用 dict 形态。
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_arch_guard", REPO / "scripts" / "check-arch.py")
ca = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_arch_guard", ca)
_spec.loader.exec_module(ca)


def _imports_of_file(rel):
    _, imps = ca.imports_of(str(REPO / rel))
    return set(imps)


def _dict_entries():
    for rel, rules in ca.WHITELIST.items():
        for rule, spec in rules.items():
            if isinstance(spec, dict):
                yield rel, rule, spec


def test_r1_targets_exact_match_actual_kernel_to_domains_imports():
    for rel, rule, spec in _dict_entries():
        if rule != "R1":
            continue
        actual = {i for i in _imports_of_file(rel)
                  if i.startswith("src.domains.")
                  and ca.layer_of(i) in ca.DOMAIN_NAMES and ca.layer_of(i) != "contracts"}
        assert set(spec["targets"]) == actual, (
            f"{rel}: R1 targets {sorted(spec['targets'])} != 实际 {sorted(actual)}"
            "——多登记须删（豁免面收窄），少登记说明新增依赖未被 targets 覆盖")


def test_r6_targets_exact_match_actual_shim_imports():
    for rel, rule, spec in _dict_entries():
        if rule != "R6":
            continue
        actual = {i for i in _imports_of_file(rel)
                  if any(i == p or i.startswith(p + ".") for p in ca.LEGACY_SHIM_PREFIXES)}
        assert set(spec["targets"]) == actual, (
            f"{rel}: R6 targets {sorted(spec['targets'])} != 实际 {sorted(actual)}")


def test_new_whitelist_entries_must_use_precise_dict_form():
    """字符串形态=整规则豁免，只允许 Phase 5 前的历史存量；新条目必须 dict。"""
    legacy_string_form = {
        rel for rel, rules in ca.WHITELIST.items()
        if not rel.startswith("CYCLE:")   # R5 环键本就以成员集合精确匹配，不是文件路径
        for r, v in rules.items() if isinstance(v, str)
    }
    for rel in legacy_string_form:
        assert rel in ("src/kernel/config.py", "src/kernel/prompt_builder.py",
                       "src/kernel/worker_package.py", "src/kernel/token_resolver.py",
                       "src/kernel/session_manager.py", "src/kernel/repo_cleanup.py",
                       "src/kernel/repo_migrate.py", "src/kernel/help_docs_sync.py",
                       "src/kernel/auth_sdk/jwt_utils.py"), (
            f"{rel} 使用字符串整规则豁免——存量之外的新条目必须用 dict targets 形态")
