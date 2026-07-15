"""HTTP Webhook 处理器"""
import json, os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from src.config import log, SKILLS_DIR, GITCODE_WEBHOOK_TOKEN, resolve_account_by_webhook_token
from src.event_processor import _submit_event, process_event


class Handler(BaseHTTPRequestHandler):
    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_POST(self):
        client_ip = self.client_address[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""

        account = "default"
        if self.path == "/webhook/gitcode" and GITCODE_WEBHOOK_TOKEN:
            token = self.headers.get("X-GitCode-Token", "")
            if token != GITCODE_WEBHOOK_TOKEN:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"invalid webhook token")
                log.warning(f"Rejected webhook: invalid token (got='{token[:20]}', expected match)")
                return
            resolved = resolve_account_by_webhook_token(token)
            if resolved:
                account = resolved
                log.info(f"Resolved account: {account}")

        log.info(f"Webhook from {client_ip}, path={self.path}, length={length}, account={account}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        _submit_event(process_event, body, self.headers, account)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            skills = [d for d in os.listdir(SKILLS_DIR) if os.path.isdir(os.path.join(SKILLS_DIR, d))]
            self.wfile.write(json.dumps({"status": "ok", "agents": ["opencode"], "skills": skills}).encode())
        elif self.path == "/api/sessions":
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
        elif self.path.startswith("/api/user-mapping"):
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
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass
