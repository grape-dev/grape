"""页签图标契约：/favicon.ico 必须返回真图标字节，不能落到 SPA 兜底。

回归背景：浏览器的「申请内测」页（Vue SPA，挂在 /static-vue/）没有声明 icon，
于是请求 /favicon.ico；而 SPA 兜底 `GET /{path:path}` 把它当页面路由返回
200 + index.html，浏览器拿到非图片内容只能显示默认空白图标。

图标素材统一用官网那张矢量 logo（shell/img/grape-logo.jpg）栅格化出的
favicon-32/48.png 与多尺寸 favicon.ico，不再用像素风 logo-64.png。
"""

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app():
    from src.api import server_common

    app = FastAPI()
    server_common.attach_frontend(app)
    return app


def test_favicon_route_registered_before_spa_catch_all():
    app = _app()
    paths = [r.path for r in app.routes]
    assert "/favicon.ico" in paths
    assert paths.index("/favicon.ico") < paths.index("/{path:path}"), (
        "favicon 必须注册在 SPA 兜底之前，否则会被兜底吞成 HTML")


def test_favicon_returns_icon_bytes_not_html():
    client = TestClient(_app())
    r = client.get("/favicon.ico")
    assert r.status_code == 200, r.text[:200]
    ctype = r.headers["content-type"].split(";")[0].strip()
    assert ctype in ("image/x-icon", "image/vnd.microsoft.icon"), ctype
    body = r.content
    assert not body.lstrip()[:15].lower().startswith(b"<!doctype"), "落到了 SPA 兜底（返回 HTML）"
    assert body[:4] == b"\x00\x00\x01\x00", "不是 ICO 容器（ICONDIR 魔数应为 00 00 01 00）"


def test_shell_and_spa_declare_the_same_favicon():
    """五个 shell 页面与 Vue SPA 都必须声明同一批图标，避免页签图标不一致。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pages = [
        os.path.join(root, "src/api/frontend/shell", f)
        for f in ("index.html", "admin.html", "help.html", "register.html", "forgot.html")
    ] + [os.path.join(root, "src/api/frontend/app/index.html")]
    for page in pages:
        with open(page, encoding="utf-8") as f:
            html = f.read()
        assert "favicon-32.png" in html, f"{page} 缺少 32x32 icon 声明"
        assert "favicon-48.png" in html, f"{page} 缺少 48x48 icon 声明"
        assert "/static/logo-64.png" not in html, f"{page} 仍在用像素风 logo-64.png"