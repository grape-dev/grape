"""前端功能开关（Issue #3395）——服务端单一下发源。

背景：插件市场已默认上线，对外演示时需要可按需关闭前端入口，但**后端
``/api/plugins*`` API 与插件/凭据数据必须保留**。为避免前端各视图自行解析
env/cookie 造成前后端判断不一致，这里在渲染 shell HTML 时注入唯一全局标记
``window.__FEATURES__``；flag=0 时同时把侧边栏插件入口整块移出 HTML（不是
``display:none`` 遮盖）。

env：
- ``GRAPE_FEATURE_PLUGINS``：默认 ``1``（开启）。仅 ``0/false/no/off`` 视为关闭。
"""
from __future__ import annotations

import json
import os
import re

# 显式关闭语义；其余任何值（含缺省/空串）都按默认开启处理，保证默认零变化。
_OFF_VALUES = {"0", "false", "no", "off", "disable", "disabled"}

# 侧边栏「🧩 插件市场」导航项：flag=0 时整块不输出（单行 <a> 块，无嵌套 </a>）。
_PLUGINS_NAV_RE = re.compile(
    r'<a\b[^>]*\bid="nav-plugins"[^>]*>.*?</a>\s*',
    re.DOTALL,
)


def plugins_enabled() -> bool:
    """插件市场前端入口是否开启（默认开启）。"""
    raw = os.environ.get("GRAPE_FEATURE_PLUGINS", "1")
    return str(raw).strip().lower() not in _OFF_VALUES


def feature_flags() -> dict:
    """当前全局特性标记（前端统一从 ``window.__FEATURES__`` 读取）。"""
    return {"plugins": plugins_enabled()}


def _flags_script() -> str:
    payload = json.dumps(feature_flags(), ensure_ascii=False, separators=(",", ":"))
    return f"<script>window.__FEATURES__={payload};</script>"


def apply_feature_flags(html: str) -> str:
    """在 shell HTML 上应用特性开关（幂等）。

    - 注入 ``window.__FEATURES__``（单一下发源，位置在 ``</head>`` 前，早于各视图 chunk）；
    - flag=0：根 ``<html>`` 打 ``data-plugins-off="1"``，并移除侧边栏插件入口整块。
    """
    if "window.__FEATURES__" not in html:
        html = html.replace("</head>", _flags_script() + "</head>", 1)

    if not plugins_enabled():
        if 'data-plugins-off="1"' not in html:
            html = re.sub(r"<html\b", '<html data-plugins-off="1"', html, count=1)
        html = _PLUGINS_NAV_RE.sub("", html)

    return html


# ---- 前端 RUM（Faro）注入开关 ----
# 上报反代 /faro/collect 的上游必须由 ``GRAPE_FARO_UPSTREAM`` 显式提供（Alloy
# faro.receiver）；未配置时反代固定 503，此时前端仍注入 SDK 只会每会话打空包
# → 这里在渲染 shell HTML 时整块移除（配了上游即自动恢复，无需改代码）。
_FARO_SCRIPT_RE = re.compile(
    r"[ \t]*<script[^>]*src=\"/static/js/vendor/faro-web-sdk\.iife\.js[^\"]*\"[^>]*>\s*</script>[ \t]*(?:<!--.*?-->)?[ \t]*\r?\n"
    r"[ \t]*<script[^>]*src=\"/static/js/vendor/faro-web-tracing\.iife\.js[^\"]*\"[^>]*>\s*</script>[ \t]*(?:<!--.*?-->)?[ \t]*\r?\n"
    r"[ \t]*<script[^>]*src=\"/static/js/faro\.js[^\"]*\"[^>]*>\s*</script>[ \t]*(?:<!--.*?-->)?[ \t]*\r?\n?",
    re.DOTALL,
)


def faro_enabled() -> bool:
    """前端 RUM 是否开启（= 反代上游是否配置）。"""
    return bool(os.environ.get("GRAPE_FARO_UPSTREAM", "").strip())


def apply_faro_toggle(html: str) -> str:
    """未配上游时移除 shell HTML 中的 Faro 脚本块（幂等）。"""
    if faro_enabled():
        return html
    return _FARO_SCRIPT_RE.sub("", html, count=1)