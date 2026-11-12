#!/usr/bin/env python3
"""theme-lab 独立预览服务（不依赖 3000 API 进程）。

- 把构建产物 static-dist 挂到根路径，/static-vue/assets/... 重写成 /assets/...
- 未知路径回落到 index.html（vue-router history 模式需要）
- 端口默认 5179，0.0.0.0 便于局域网看
"""
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/home/mys/grape/src/api/frontend/static-dist"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5179


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        # 根路径直接落到试点页，省得记路径
        if self.path.split("?")[0] in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/theme-lab")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._file_exists(self.path.split("?")[0]):
            self.path = "/index.html"
        return super().do_GET()

    def _file_exists(self, p):
        f = os.path.join(ROOT, p.lstrip("/"))
        return os.path.isfile(f) or (p != "/" and os.path.isdir(f) and os.path.isfile(os.path.join(f, "index.html")))

    def send_head(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html") or path.endswith(".html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                body = f.read().replace(b"/static-vue/", b"/")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            import io

            return io.BytesIO(body)
        return super().send_head()

    def log_message(self, format, *args):  # noqa: A002 - 父类签名
        sys.stderr.write("%s %s\n" % (self.address_string(), format % args))


if __name__ == "__main__":
    print(f"theme-lab preview -> http://localhost:{PORT}/  (root={ROOT})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()