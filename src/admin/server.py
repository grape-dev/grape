"""管理台 HTTP 服务（端口 80）：登录、OAuth、账户 CRUD、测试连接"""
import json, os, time, hashlib, urllib.request, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

_ADMIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_PATH = os.path.join(_ADMIN_DIR, "page.html")
ADMIN_USER = "developer"
ADMIN_PASS = "YOUR_SSH_PASSWORD"
_admin_tokens: dict[str, float] = {}

_GITCODE_OAUTH_CLIENT_ID = ""
_GITCODE_OAUTH_CLIENT_SECRET = ""
_FEISHU_OAUTH_APP_ID = ""

_PAGE_HTML = ""
if os.path.exists(_PAGE_PATH):
    with open(_PAGE_PATH, encoding="utf-8") as f:
        _PAGE_HTML = f.read()

_OAUTH_LOADED = False

def _load_oauth_config():
    global _GITCODE_OAUTH_CLIENT_ID, _GITCODE_OAUTH_CLIENT_SECRET, _FEISHU_OAUTH_APP_ID, _OAUTH_LOADED
    if _OAUTH_LOADED:
        return
    try:
        from src.config import get_account
        acc = get_account("default")
        _GITCODE_OAUTH_CLIENT_ID = acc.get("gitcode_oauth_client_id", "") or ""
        _GITCODE_OAUTH_CLIENT_SECRET = acc.get("gitcode_oauth_client_secret", "") or ""
        _FEISHU_OAUTH_APP_ID = acc.get("feishu_app_id", "") or ""
    except:
        pass
    _OAUTH_LOADED = True


class AdminHandler(BaseHTTPRequestHandler):
    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _serve(self, token=""):
        html = _PAGE_HTML
        from src.config import ACCOUNTS
        items = []
        for n, a in ACCOUNTS.items():
            items.append(_account_summary(a))
        embed = json.dumps(items, ensure_ascii=False)
        html = html.replace("__ACCOUNTS_PLACEHOLDER__", embed or "[]")
        if token and token in _admin_tokens and time.time() < _admin_tokens[token]:
            html = html.replace("__TOKEN_PLACEHOLDER__", json.dumps(token))
        else:
            html = html.replace("__TOKEN_PLACEHOLDER__", "\"\"")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_GET(self):
        try:
            path = self._path()
            if path in ("/admin", "/admin/panel"):
                token = ""
                if "?" in self.path:
                    q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                    token = q.get("token", [""])[0]
                self._serve(token)
                return

            # OAuth
            if path == "/api/admin/oauth/gitcode":
                _load_oauth_config()
                if not _GITCODE_OAUTH_CLIENT_ID:
                    self._json(400, {"error": "GitCode OAuth not configured"})
                    return
                redirect_uri = f"http://{self.headers.get('Host','localhost')}/api/admin/oauth/callback"
                url = (f"https://gitcode.com/login/oauth/authorize"
                       f"?client_id={_GITCODE_OAUTH_CLIENT_ID}"
                       f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
                       f"&response_type=code&scope=api")
                self.send_response(302)
                self.send_header("Location", url)
                self.end_headers()
                return

            if path == "/api/admin/oauth/callback":
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                code = qs.get("code", [""])[0]
                if not code:
                    self._json(400, {"error": "missing code"})
                    return
                _load_oauth_config()
                token_url = "https://gitcode.com/login/oauth/access_token"
                data = urllib.parse.urlencode({
                    "client_id": _GITCODE_OAUTH_CLIENT_ID,
                    "client_secret": _GITCODE_OAUTH_CLIENT_SECRET,
                    "code": code, "grant_type": "authorization_code",
                }).encode()
                try:
                    req = urllib.request.Request(token_url, data=data, headers={"Accept": "application/json"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    result = json.loads(resp.read())
                    access_token = result.get("access_token", "")
                    if not access_token:
                        self._json(502, {"error": "OAuth token exchange failed"})
                        return
                    user_req = urllib.request.Request(
                        "https://api.gitcode.com/api/v5/user",
                        headers={"Authorization": f"Bearer {access_token}"})
                    user_resp = urllib.request.urlopen(user_req, timeout=10)
                    user_info = json.loads(user_resp.read())
                    login = user_info.get("login", "")
                    from src.config import log
                    log.info(f"Admin OAuth login: {login}")
                    token = hashlib.sha256(f"oauth:{login}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
                    _admin_tokens[token] = time.time() + 86400
                    self.send_response(302)
                    self.send_header("Location", f"/admin/panel?token={token}")
                    self.end_headers()
                except Exception as e:
                    self._json(502, {"error": f"OAuth failed: {e}"})
                return

            # Account APIs
            if path.startswith("/api/admin/accounts"):
                if not self._auth():
                    return
                from src.config import ACCOUNTS, get_account
                name = path.replace("/api/admin/accounts", "").strip("/")
                if name:
                    acc = get_account(name)
                    if not acc:
                        self._json(404, {"error": "not found"})
                        return
                    self._json(200, _account_detail(acc))
                else:
                    items = [_account_summary(a) for n, a in ACCOUNTS.items()]
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
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""
                ctype = self.headers.get("Content-Type", "")
                username = password = ""
                if "application/json" in ctype:
                    data = json.loads(body.decode("utf-8", errors="replace"))
                    username = data.get("username", "")
                    password = data.get("password", "")
                else:
                    params = urllib.parse.parse_qs(body.decode())
                    username = params.get("username", [""])[0]
                    password = params.get("password", [""])[0]
                from src.config import log
                if username == ADMIN_USER and password == ADMIN_PASS:
                    token = hashlib.sha256(f"{ADMIN_USER}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
                    _admin_tokens[token] = time.time() + 86400
                    log.info(f"Admin login success: {username}")
                    if "application/json" in ctype:
                        self._json(200, {"token": token})
                    else:
                        self.send_response(302)
                        self.send_header("Location", f"/admin/panel?token={token}")
                        self.end_headers()
                else:
                    log.info(f"Admin login failed: {username}")
                    if "application/json" in ctype:
                        self._json(401, {"error": "invalid credentials"})
                    else:
                        self.send_response(302)
                        self.send_header("Location", "/admin/?error=1")
                        self.end_headers()
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
                       "huawei_cloud_ak": "", "huawei_cloud_sk": "",
                       "CODEARTS_CLI_AK": "", "CODEARTS_CLI_SK": "",
                       "bot_usernames": []}
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


def _account_summary(a: dict) -> dict:
    return {
        "name": a.get("name", ""),
        "gitcode_api_token": "YES" if a.get("gitcode_api_token") else "NO",
        "feishu_app_id": "YES" if a.get("feishu_app_id") else "NO",
        "huawei_cloud_ak": "YES" if a.get("huawei_cloud_ak") else "NO",
    }

def _account_detail(a: dict) -> dict:
    return {
        "name": a.get("name", ""),
        "gitcode_api_token": a.get("gitcode_api_token", ""),
        "gitcode_webhook_token": a.get("gitcode_webhook_token", ""),
        "feishu_app_id": a.get("feishu_app_id", ""),
        "feishu_app_secret": a.get("feishu_app_secret", ""),
        "huawei_cloud_ak": a.get("huawei_cloud_ak", ""),
        "huawei_cloud_sk": a.get("huawei_cloud_sk", ""),
        "CODEARTS_CLI_AK": a.get("CODEARTS_CLI_AK", ""),
        "CODEARTS_CLI_SK": a.get("CODEARTS_CLI_SK", ""),
        "bot_usernames": a.get("bot_usernames", []),
    }


def start_admin_server():
    from src.config import log
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
