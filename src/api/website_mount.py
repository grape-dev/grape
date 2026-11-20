"""可选的官网静态站挂载：WEBSITE_DIR 设置时在 /website/* 提供静态内容（测试机预览用）。"""
import os
from fastapi.staticfiles import StaticFiles


def install_website_routes(app) -> None:
    ws_dir = os.environ.get("WEBSITE_DIR")
    if not ws_dir or not os.path.isdir(ws_dir):
        return
    mounted = {getattr(r, "name", None) for r in app.routes}
    if "website_static" in mounted:
        return
    app.mount("/website", StaticFiles(directory=ws_dir, html=True), name="website_static")
    route = app.routes.pop()
    app.routes.insert(0, route)
