"""前端 RUM（Faro）注入开关回归。

来源：本机实障——未配 ``GRAPE_FARO_UPSTREAM`` 时反代 /faro/collect 固定 503，但前端仍注入
SDK，每个会话都在打空包。修法 = 渲染时按上游是否配置整块增删脚本（`frontend_flags.
apply_faro_toggle`），产品代码不动页面结构。

本测试锁三件事（都用真实 shell HTML，不构造假片段——结构一变就该响）：
1. 未配上游 → 三个 faro script 整块移除，其它 script 不受影响；
2. 配了上游 → 原样返回，重复调用幂等；
3. `faro.js` 自身保留 `__FARO_ENABLED__ === false` 运行时闸（浏览器缓存旧 HTML 的兜底）。
"""
from pathlib import Path

import pytest

from src.api import frontend_flags

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "src/api/frontend/shell/index.html"
FARO_JS = ROOT / "src/api/frontend/shell/js/faro.js"

FARO_SDK = "faro-web-sdk.iife.js"
FARO_TRACING = "faro-web-tracing.iife.js"
FARO_INIT = "/static/js/faro.js"


@pytest.fixture(autouse=True)
def _unset_upstream(monkeypatch):
    monkeypatch.delenv("GRAPE_FARO_UPSTREAM", raising=False)


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_real_shell_html_has_faro_block():
    """前提：真实页面里确实有这三条（否则下面的移除断言是空跑）。"""
    html = _html()
    assert FARO_SDK in html
    assert FARO_TRACING in html
    assert FARO_INIT in html


def test_removes_faro_block_when_upstream_unset():
    html = _html()
    out = frontend_flags.apply_faro_toggle(html)

    assert FARO_SDK not in out
    assert FARO_TRACING not in out
    assert FARO_INIT not in out

    # 不能把别的脚本一起删掉（正则贪婪/跨行的经典后果）
    assert "/static/js/api.js" in out
    assert "/static/js/app.js" in out
    assert "/static-vue/viewjs/plugins.js" in out
    assert len(out) < len(html)


def test_keeps_html_unchanged_when_upstream_configured(monkeypatch):
    monkeypatch.setenv("GRAPE_FARO_UPSTREAM", "http://127.0.0.1:12347")
    html = _html()

    assert frontend_flags.faro_enabled() is True
    assert frontend_flags.apply_faro_toggle(html) == html


def test_toggle_is_idempotent():
    once = frontend_flags.apply_faro_toggle(_html())
    assert frontend_flags.apply_faro_toggle(once) == once


def test_faro_js_keeps_runtime_guard():
    src = FARO_JS.read_text(encoding="utf-8")
    assert "__FARO_ENABLED__ === false" in src