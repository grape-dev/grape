"""管理台 HTTP 服务（端口 80）：纯服务端渲染 + 内联 JS"""
import json, os, time, hashlib, urllib.request, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor


class PooledHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    pool = ThreadPoolExecutor(max_workers=4)

    def process_request(self, request, client_address):
        self.pool.submit(self.process_request_thread, request, client_address)


ADMIN_USER = "developer"
ADMIN_PASS = "YOUR_SSH_PASSWORD"


class AdminHandler(BaseHTTPRequestHandler):
    _tokens = {}

    def _send_html(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _valid_token(self, token):
        return bool(token and token in type(self)._tokens and time.time() < type(self)._tokens[token])

    def _get_param(self, key, default=""):
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        return qs.get(key, [default])[0]

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return json.loads(body.decode("utf-8", errors="replace"))
        return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}

    def _path(self):
        return self.path.split('?')[0].rstrip('/') or '/'

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                from src.config import log
                log.warning(f"Admin error: {e}")
            except:
                pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    # ====== GET ======
    def do_GET(self):
        path = self._path()
        token = self._get_param("token")

        # Login page
        if path in ("/admin", "/admin/", "/"):
            if self._valid_token(token):
                self._send_html(_render_panel(token, self._get_param("select", "")))
            else:
                self._send_html(_render_login(self._get_param("error", "")))
            return

        # Logout — invalidate token
        if path == "/admin/logout":
            if token:
                type(self)._tokens.pop(token, None)
            self._send_html(_render_login(""))
            return

        # Delete account
        if path == "/admin/delete":
            if self._valid_token(token):
                name = self._get_param("name", "")
                if name and name != "default":
                    from src.config import ACCOUNTS, save_accounts
                    items = [a for a in ACCOUNTS.values() if a.get("name") != name]
                    save_accounts(items)
            self._send_html(_render_panel(token, "", "已删除 " + self._get_param("name", "")))
            return

        # Test connection
        if path == "/admin/test":
            msg = ""
            if self._valid_token(token):
                name = self._get_param("name", "")
                from src.config import get_account
                acc = get_account(name)
                if acc and acc.get("gitcode_api_token"):
                    try:
                        req = urllib.request.Request("https://api.gitcode.com/api/v5/user",
                                                     headers={"Authorization": f"Bearer {acc['gitcode_api_token']}"})
                        resp = urllib.request.urlopen(req, timeout=10)
                        user = json.loads(resp.read())
                        msg = f"连接成功: {user.get('login', '?')}"
                    except Exception as e:
                        msg = f"连接失败: {e}"
                else:
                    msg = "未配置 GitCode Token"
            self._send_html(_render_panel(token, self._get_param("name", ""), msg))
            return

        # API: get account detail (for fetch)
        if path.startswith("/api/admin/accounts/"):
            if not self._valid_token(self.headers.get("Authorization", "").replace("Bearer ", "").strip()):
                self._send_json(401, {"error": "unauthorized"})
                return
            from src.config import get_account
            name = path.replace("/api/admin/accounts/", "").strip("/")
            acc = get_account(name)
            if not acc:
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, _account_detail(acc))
            return

        # API: list accounts
        if path == "/api/admin/accounts":
            if not self._valid_token(self.headers.get("Authorization", "").replace("Bearer ", "").strip()):
                self._send_json(401, {"error": "unauthorized"})
                return
            from src.config import ACCOUNTS
            items = [_account_summary(a) for a in ACCOUNTS.values()]
            self._send_json(200, items)
            return

        self.send_response(404)
        self.end_headers()

    # ====== POST ======
    def do_POST(self):
        path = self._path()

        # Login
        if path == "/admin/login":
            form = self._read_form()
            if form.get("username") == ADMIN_USER and form.get("password") == ADMIN_PASS:
                token = hashlib.sha256(f"{ADMIN_USER}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
                type(self)._tokens[token] = time.time() + 86400
                # Return panel page directly — NO redirect
                self._send_html(_render_panel(token, ""))
            else:
                self._send_html(_render_login("用户名或密码错误"))
            return

        # Save account
        if path == "/admin/save":
            token = self._get_param("token")
            if not self._valid_token(token):
                self._send_html(_render_login("会话过期"))
                return
            form = self._read_form()
            name = form.pop("name", "")
            if not name:
                self._send_html(_render_panel(token, "", "名称不能为空"))
                return
            from src.config import ACCOUNTS, save_accounts
            items = list(ACCOUNTS.values())
            existing = next((a for a in items if a.get("name") == name), None)
            if existing:
                for k, v in form.items():
                    existing[k] = v
            else:
                new = {"name": name, "gitcode_api_token": "", "gitcode_webhook_token": "",
                       "feishu_app_id": "", "feishu_app_secret": "",
                       "huawei_cloud_ak": "", "huawei_cloud_sk": "",
                       "CODEARTS_CLI_AK": "", "CODEARTS_CLI_SK": "", "bot_usernames": []}
                new.update(form)
                items.append(new)
            if "bot_usernames" in form and isinstance(form["bot_usernames"], str):
                for a in items:
                    if a.get("name") == name:
                        a["bot_usernames"] = [s.strip() for s in form["bot_usernames"].split(",") if s.strip()]
            save_accounts(items)
            self._send_html(_render_panel(token, name, "保存成功"))
            return

    def log_message(self, fmt, *args):
        pass


# ====== HTML Rendering ======

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333;min-height:100vh}
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;background:linear-gradient(135deg,#667eea,#764ba2)}
.login-card{background:#fff;border-radius:12px;padding:40px;width:380px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.15)}
.login-card h1{text-align:center;margin-bottom:4px;font-size:22px}
.login-card .sub{text-align:center;color:#888;font-size:13px;margin-bottom:20px}
.login-card input{width:100%;padding:12px 16px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;margin-bottom:12px;outline:none}
.login-card input:focus{border-color:#667eea}
.login-card button{width:100%;padding:12px;background:#667eea;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer}
.login-card .err{color:#e74c3c;font-size:13px;text-align:center;margin-top:8px}
.header{background:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:24px}
.header h2{font-size:16px;font-weight:600}
.header a{font-size:13px;color:#e74c3c;cursor:pointer;text-decoration:none}
.wrap{display:flex;max-width:1200px;margin:0 auto;padding:0 24px 24px;gap:24px}
.sidebar{width:260px;flex-shrink:0}
.sidebar h3{font-size:13px;color:#888;padding:0 12px;margin-bottom:8px}
.list{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.item{padding:12px 16px;cursor:pointer;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;justify-content:space-between;text-decoration:none;color:#333}
.item:hover{background:#f8f9ff}
.item.active{background:#eef0ff;font-weight:600}
.dots{display:flex;gap:3px}
.dot{width:7px;height:7px;border-radius:50%;background:#ccc}
.dot.ok{background:#27ae60}
.add-link{padding:12px;text-align:center;color:#667eea;cursor:pointer;font-size:14px;border-top:1px solid #f0f0f0;display:block;text-decoration:none}
.main{flex:1;background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.06);padding:24px}
.main h3{font-size:16px;padding-bottom:12px;border-bottom:1px solid #f0f0f0;margin-bottom:16px}
.group{margin-bottom:18px}
.group h4{font-size:13px;color:#888;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #f5f5f5}
.row{margin-bottom:10px}
.row label{display:block;font-size:12px;color:#666;margin-bottom:2px}
.row .iw{display:flex;gap:6px}
.row input{flex:1;padding:9px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px;outline:none}
.row input:focus{border-color:#667eea}
.row .eye{background:#f5f5f5;border:1px solid #d9d9d9;border-radius:6px;padding:0 10px;cursor:pointer;font-size:14px;color:#888;display:flex;align-items:center;user-select:none}
.acts{display:flex;gap:8px;margin-top:16px;padding-top:16px;border-top:1px solid #f0f0f0}
.acts button,.acts a{padding:10px 20px;border:none;border-radius:6px;font-size:13px;cursor:pointer;text-decoration:none}
.btn-p{background:#667eea;color:#fff}
.btn-s{background:#e8e8e8;color:#333}
.btn-d{background:#e74c3c;color:#fff}
.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;color:#fff;font-size:13px;z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
.toast.ok{background:#27ae60}
.toast.err{background:#e74c3c}
@media(max-width:767px){.wrap{flex-direction:column}.sidebar{width:100%}}
"""


def _render_login(error=""):
    err_html = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DevOps Bot 管理台</title>
<style>{_CSS}</style></head><body>
<div class="login-wrap"><div class="login-card">
<h1>DevOps Bot</h1><p class="sub">管理台登录</p>
<form method="POST" action="/admin/login">
<input name="username" placeholder="用户名" required>
<input name="password" type="password" placeholder="密码" required>
<button type="submit">登录</button>
</form>
{err_html}
</div></div>
</body></html>"""


def _render_panel(token, select_name="", message=""):
    from src.config import ACCOUNTS, get_account

    # Account list
    items_html = ""
    for n, a in ACCOUNTS.items():
        active = " active" if n == select_name else ""
        gc = " ok" if a.get("gitcode_api_token") else ""
        fs = " ok" if a.get("feishu_app_id") else ""
        hw = " ok" if a.get("huawei_cloud_ak") else ""
        items_html += f'<a class="item{active}" href="/admin?token={token}&select={n}"><span>{n}</span><span class="dots"><span class="dot{gc}"></span><span class="dot{fs}"></span><span class="dot{hw}"></span></span></a>\n'

    # Account detail form
    if select_name:
        acc = get_account(select_name) or {}
        bu = ",".join(acc.get("bot_usernames", [])) if isinstance(acc.get("bot_usernames"), list) else str(acc.get("bot_usernames", ""))
        form_html = f'<h3>{select_name}</h3>'
        form_html += '<form method="POST" action="/admin/save?token=' + token + '">'
        form_html += f'<input type="hidden" name="name" value="{select_name}">'

        form_html += '<div class="group"><h4>基础信息</h4>'
        form_html += f'<div class="row"><label>账户名称</label><input value="{select_name}" disabled></div>'
        form_html += f'<div class="row"><label>Bot 用户名（逗号分隔）</label><input name="bot_usernames" value="{bu}"></div></div>'

        groups = [
            ("GitCode", [("gitcode_api_token", "API Token"), ("gitcode_webhook_token", "Webhook Token")]),
            ("飞书", [("feishu_app_id", "App ID"), ("feishu_app_secret", "App Secret")]),
            ("华为云", [("huawei_cloud_ak", "Access Key"), ("huawei_cloud_sk", "Secret Key")]),
            ("CodeArts", [("CODEARTS_CLI_AK", "CLI Access Key"), ("CODEARTS_CLI_SK", "CLI Secret Key")]),
        ]
        for gname, fields in groups:
            form_html += f'<div class="group"><h4>{gname}</h4>'
            for key, label in fields:
                val = acc.get(key, "")
                fid = key + "_" + str(hash(key) % 10000)
                form_html += f'<div class="row"><label>{label}</label><div class="iw">'
                form_html += f'<input id="{fid}" type="password" name="{key}" value="{val}">'
                form_html += f'<span class="eye" onclick="var e=document.getElementById(\'{fid}\');e.type=e.type==\'password\'?\'text\':\'password\'">👁</span>'
                form_html += '</div></div>'
            form_html += '</div>'

        form_html += '<div class="acts">'
        form_html += '<button class="btn-p" type="submit">保存</button>'
        form_html += f'<a class="btn-s" href="/admin/test?token={token}&name={select_name}">测试连接</a>'
        if select_name != "default":
            form_html += f'<a class="btn-d" href="/admin/delete?token={token}&name={select_name}" onclick="return confirm(\'确定删除？\')">删除</a>'
        form_html += '</div></form>'
    else:
        form_html = '<h3>请选择左侧账户</h3>'

    # Toast
    toast_html = ""
    if message:
        toast_html = f'<div class="toast show ok">{message}</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DevOps Bot 管理台</title>
<style>{_CSS}</style></head><body>
<div class="header"><h2>DevOps Bot 管理台</h2><a href="/admin/logout?token={token}">退出</a></div>
<div class="wrap">
  <div class="sidebar"><h3>账户列表</h3><div class="list">
    {items_html}
    <a class="add-link" href="/admin?token={token}&select=__new__">+ 新增</a>
  </div></div>
  <div class="main">{form_html}</div>
</div>
{toast_html}
</body></html>"""


def _account_summary(a):
    return {
        "name": a.get("name", ""),
        "gitcode_api_token": "YES" if a.get("gitcode_api_token") else "NO",
        "feishu_app_id": "YES" if a.get("feishu_app_id") else "NO",
        "huawei_cloud_ak": "YES" if a.get("huawei_cloud_ak") else "NO",
    }

def _account_detail(a):
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
    AdminHandler.timeout = 30
    while True:
        try:
            server = PooledHTTPServer(("", 80), AdminHandler)
            server.socket.setsockopt(1, 2, 1)
            log.info("Admin server started on :80 (pooled, 4 workers, 30s timeout)")
            server.serve_forever()
        except OSError as e:
            log.warning(f"Admin server failed on :80: {e}, retrying in 5s")
            time.sleep(5)
        except Exception as e:
            log.warning(f"Admin server error: {e}, restarting in 5s")
            time.sleep(5)
