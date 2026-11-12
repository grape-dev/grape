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
    import re as _re

    native = {}
    # D105-14 改动点 C：模板插值第四形态（构建期已校验占位符/触发锚合法）。
    # 语义：trigger 非空才产出；template 占位符按中性字段替换，缺失 strict 抛错
    #（不静默保留字面量）；mode=append/prepend 且模板未引用 key 字段时与原值拼接。
    for key, rule in rules.items():
        if not (isinstance(rule, dict) and rule.get("template") is not None):
            continue
        trigger = rule.get("trigger") or key
        tval = neutral.get(trigger)
        if tval is None or tval == "":
            continue
        template = rule["template"]

        def _sub(m: "_re.Match") -> str:
            ph = m.group(1)
            v = neutral.get(ph)
            if v is None or v == "":
                raise ValueError(
                    f"template 占位符 {{{ph}}} 数据缺失（strict：不静默保留字面量）")
            return str(v)

        rendered = _re.sub(r"\{(\w+)\}", _sub, template)
        native_name = rule.get("native") or key
        mode = rule.get("mode") or "replace"
        if mode != "replace" and f"{{{key}}}" not in template:
            base_val = neutral.get(key) or ""
            rendered = (base_val + rendered) if mode == "append" else (rendered + base_val)
        native[native_name] = rendered

    for key, value in neutral.items():
        if value is None or value == "" or (isinstance(value, (list, dict)) and not value):
            continue
        rule = rules.get(key)
        if rule is None:
            native[key] = value
            continue
        # 台阶3：声明化 drop 形态（{"drop": true}）——providers/<name>.py 此前用
        # no-op transform 实现"显式丢弃"，纯 JSON 卡表达不了回调，补此形态。
        if isinstance(rule, dict) and rule.get("drop"):
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