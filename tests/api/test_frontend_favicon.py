"""页签图标契约：/favicon.ico 必须返回图片，不能落到 SPA 兜底。

回归背景：浏览器的「申请内测」页（Vue SPA，挂在 /static-vue/）没有声明 icon，
于是请求 /favicon.ico；而 SPA 兜底 `GET /{path:path}` 把它当页面路由返回
200 + index.html，浏览器拿到非图片内容只能显示默认空白图标。
"""

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


def test_favicon_returns_png_not_html():
    client = TestClient(_app())
    r = client.get("/favicon.ico")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("image/png"), r.headers
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "返回内容不是 PNG（说明落到了 SPA 兜底）"