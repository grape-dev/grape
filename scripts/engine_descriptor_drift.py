#!/usr/bin/env python3
"""引擎 descriptor drift 守卫（P0b B4，2026-11-13 spec）。

用法：
    python3 scripts/engine_descriptor_drift.py        # 退出码 0=零漂移，1=有漂移
    pytest tests/runtime/test_engine_descriptor_drift.py  # 单测（含人为造漂移）

对账面（散布静态值 vs descriptor 派生）：
1. executor.discover_backends 的 known 值域+cli/pip ←→ reg.discover_map()
2. instances.ENGINE_CANONICAL_PREFIXES ←→ reg.prefix_map()
3. mgmt/user/db.py ensure_default_runtime 的 _KNOWN 源码字符串 ←→ device_known 集
   （该白名单是函数内局部变量，无运行时符号——按源码文本比对，同 drift 语义）
4. 探活派发一致性：descriptor probe.impl/probe.env_hook ←→ probe_dispatch
   实现表（B5 合一后命令表已消——worker/insight 循环同走分派器，命令表
   漂移结构性不可能，守卫改查 token 注册齐备性）
5. engine_strategies.ENGINE_REGISTRY 键域 ←→ capabilities.sandbox_installable 集
   （spec 验收 A4：双注册表漂移即红）
6. instances.USAGE_RETRY_ENGINES ←→ usage_retry 集

设计依据 spec §7 风险表：守卫产物进 CI 比对而非人肉维护；脚本有单测。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.domains.runtime.engine.descriptor import get_registry


def collect_drift() -> list[str]:
    """收集全部漂移项（空列表 = 零漂移）。"""
    problems: list[str] = []
    reg = get_registry()
    all_engines = set(reg.all().keys())

    # 1. executor known 表
    import src.domains.runtime.engine.registry  # noqa: F401 先触发（绕开循环导入）
    from src.domains.runtime.agents import executor
    known = {r["name"]: {"cli": r["pip_package"], "pip": r["pip_package"],
                         "cli_name": r["name"]} for r in executor.discover_backends()}
    if set(known.keys()) != all_engines:
        problems.append(f"discover/known 值域漂移: known={sorted(known)} "
                        f"descriptor={sorted(all_engines)}")
    else:
        want = reg.discover_map()
        # known 的 cli 字段在 discover 结果里是 cli_path，不在时回退 pip 名——
        # 值级对账以 discover_map 的 cli/pip 为准
        for name, info in want.items():
            got_pip = known.get(name, {}).get("pip", "")
            if got_pip != info["pip"]:
                problems.append(f"discover/known pip 漂移: {name} known={got_pip!r} "
                                f"descriptor={info['pip']!r}")

    # 2. instances 前缀表
    from src.domains.runtime.engine import instances
    if reg.prefix_map() != instances.ENGINE_CANONICAL_PREFIXES:
        problems.append(f"prefix map 漂移: instances={instances.ENGINE_CANONICAL_PREFIXES} "
                        f"descriptor={reg.prefix_map()}")

    # 3. db.py _KNOWN 源码白名单（device_known）
    from src.domains.mgmt.user import db as user_db
    device_known = {c for c, d in reg.all().items() if d.get("device_known")}
    src_text = open(user_db.__file__, encoding="utf-8").read()
    problems.extend(_db_known_source_check(src_text, device_known))

    # 4. 探活派发一致性（B5 合一后命令表已消，改为 token 注册校验）：
    #    descriptor probe.impl / probe.env_hook 引用的实现必须在分派器注册表里
    from src.domains.runtime.engine import probe_dispatch
    for c, d in reg.all().items():
        impl = d["probe"].get("impl")
        if impl and impl not in probe_dispatch._ACP_IMPLS:
            problems.append(f"probe.impl 未注册: {c}={impl!r}")
        hook = d["probe"].get("env_hook")
        if hook and hook not in probe_dispatch._ENV_HOOKS:
            problems.append(f"probe.env_hook 未注册: {c}={hook!r}")

    # 5. engine_strategies.ENGINE_REGISTRY vs sandbox_installable
    from src.domains.runtime.agents.sandbox.backends import engine_strategies
    installable = {c for c, d in reg.all().items()
                   if d.get("capabilities", {}).get("sandbox_installable")}
    if set(engine_strategies.ENGINE_REGISTRY.keys()) != installable:
        problems.append(f"ENGINE_REGISTRY 漂移: registry={sorted(engine_strategies.ENGINE_REGISTRY)} "
                        f"descriptor.sandbox_installable={sorted(installable)}")

    # 6. usage retry 集
    want_retry = {c for c, d in reg.all().items() if d.get("usage_retry")}
    if instances.USAGE_RETRY_ENGINES != want_retry:
        problems.append(f"USAGE_RETRY_ENGINES 漂移: instances={instances.USAGE_RETRY_ENGINES} "
                        f"descriptor={want_retry}")

    return problems


def _db_known_source_check(src_text: str, device_known: set[str]) -> list[str]:
    """db.py _KNOWN 白名单源码比对。

    [P0b B3 后] 主路径已从 descriptor 派生（_KNOWN = {c for ...} 表达式），源码里
    只剩异常 fallback 的字面量白名单需要守卫——只抓「字面量形态」的 _KNOWN 赋值
    （集合推导式跳过），其值须与 descriptor.device_known 一致。
    """
    problems = []
    literals = []
    for m in re.finditer(r'_KNOWN = \{([^}]*)\}', src_text):
        body = m.group(1)
        if "for" in body:  # 派生表达式（descriptor 单源）——非比对对象
            continue
        literals.append(set(re.findall(r'"([^"]+)"', body)))
    if not literals:
        return [f"db.py _KNOWN 字面量 fallback 未找到（结构变更？须同步 drift 脚本）"]
    for got in literals:
        if got != device_known:
            problems.append(f"db._KNOWN 漂移: db={sorted(got)} descriptor.device_known={sorted(device_known)}")
    return problems


def main() -> int:
    problems = collect_drift()
    if problems:
        print("ENGINE DESCRIPTOR DRIFT DETECTED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("engine descriptor drift check: OK "
          f"({get_registry().count()} engines, 6 对账面零漂移)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
