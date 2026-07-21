"""管理台 HTTP 服务（端口 80）：多用户管理台"""
import json, os, time, hashlib, urllib.request, urllib.parse, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor
from src.user import db as user_db


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

        # 普通用户 → 登录页 / 面板
        if path in ("", "/"):
            uid = user_db.validate_token(token or "")
            if uid:
                user = user_db.get_user(uid)
                if user:
                    self._send_html(_render_user_panel(token))
                    return
            self._send_html(_render_user_login(self._get_param("error", "")))
            return

        # 普通用户 → 面板（通过 token 直接访问）
        if path == "/panel":
            uid = user_db.validate_token(token or "")
            if uid and user_db.get_user(uid):
                self._send_html(_render_user_panel(token))
                return
            self._send_html(_render_user_login("会话过期"))
            return

        # 普通用户 → 退出
        if path == "/logout":
            if token:
                user_db.clear_token(token)
            self._send_html(_render_user_login(""))
            return

        # 管理员路由（保持不变）
        if path.startswith("/admin") or path == "/admin":
            admin_path = path[len("/admin"):] or "/"
            if not admin_path.startswith("/"):
                admin_path = "/" + admin_path
        else:
            admin_path = None

        if admin_path in ("", "/"):
            if self._valid_token(token):
                self._send_html(_render_admin_panel(token, self._get_param("select", "")))
            else:
                self._send_html(_render_admin_login(self._get_param("error", "")))
            return

        # Admin Logout
        if admin_path == "/logout":
            if token:
                type(self)._tokens.pop(token, None)
            self._send_html(_render_admin_login(""))
            return

        # Admin: delete account
        if admin_path == "/delete":
            if self._valid_token(token):
                name = self._get_param("name", "")
                if name and name != "default":
                    from src.config import ACCOUNTS, save_accounts
                    items = [a for a in ACCOUNTS.values() if a.get("name") != name]
                    save_accounts(items)
            self._send_html(_render_admin_panel(token, "", "已删除 " + self._get_param("name", "")))
            return

        # Admin: test connection
        if admin_path == "/test":
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
            self._send_html(_render_admin_panel(token, self._get_param("name", ""), msg))
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
        form = self._read_form()

        # 普通用户登录
        if path in ("", "/", "/login"):
            user = user_db.verify_user(form.get("username", ""), form.get("password", ""))
            if user:
                token = user_db.create_session_token(user["id"])
                self._send_html(_render_user_panel(token))
            else:
                self._send_html(_render_user_login("用户名或密码错误"))
            return

        # 普通用户 → 保存凭据
        if path == "/user/cred":
            token = self._get_param("token")
            uid = user_db.validate_token(token)
            if not uid:
                self._send_html(_render_user_login("会话过期"))
                return
            provider = form.get("provider", "")
            try:
                config = json.loads(form.get("config_json", "{}"))
            except json.JSONDecodeError:
                config = {}
            user_db.set_credential(uid, provider, config)
            self._send_html(_render_user_panel(token))
            return

        # 普通用户 → 添加云主机
        if path == "/user/vm":
            token = self._get_param("token")
            uid = user_db.validate_token(token)
            if not uid:
                self._send_html(_render_user_login("会话过期"))
                return
            vm_id = user_db.add_vm(uid, form.get("name", ""), form.get("host", ""),
                                    form.get("ssh_user", "root"), form.get("ssh_password", ""),
                                    int(form.get("port", 22)))
            self._send_html(_render_user_panel(token))
            return

        # 普通用户 → 上传自定义技能
        if path == "/user/skill":
            token = self._get_param("token")
            uid = user_db.validate_token(token)
            if not uid:
                self._send_html(_render_user_login("会话过期"))
                return
            from src.user import validator
            skill_name = form.get("skill_name", "")
            content = form.get("content", "")
            if not re.match(r"^[a-z0-9-]+$", skill_name):
                self._send_html(_render_user_panel(token))
                return
            user_db.create_skill(uid, skill_name)
            user_db.save_skill_md(uid, skill_name, content)
            self._send_html(_render_user_panel(token))
            return

        # 普通用户 → 仓库技能映射
        if path == "/user/mapping":
            token = self._get_param("token")
            uid = user_db.validate_token(token)
            if not uid:
                self._send_html(_render_user_login("会话过期"))
                return
            user_db.set_repo_mapping(uid, form.get("repo_key", ""), form.get("event_type", ""), form.get("skill_name", ""))
            self._send_html(_render_user_panel(token))
            return

        # 管理员登录
        if path == "/admin/login":
            if form.get("username") == ADMIN_USER and form.get("password") == ADMIN_PASS:
                token = hashlib.sha256(f"{ADMIN_USER}:{time.time()}:{os.urandom(8).hex()}".encode()).hexdigest()
                type(self)._tokens[token] = time.time() + 86400
                # Return panel page directly — NO redirect
                self._send_html(_render_admin_panel(token, ""))
            else:
                self._send_html(_render_admin_login("用户名或密码错误"))
            return

        # Save account
        if path == "/admin/save":
            token = self._get_param("token")
            if not self._valid_token(token):
                self._send_html(_render_admin_login("会话过期"))
                return
            name = form.pop("name", "")
            if not name:
                self._send_html(_render_admin_panel(token, "", "名称不能为空"))
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
            # 同步密码到 users 表（auth 用 hash，accounts.json 存明文供显示）
            pwd = form.get("password", "")
            if pwd:
                user = user_db.get_user_by_username(name)
                if user:
                    user_db.update_password(user["id"], pwd)
                else:
                    user_db.create_user(name, pwd, 0)
            save_accounts(items)
            self._send_html(_render_admin_panel(token, name, "保存成功"))
            return

    def log_message(self, fmt, *args):
        pass


# ====== Admin HTML Rendering ======

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
.login-card .link{text-align:center;margin-top:12px;font-size:12px;color:#888}
.login-card .link a{color:#667eea;text-decoration:none}
.header{background:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:24px}
.header h2{font-size:16px;font-weight:600}
.header a{font-size:13px;color:#e74c3c;cursor:pointer;text-decoration:none}
.header .right{font-size:13px;color:#666}
.wrap{display:flex;max-width:1200px;margin:0 auto;padding:0 24px 24px;gap:24px}
.sidebar{width:260px;flex-shrink:0}
.sidebar h3{font-size:13px;color:#888;padding:0 12px;margin-bottom:8px}
.list{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.item{padding:12px 16px;cursor:pointer;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;justify-content:space-between;text-decoration:none;color:#333}
.item:hover{background:#f8f9ff}
.item.active{background:#eef0ff;font-weight:600}
.item form input,.item form button{font-size:12px}
.dots{display:flex;gap:3px}
.dot{width:7px;height:7px;border-radius:50%;background:#ccc}
.dot.ok{background:#27ae60}
.add-link{padding:12px;text-align:center;color:#667eea;font-size:14px;border-top:1px solid #f0f0f0;display:block;text-decoration:none}
.main{flex:1;background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.06);padding:24px}
.main h3{font-size:16px;padding-bottom:12px;border-bottom:1px solid #f0f0f0;margin-bottom:16px}
.group{margin-bottom:18px}
.group h4{font-size:13px;color:#888;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #f5f5f5}
.row{margin-bottom:10px}
.row label{display:block;font-size:12px;color:#666;margin-bottom:2px}
.row .iw{display:flex;gap:6px}
.row input{flex:1;padding:9px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px;outline:none}
.row input:focus{border-color:#667eea}
select{width:100%;padding:9px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px;outline:none;background:#fff}
textarea{width:100%;padding:9px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px;outline:none;resize:vertical}
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


def _render_admin_login(error=""):
    err_html = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DevOps Bot 管理台</title>
<style>{_CSS}</style></head><body>
<div class="login-wrap"><div class="login-card">
<h1>DevOps Bot</h1><p class="sub">管理员登录</p>
<form method="POST" action="/admin/login">
<input name="username" placeholder="用户名" required>
<input name="password" type="password" placeholder="密码" required>
<button type="submit">登录</button>
</form>
<p class="link"><a href="/">返回用户登录</a></p>
{err_html}
</div></div>
</body></html>"""


def _render_admin_panel(token, select_name="", message=""):
    from src.config import ACCOUNTS, get_account

    # 侧边栏：账户列表 + 用户管理链接
    items_html = ""
    for n, a in ACCOUNTS.items():
        active = " active" if n == select_name else ""
        gc = " ok" if a.get("gitcode_api_token") else ""
        fs = " ok" if a.get("feishu_app_id") else ""
        hw = " ok" if a.get("huawei_cloud_ak") else ""
        items_html += f'<a class="item{active}" href="/admin?token={token}&select={n}"><span>{n}</span><span class="dots"><span class="dot{gc}"></span><span class="dot{fs}"></span><span class="dot{hw}"></span></span></a>\n'

    if select_name:
        # 账户编辑视图（已有的逻辑）
        acc = get_account(select_name) or {}
        bu = ",".join(acc.get("bot_usernames", [])) if isinstance(acc.get("bot_usernames"), list) else str(acc.get("bot_usernames", ""))
        form_html = f'<h3>{select_name}</h3>'
        form_html += '<form method="POST" action="/admin/save?token=' + token + '">'
        form_html += f'<input type="hidden" name="name" value="{select_name}">'
        form_html += '<div class="group"><h4>基础信息</h4>'
        form_html += f'<div class="row"><label>账户名称</label><input value="{select_name}" disabled></div>'
        form_html += f'<div class="row"><label>Bot 用户名</label><input name="bot_usernames" value="{bu}"></div></div>'
        form_html += '</div>'
        groups = [
            ("基础信息", [("password", "密码"), ("bot_usernames", "Bot 用户名")]),
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
                form_html += f'<span class="eye" onclick="var e=document.getElementById(\'{fid}\');e.type=e.type==\'password\'?\'text\':\'password\'">👁</span></div></div>'
            form_html += '</div>'
        form_html += '<div class="acts"><button class="btn-p" type="submit">保存</button>'
        form_html += f'<a class="btn-s" href="/admin/test?token={token}&name={select_name}">测试连接</a>'
        if select_name != "default":
            form_html += f'<a class="btn-d" href="/admin/delete?token={token}&name={select_name}" onclick="return confirm(\'确定删除？\')">删除</a>'
        form_html += '</div></form>'

    else:
        form_html = '<h3>请选择左侧账户</h3>'

    toast_html = f'<div class="toast show ok">{message}</div>' if message else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DevOps Bot 管理台</title>
<style>{_CSS}</style></head><body>
<div class="header"><h2>DevOps Bot 管理台</h2><a href="/admin/logout?token={token}">退出</a></div>
<div class="wrap">
  <div class="sidebar"><h3>账户列表</h3><div class="list">{items_html}<a class="add-link" href="/admin?token={token}&select=__new__">+ 新增账户</a></div></div>
  <div class="main">{form_html}</div>
</div>
{toast_html}
<script>var t=document.querySelector('.toast.show');if(t){{setTimeout(function(){{t.style.opacity='0';}},3000);}}</script>
</body></html>"""


# ====== User HTML Rendering ======

def _render_user_login(error=""):
    err_html = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DevOps Bot 用户中心</title>
<style>{_CSS}</style></head><body>
<div class="login-wrap"><div class="login-card">
<h1>DevOps Bot</h1><p class="sub">用户登录</p>
<form method="POST" action="/login">
<input name="username" placeholder="用户名" required>
<input name="password" type="password" placeholder="密码" required>
<button type="submit">登录</button>
</form>
<p class="link"><a href="/admin/">管理员登录</a></p>
{err_html}
</div></div>
</body></html>"""


def _render_user_panel(token):
    uid = user_db.validate_token(token)
    user = user_db.get_user(uid) if uid else None
    if not user:
        return _render_user_login("会话过期")

    creds = user_db.get_credentials(uid)
    cred_html = ""
    for c in creds:
        keys = ", ".join(c.get("config", {}).keys())
        cred_html += f'<div class="item"><span>{c["provider"]}</span><span style="font-size:12px;color:#888">{keys}</span></div>'

    vms = user_db.get_vms(uid)
    vm_html = ""
    for v in vms:
        vm_html += f'<div class="item"><span>{v["name"]}</span><span style="font-size:12px;color:#888">{v["host"]}:{v["port"]}</span></div>'

    skills = user_db.list_skills(uid)
    skills_html = ""
    for s in skills:
        skills_html += f'<div class="item"><span>{s["skill_name"]}</span></div>'

    mappings = user_db.get_repo_mappings(uid)
    mapping_html = ""
    for m in mappings:
        mapping_html += f'<div class="item"><span>{m["repo_key"]} / {m["event_type"]} → {m["skill_name"]}</span></div>'

    skill_opts = "".join(f'<option value="{s["skill_name"]}">{s["skill_name"]}</option>' for s in skills)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DevOps Bot 用户中心</title>
<style>{_CSS}</style></head><body>
<div class="header"><h2>DevOps Bot 用户中心</h2><span class="right">{user['username']} <a href="/logout?token={token}" style="color:#e74c3c;margin-left:8px">退出</a></span></div>
<div class="wrap">
  <div class="sidebar" style="width:180px">
    <div class="list">
      <a class="item" onclick="showSection('info')">基本信息</a>
      <a class="item" onclick="showSection('creds')">凭据管理</a>
      <a class="item" onclick="showSection('vms')">云主机</a>
      <a class="item" onclick="showSection('skills')">自定义技能</a>
    </div>
  </div>
  <div class="main">

    <div id="section-info">
      <h3>基本信息</h3>
      <div class="row"><label>用户名</label><input value="{user['username']}" disabled></div>
      <div class="row"><label>用户 ID</label><input value="{user['id']}" disabled></div>
    </div>

    <div id="section-creds" style="display:none">
      <h3>凭据管理</h3>
      <div class="list">{cred_html or '<div class="item" style="color:#888">暂无凭据</div>'}</div>
      <div class="group" style="margin-top:12px"><h4>添加/更新凭据</h4>
        <form method="POST" action="/user/cred?token={token}">
          <div class="row"><label>类型</label><select name="provider" required>
            <option value="gitcode">GitCode</option>
            <option value="feishu">飞书</option>
            <option value="huawei">华为云</option>
            <option value="codearts">CodeArts</option>
          </select></div>
          <div class="row"><label>配置 (JSON)</label><textarea name="config_json" rows="3" style="width:100%;padding:9px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px" required></textarea></div>
          <button class="btn-p" type="submit">保存</button>
        </form>
      </div>
    </div>

    <div id="section-vms" style="display:none">
      <h3>云主机</h3>
      <div class="list">{vm_html or '<div class="item" style="color:#888">暂无云主机</div>'}</div>
      <div class="group" style="margin-top:12px"><h4>新增云主机</h4>
        <form method="POST" action="/user/vm?token={token}">
          <div class="row"><label>名称</label><input name="name" required></div>
          <div class="row"><label>主机地址</label><input name="host" placeholder="IP 或域名" required></div>
          <div class="row"><label>端口</label><input name="port" value="22"></div>
          <div class="row"><label>SSH 用户</label><input name="ssh_user" value="root" required></div>
          <div class="row"><label>SSH 密码</label><input name="ssh_password" type="password" required></div>
          <button class="btn-p" type="submit">添加</button>
        </form>
      </div>
    </div>

    <div id="section-skills" style="display:none">
      <h3>自定义技能</h3>
      <div class="list">{skills_html or '<div class="item" style="color:#888">暂无技能</div>'}</div>
      <div class="group" style="margin-top:12px"><h4>上传新技能</h4>
        <form method="POST" action="/user/skill?token={token}">
          <div class="row"><label>技能名称</label><input name="skill_name" pattern="[a-z0-9-]+" placeholder="kebab-case" required></div>
          <div class="row"><label>SKILL.md 内容</label><textarea name="content" rows="8" style="width:100%;padding:9px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px" required></textarea></div>
          <button class="btn-p" type="submit">上传</button>
        </form>
      </div>
      <div class="group"><h4>仓库技能映射</h4>
        <div class="list">{mapping_html or '<div class="item" style="color:#888">暂无映射</div>'}</div>
        <form method="POST" action="/user/mapping?token={token}" style="margin-top:8px">
          <div class="row"><label>仓库</label><input name="repo_key" placeholder="owner/repo" required></div>
          <div class="row"><label>事件类型</label><select name="event_type">
            <option value="pull_request_opened">pull_request_opened</option>
            <option value="pull_request_updated">pull_request_updated</option>
            <option value="issue_opened">issue_opened</option>
            <option value="issue_comment">issue_comment</option>
            <option value="note_hook_pr">note_hook_pr</option>
          </select></div>
          <div class="row"><label>技能</label><select name="skill_name">{skill_opts}</select></div>
          <button class="btn-p" type="submit">添加映射</button>
        </form>
      </div>
    </div>

  </div>
</div>
<script>
function showSection(name){{['info','creds','vms','skills'].forEach(function(s){{document.getElementById('section-'+s).style.display=s===name?'block':'none';}});}}
</script>
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
