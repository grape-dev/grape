"""管理台 HTTP 服务（端口 80）：登录、账户 CRUD、测试连接"""
import json, os, time, hashlib, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

_ADMIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_PATH = os.path.join(_ADMIN_DIR, "page.html")
ADMIN_USER = "developer"
ADMIN_PASS = "YOUR_SSH_PASSWORD"
_admin_tokens: dict[str, float] = {}

_PAGE_HTML = ""
if os.path.exists(_PAGE_PATH):
    with open(_PAGE_PATH, encoding="utf-8") as f:
        _PAGE_HTML = f.read()


class AdminHandler(BaseHTTPRequestHandler):
    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _auth(self) -> bool:
        token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if token in _admin_tokens and time.time() < _admin_tokens[token]:
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return None

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionError:
            pass
        except Exception as e:
            try:
                from src.config import log
                log.warning(f"Admin handler error: {e}")
            except:
                pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def _path(self):
        return self.path.split('?')[0].rstrip('/') or '/'

    def do_GET(self):
        try:
            path = self._path()
            if path == "/admin":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(_PAGE_HTML.encode("utf-8"))
                return

            if self.path.startswith("/api/admin/accounts"):
                if not self._auth():
                    return
                from src.config import ACCOUNTS, get_account
                name = self.path.replace("/api/admin/accounts", "").strip("/")
                if name:
                    acc = get_account(name)
                    if not acc:
                        self._json(404, {"error": "not found"})
                        return
                    self._json(200, acc)
                else:
                    items = []
                    for n, a in ACCOUNTS.items():
                        items.append({
                            "name": n,
                            "gitcode_api_token": "YES" if a.get("gitcode_api_token") else "NO",
                            "feishu_app_id": "YES" if a.get("feishu_app_id") else "NO",
                            "huawei_cloud_ak": "YES" if a.get("huawei_cloud_ak") else "NO",
                        })
                    self._json(200, items)
                return
        except Exception as e:
            try:
                from src.config import log
                log.warning(f"Admin GET error: {e}")
            except:
                pass
            self._json(500, {"error": str(e)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        try:
            if self._path() == "/api/admin/login":
                data = self._read_body()
                if data is None:
                    return
                if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
                    token = hashlib.sha256(f"{ADMIN_USER}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
                    _admin_tokens[token] = time.time() + 86400
                    self._json(200, {"token": token})
                else:
                    self._json(401, {"error": "invalid credentials"})
                return

            if self.path.startswith("/api/admin/accounts/") and self.path.endswith("/test"):
                if not self._auth():
                    return
                from src.config import get_account
                name = self.path.replace("/api/admin/accounts/", "").replace("/test", "").split("/")[0]
                acc = get_account(name)
                if not acc:
                    self._json(404, {"error": "not found"})
                    return
                gtoken = acc.get("gitcode_api_token", "")
                if not gtoken:
                    self._json(400, {"error": "No GitCode token configured"})
                    return
                try:
                    req = urllib.request.Request("https://api.gitcode.com/api/v5/user",
                                                 headers={"Authorization": f"Bearer {gtoken}"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    user = json.loads(resp.read())
                    self._json(200, {"status": "ok", "login": user.get("login", "?")})
                except Exception as e:
                    self._json(502, {"error": f"GitCode API error: {e}"})
                return
        except Exception as e:
            try:
                from src.config import log
                log.warning(f"Admin POST error: {e}")
            except:
                pass
            self._json(500, {"error": str(e)})
            return
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        if self.path.startswith("/api/admin/accounts/"):
            if not self._auth():
                return
            from src.config import ACCOUNTS, get_account, save_accounts
            name = self.path.replace("/api/admin/accounts/", "").split("/")[0]
            data = self._read_body()
            if data is None:
                return
            items = [a for a in ACCOUNTS.values()]
            existing = next((a for a in items if a["name"] == name), None)
            if existing:
                for k, v in data.items():
                    if v is not None:
                        existing[k] = v
            else:
                new = {"name": name, "gitcode_api_token": "", "gitcode_webhook_token": "",
                       "feishu_app_id": "", "feishu_app_secret": "",
                       "huawei_cloud_ak": "", "huawei_cloud_sk": "", "bot_usernames": []}
                new.update(data)
                items.append(new)
            save_accounts(items)
            self._json(200, get_account(name))
            return
        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/api/admin/accounts/"):
            if not self._auth():
                return
            from src.config import ACCOUNTS, save_accounts
            name = self.path.replace("/api/admin/accounts/", "").split("/")[0]
            if name == "default":
                self._json(400, {"error": "cannot delete default account"})
                return
            items = [a for a in ACCOUNTS.values() if a["name"] != name]
            if len(items) == len(ACCOUNTS):
                self._json(404, {"error": "not found"})
                return
            save_accounts(items)
            self._json(200, {"status": "deleted"})
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def start_admin_server():
    from src.config import log
    import time
    while True:
        try:
            server = HTTPServer(("", 80), AdminHandler)
            server.socket.setsockopt(1, 2, 1)
            log.info("Admin server started on :80")
            server.serve_forever()
        except OSError as e:
            log.warning(f"Admin server failed on :80: {e}, retrying in 5s")
            time.sleep(5)
        except Exception as e:
            log.warning(f"Admin server error: {e}, restarting in 5s")
            time.sleep(5)
