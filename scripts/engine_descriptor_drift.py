#!/usr/bin/env python3
"""引擎 descriptor drift 守卫（P0b B4，2026-11-13 spec）。

用法：
    python3 scripts/engine_descriptor_drift.py        # 退出码 0=零漂移，1=有漂移
    pytest tests/runtime/test_engine_descriptor_drift.py  # 单测（含人为造漂移）

对账面（散布静态值 vs descriptor 派生）：
1. executor.discover_backends 的 known 值域+cli/pip ←→ reg.discover_map()（cli 面
   依赖 executor 结果行携带 discover.cli——消费证据，不是 pip 换名自比）
2. reg.prefix_map() ←→ _FROZEN_PREFIX 静态冻结基线（instances 前缀表已派生，
   自比恒真，真漂移靠冻结基线）
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


# prefix 冻结基线（drift 面 2）：与 reg.prefix_map() 全量比对。引擎键/别名
# 变更 = 刻意的基线推进（改 descriptor 必须同步改这里），漏改即红——这就是
# 「新增引擎漏配即红」承诺的落点。注意：基线变更须在 commit message 里显式说明。
_FROZEN_PREFIX: dict[str, str] = {
    # opencode 为 is_default 引擎，不在 prefix_map（未知名 fallback，见 descriptor.py）
    "hermes": "hermes",
    "hwcloud": "hwcloud",
    "aishell": "hwcloud",
    "aishell-dsh": "aishell-dsh",
    "codex": "codex",
    "codearts": "codearts",
    "dsh": "dsh",
    "atomcode": "atomcode",
    "mock-acp": "mock-acp",
}


def collect_drift() -> list[str]:
    """收集全部漂移项（空列表 = 零漂移）。"""
    problems: list[str] = []
    reg = get_registry()
    all_engines = set(reg.all().keys())

    # 1. executor known 表
    # [F3 口径声明·PR #2047 复审] 本面为**结构自检**而非漂移拦截：discover 的
    # cli/pip 本身由 reg.discover_map() 派生（executor.py discover），对账恒真——
    # 保留意义是锁「discover 结果的形状」（引擎名全集/键结构）随 descriptor 集
    # 变化；真正拦 discover 覆盖面的是面 2（prefix 冻结基线）与面 3（db._KNOWN
    # 字面量 fallback）。六个面的分级：2/3/4/5 = 拦截面，1/6 = 结构自检面。
    import src.domains.runtime.engine.registry  # noqa: F401 先触发（绕开循环导入）
    from src.domains.runtime.agents import executor
    known = {r["name"]: {"cli": r.get("cli", ""), "pip": r.get("pip_package", "")}
             for r in executor.discover_backends()}
    if set(known.keys()) != all_engines:
        problems.append(f"discover/known 值域漂移: known={sorted(known)} "
                        f"descriptor={sorted(all_engines)}")
    else:
        want = reg.discover_map()
        # known 的 cli 字段在 discover 结果里是 cli_path，不在时回退 pip 名——
        # 值级对账以 discover_map 的 cli/pip 为准
        for name, info in want.items():
            got_cli = known.get(name, {}).get("cli", "")
            if got_cli != info["cli"]:
                problems.append(f"discover/known cli 漂移: {name} known={got_cli!r} "
                                f"descriptor={info['cli']!r}")
            got_pip = known.get(name, {}).get("pip", "")
            if got_pip != info["pip"]:
                problems.append(f"discover/known pip 漂移: {name} known={got_pip!r} "
                                f"descriptor={info['pip']!r}")

    # 2. prefix 面：对静态冻结基线（instances.ENGINE_CANONICAL_PREFIXES 已由
    #    prefix_map() 派生，与派生源自比恒真——真漂移探测改为对冻结基线比对，
    #    引擎名/别名变更必须显式更新 _FROZEN_PREFIX，漏更新即红）。
    from src.domains.runtime.engine import instances
    if reg.prefix_map() != _FROZEN_PREFIX:
        problems.append(f"prefix map 漂移: frozen={_FROZEN_PREFIX} "
                        f"descriptor={reg.prefix_map()}")
    if instances.ENGINE_CANONICAL_PREFIXES != reg.prefix_map():
        problems.append("instances.ENGINE_CANONICAL_PREFIXES 与 descriptor 派生不一致"
                        "（派生链断裂，须检查 instances.py）")

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
        args_hook = d["probe"].get("args_hook")
        if args_hook and args_hook not in probe_dispatch._ARGS_HOOKS:
            problems.append(f"probe.args_hook 未注册: {c}={args_hook!r}")

    # 5. engine_strategies.ENGINE_REGISTRY vs sandbox_installable
    from src.domains.runtime.agents.sandbox.backends import engine_strategies
    installable = {c for c, d in reg.all().items()
                   if d.get("capabilities", {}).get("sandbox_installable")}
    if set(engine_strategies.ENGINE_REGISTRY.keys()) != installable:
        problems.append(f"ENGINE_REGISTRY 漂移: registry={sorted(engine_strategies.ENGINE_REGISTRY)} "
                        f"descriptor.sandbox_installable={sorted(installable)}")

    # 6. usage retry 集
    # [F3 口径声明] 同面 1：USAGE_RETRY_ENGINES 由 descriptor usage_retry 派生
    # （instances.py），本面为结构自检；usage 消费面的真拦截在消费点单测。
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
