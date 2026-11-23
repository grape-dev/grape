"""插件健康自检（Issue #1089 挂点④）。

derive_plugin_health：对比引擎期望插件（desired_plugins = 控制台远程安装请求
∪ Agent 类型种子默认）与 Runtime 上报的 installed_plugins，产出自检结果
（missing / healthy），供引擎详情与前端健康徽标消费。
"""
import json


def _parse_names(raw, key: str = "name") -> list[str]:
    """JSON/列表 → 规范化插件名列表（dict 条目取 name，字符串直接用）。"""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        if isinstance(item, dict):
            v = item.get(key)
        else:
            v = item
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    return sorted(set(names))


def derive_plugin_health(installed_raw, desired_raw) -> dict:
    """健康自检：installed/desired 均接受 JSON 字符串或列表。

    返回 {installed, desired, missing, extra, healthy}；
    healthy = missing 为空（多装的 extra 不扣健康，允许本地试验插件）。
    """
    installed = _parse_names(installed_raw)
    desired = _parse_names(desired_raw)
    installed_set = set(installed)
    missing = [d for d in desired if d not in installed_set]
    desired_set = set(desired)
    extra = [i for i in installed if i not in desired_set]
    return {
        "installed": installed,
        "desired": desired,
        "missing": missing,
        "extra": extra,
        "healthy": not missing,
    }
