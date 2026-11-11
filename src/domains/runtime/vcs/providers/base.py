"""中性 Git 契约 base（git-repo-provider-neutral-interface-design.md §4.2）。

apply_rules：把中性字段 dict 按 (resource, op) 规则翻译为 provider 原生 payload。
"""


def apply_rules(rules: dict, neutral: dict, ctx: dict) -> dict:
    """统一翻译引擎：中性字段 → 原生字段/值映射/transform 回调。

    规则形态（rule）：
    - str                         → 原生字段名，值原样拷贝
    - {"native": name, "values": {...}} → 原生字段名 + 值映射
    - {"native": None, "transform": fn} → 无原生字段，fn(value, neutral, native, ctx) 写 native
    - 无规则                     → 原样透传（中性字段名即原生字段名）
    空值（None/""/空容器）跳过。
    """
    native = {}
    for key, value in neutral.items():
        if value is None or value == "" or (isinstance(value, (list, dict)) and not value):
            continue
        rule = rules.get(key)
        if rule is None:
            native[key] = value
            continue
        if isinstance(rule, str):
            native[rule] = value
            continue
        fn = rule.get("transform")
        if fn is not None:
            fn(value, neutral, native, ctx)
            continue
        name = rule.get("native")
        values = rule.get("values")
        val = values.get(value, value) if values and value in values else value
        if name and val not in (None, ""):
            native[name] = val
    return native