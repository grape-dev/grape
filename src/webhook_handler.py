"""HTTP Webhook 处理器 + 管理台 API"""
import json, os, time, hashlib, urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from src.config import log, SKILLS_DIR, GITCODE_WEBHOOK_TOKEN, resolve_account_by_webhook_token, ACCOUNTS, get_account, save_accounts
from src.event_processor import _submit_event, process_event

# ====== 管理台认证 ======
ADMIN_USER = "developer"
ADMIN_PASS = "YOUR_SSH_PASSWORD"
_admin_tokens: dict[str, float] = {}

# ====== 管理台页面（从文件读取） ======
_ADMIN_HTML = ""
_ADMIN_HTML_PATH = os.path.join(os.path.dirname(__file__), "admin", "page.html")
if os.path.exists(_ADMIN_HTML_PATH):
    with open(_ADMIN_HTML_PATH, encoding="utf-8") as _f:
        _ADMIN_HTML = _f.read()



class Handler(BaseHTTPRequestHandler):
    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _admin_auth(self) -> bool:
        token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if token in _admin_tokens and time.time() < _admin_tokens[token]:
            return True
        self._json_response(401, {"error": "unauthorized"})
        return False

    def _admin_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid JSON"})
            return None

    # -------- POST routes --------
    def do_POST(self):
        client_ip = self.client_address[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""

        # —— Webhook ——
        if self.path == "/webhook/gitcode" and GITCODE_WEBHOOK_TOKEN:
            token = self.headers.get("X-GitCode-Token", "")
            if token != GITCODE_WEBHOOK_TOKEN:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"invalid webhook token")
                log.warning(f"Rejected webhook: invalid token (got='{token[:20]}', expected match)")
                return
            resolved = resolve_account_by_webhook_token(token)
            account = resolved or "default"
            log.info(f"Webhook from {client_ip}, path={self.path}, length={length}, account={account}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            _submit_event(process_event, body, self.headers, account)
            return

        # —— Admin Login ——
        if self.path == "/api/admin/login":
            try:
                data = json.loads(body)
                if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
                    token = hashlib.sha256(f"{ADMIN_USER}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
                    _admin_tokens[token] = time.time() + 86400
                    self._json_response(200, {"token": token})
                else:
                    self._json_response(401, {"error": "invalid credentials"})
            except json.JSONDecodeError:
                self._json_response(400, {"error": "invalid JSON"})
            return

        # —— Admin Test Connection ——
        if self.path.startswith("/api/admin/accounts/") and self.path.endswith("/test"):
            if not self._admin_auth():
                return
            parts = self.path.replace("/api/admin/accounts/", "").replace("/test", "").split("/")
            name = parts[0]
            acc = get_account(name)
            if not acc:
                self._json_response(404, {"error": "not found"})
                return
            gtoken = acc.get("gitcode_api_token", "")
            if not gtoken:
                self._json_response(400, {"error": "No GitCode token configured"})
                return
            try:
                req = urllib.request.Request("https://api.gitcode.com/api/v5/user",
                                             headers={"Authorization": f"Bearer {gtoken}"})
                resp = urllib.request.urlopen(req, timeout=10)
                user = json.loads(resp.read())
                self._json_response(200, {"status": "ok", "login": user.get("login", "?")})
            except Exception as e:
                self._json_response(502, {"error": f"GitCode API error: {e}"})
            return

        # —— Account Save (PUT via POST override) ——
        if self.path.startswith("/api/admin/accounts/"):
            if not self._admin_auth():
                return
            name = self.path.replace("/api/admin/accounts/", "").split("/")[0]
            data = self._admin_body()
            if data is None:
                return
            self._admin_save_account(name, data)
            return

        # —— Else: webhook fallback ——
        log.info(f"Webhook from {client_ip}, path={self.path}, length={length}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        _submit_event(process_event, body, self.headers, "default")

    # -------- GET routes --------
    def do_GET(self):
        # —— Admin HTML page ——
        if self.path == "/admin/" or self.path == "/admin":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_ADMIN_HTML.encode("utf-8"))
            return

        # —— Admin API: list / get ——
        if self.path.startswith("/api/admin/accounts"):
            if not self._admin_auth():
                return
            name = self.path.replace("/api/admin/accounts", "").strip("/")
            if name:
                acc = get_account(name)
                if not acc:
                    self._json_response(404, {"error": "not found"})
                    return
                self._json_response(200, acc)
            else:
                items = []
                for n, a in ACCOUNTS.items():
                    items.append({
                        "name": n,
                        "gitcode_api_token": "YES" if a.get("gitcode_api_token") else "NO",
                        "feishu_app_id": "YES" if a.get("feishu_app_id") else "NO",
                        "huawei_cloud_ak": "YES" if a.get("huawei_cloud_ak") else "NO",
                    })
                self._json_response(200, items)
            return

        # —— Health ——
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            skills = [d for d in os.listdir(SKILLS_DIR) if os.path.isdir(os.path.join(SKILLS_DIR, d))]
            self.wfile.write(json.dumps({"status": "ok", "agents": ["opencode"], "skills": skills}).encode())
            return

        # —— Sessions ——
        if self.path == "/api/sessions":
            from src.session_manager import db
            with __import__("src.session_manager", fromlist=["db_lock"]).db_lock:
                c = db.cursor()
                c.execute("SELECT id, repo_key, source, repository, status, event_history, oc_session_id, created_at, last_active_at FROM sessions ORDER BY last_active_at DESC")
                rows = c.fetchall()
            sessions = []
            for r in rows:
                sessions.append({
                    "id": r[0], "repo_key": r[1], "source": r[2], "repository": r[3], "status": r[4],
                    "event_history": json.loads(r[5] or "[]"), "oc_session_id": r[6],
                    "created_at": r[7], "last_active_at": r[8]
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(sessions, ensure_ascii=False).encode())
            return

        # —— User mapping ——
        if self.path.startswith("/api/user-mapping"):
            params = parse_qs(urlparse(self.path).query)
            login = params.get("login", [None])[0]
            repo_key = params.get("repo_key", [None])[0]
            if repo_key and not repo_key.startswith("gitcode:"):
                repo_key = f"gitcode:{repo_key}"
            if not login or not repo_key:
                self._json_response(400, {"error": "missing login or repo_key"})
                return
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT feishu_open_id, feishu_name FROM feishu_users WHERE repo_key=? AND gitcode_user=?", (repo_key, login))
                row = c.fetchone()
            if row:
                self._json_response(200, {"login": login, "open_id": row[0], "nick_name": row[1]})
                return
            from src.im.feishu import get_chat_id_for_repo, sync_repo_feishu_mapping
            chat_id = get_chat_id_for_repo(repo_key)
            if chat_id:
                sync_repo_feishu_mapping(repo_key, chat_id)
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT feishu_open_id, feishu_name FROM feishu_users WHERE repo_key=? AND gitcode_user=?", (repo_key, login))
                    row = c.fetchone()
                if row:
                    self._json_response(200, {"login": login, "open_id": row[0], "nick_name": row[1]})
                    return
            self._json_response(404, {"error": "not_found", "login": login})
            return

        self.send_response(404)
        self.end_headers()

    # -------- Admin helpers --------
    def _admin_save_account(self, name: str, data: dict):
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
        self._json_response(200, get_account(name))

    # -------- PUT (for admin save) --------
    def do_PUT(self):
        if self.path.startswith("/api/admin/accounts/"):
            if not self._admin_auth():
                return
            name = self.path.replace("/api/admin/accounts/", "").split("/")[0]
            data = self._admin_body()
            if data is None:
                return
            self._admin_save_account(name, data)
            return
        self.send_response(404)
        self.end_headers()

    # -------- DELETE --------
    def do_DELETE(self):
        if self.path.startswith("/api/admin/accounts/"):
            if not self._admin_auth():
                return
            name = self.path.replace("/api/admin/accounts/", "").split("/")[0]
            if name == "default":
                self._json_response(400, {"error": "cannot delete default account"})
                return
            items = [a for a in ACCOUNTS.values() if a["name"] != name]
            if len(items) == len(ACCOUNTS):
                self._json_response(404, {"error": "not found"})
                return
            save_accounts(items)
            self._json_response(200, {"status": "deleted"})
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass
