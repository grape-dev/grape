"""字段说明气泡（AGHelp）必须在任何容器里都能完整显示。

2026-11-15 真浏览器实锤：原来气泡是 `position:absolute; z-index:50` 挂在 `.ap-group` 里，
而 `.ap-group { overflow:hidden }`（圆角裁切）把气泡裁掉，`elementFromPoint` 命中的是
页面容器 —— 现象就是"问号点了被遮住看不到"。修法是 Teleport 到 body + fixed 定位。
"""
from pathlib import Path

HELP = Path(__file__).resolve().parents[2] / "src/api/frontend/app/src/components/ui/AGHelp.vue"


def test_help_popover_escapes_clipping_containers():
    text = HELP.read_text(encoding="utf-8")
    assert "Teleport" in text, "气泡没有 Teleport 到 body（会被 overflow:hidden 祖先裁掉）"
    assert "position: fixed" in text, "气泡不是 fixed 定位"
    # z-index 必须高于 admin 页的浮层（弹层 2000），否则会被后半页元素盖住
    assert "z-index: 3000" in text, "气泡 z-index 太低，可能被页面元素覆盖"
    assert "position: absolute" not in text, "仍残留 absolute 定位（会被容器裁切）"


def test_help_popover_repositions_on_scroll_and_resize():
    """滚动/缩放时要重算位置，否则气泡会脱离问号（fixed 是相对视口的）。"""
    text = HELP.read_text(encoding="utf-8")
    assert "addEventListener('scroll'" in text and "addEventListener('resize'" in text
    assert "getBoundingClientRect" in text, "没有按锚点计算位置"
