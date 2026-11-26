"""共享 SPA / 静态资源路由（Issue #596 P1：从 main.py 抽出，A 进程复用）。

包含：/static mount、issue_attachments、community_uploads、
NoCacheStaticMiddleware、i18n boost 注入、auth 页面、SPA catch-all、
/admin、/account、/oauth-popup-callback.html。

仅 A 进程（grape_server.py）与单进程模式（main.py）调用——B/C 进程
不提供前端页面。
"""
import asyncio
import json as _json_mod
import logging
import os
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect

# Issue #3395：前端功能开关单一下发源（shell HTML 注入 window.__FEATURES__）
from src.api.frontend_flags import apply_feature_flags, apply_faro_toggle, faro_enabled

log = logging.getLogger(__name__)

# Faro 上报反代上游（Alloy faro.receiver）。必须经 GRAPE_FARO_UPSTREAM 显式配置，
# 无默认值：未配置时 /faro/collect 直接 503 disabled，避免把部署拓扑写进代码
# （QA §1.1-1-5 / 架构审查 P2-1）。
_FARO_UPSTREAM = os.environ.get("GRAPE_FARO_UPSTREAM", "").strip()
# 单次上报体上限（SDK 批量上报通常 <64KB；公开端点防滥用）
_FARO_MAX_BODY = int(os.environ.get("GRAPE_FARO_MAX_BODY", str(256 * 1024)))

# 前端 RUM 脚本块的双向开关在 frontend_flags（与 feature flags 同一处下发源）：
# 未配 GRAPE_FARO_UPSTREAM 时整块移除，配了自动恢复。


# 同源校验开关（默认开；无 Origin 的探针请求放行）
_FARO_STRICT_ORIGIN = os.environ.get("GRAPE_FARO_STRICT_ORIGIN", "1").strip() not in ("0", "false", "no")


def _registered(app: FastAPI, path: str, method: str = "GET") -> bool:
    """该 (method, path) 是否已被别的装配函数注册过（Issue #3757）。"""
    from src.api.server_common import has_route
    return has_route(app, path, method)


def _spa_post(app: FastAPI, path: str, *, include_in_schema: bool = True):
    """幂等 POST 注册装饰器（同 `_spa_get`）。"""
    def deco(fn):
        if not _registered(app, path, "POST"):
            app.add_api_route(path, fn, methods=["POST"], include_in_schema=include_in_schema)
        return fn
    return deco


def _spa_get(app: FastAPI, path: str, *, include_in_schema: bool = True):
    """幂等 GET 注册装饰器（Issue #3757）。

    `server_common.attach_frontend` 与 `install_spa_routes` 是同一批页面路由的两份
    装配实现（M-1 架构守护），两个入口（main.py / grape_server.py）都会依次调用；
    此前只有 mount 做了「已挂过就跳过」，**页面路由没有**——同一 path 进 app.routes
    两遍，先注册者生效、第二份是死代码，却让路由注册表与契约唯一性双双失真。
    这里把「已注册则不再挂载」补齐到路由级；返回原函数，保证退化时不丢 handler。
    """
    def deco(fn):
        if not _registered(app, path):
            app.add_api_route(path, fn, methods=["GET"], include_in_schema=include_in_schema)
        return fn
    return deco


def install_spa_routes(app: FastAPI) -> None:
    """挂载静态资源 + SPA 路由（与 main.py 原实现一致，单一实现源）。

    main.py 拆分时同样调用本函数（其原内联块删除）。
    """
    from src.kernel.config import WORKSPACE_DIR as _WS_DIR

    # M-1（架构守护）：server_common.attach_frontend 与这里的挂载点是同一批路径，
    # 两个调用方（main.py / grape_server.py）都会依次调用 → 同一路径被 mount 两次，
    # 先注册者生效。这里统一「已挂过就跳过」，保证只有一份实现真正生效。
    _mounted = {getattr(r, "name", None) for r in app.routes}

    # Issue 附件静态服务（必须在 /static 之前 mount）
    _ISSUE_ATTACH_DIR = os.path.join(_WS_DIR, "..", "data", "issue_attachments")
    os.makedirs(_ISSUE_ATTACH_DIR, exist_ok=True)
    if "issue_attachments" not in _mounted:
        app.mount("/static/issue_attachments", StaticFiles(directory=_ISSUE_ATTACH_DIR), name="issue_attachments")

    # Community uploads
    _api_dir = os.path.dirname(os.path.abspath(__file__))
    _COMMUNITY_UPLOAD_DIR = os.path.join(_api_dir, "data", "community_uploads")
    os.makedirs(_COMMUNITY_UPLOAD_DIR, exist_ok=True)
    if "community_uploads" not in _mounted:
        app.mount("/static/community_uploads", StaticFiles(directory=_COMMUNITY_UPLOAD_DIR), name="community_uploads")

    # Vue refactor: static root moved to frontend/shell (phase 1); keep serving under /static
    _legacy_dir = os.path.join(_api_dir, "frontend", "shell")
    STATIC_DIR = _legacy_dir if os.path.isdir(_legacy_dir) else os.path.join(_api_dir, "static")
    if not os.path.isdir(STATIC_DIR):
        return

    if "static" not in _mounted:
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Vue refactor (phase 1): serve new Vue app build output under /static-vue/
    # (approved Gate 0 option A). Temporary mount — removed when migration completes.
    _VUE_DIST_DIR = os.path.join(_api_dir, "frontend", "static-dist")
    if os.path.isdir(_VUE_DIST_DIR) and "static_vue" not in _mounted:
        class _VueSPAStaticFiles(StaticFiles):
            """StaticFiles with history-mode fallback: 只吞 404，其余异常必须上抛。

            M-1（架构守护）：与 server_common._VueSPAStaticFiles 语义对齐 —— 真实 5xx
            被伪装成 200 HTML 会毁掉故障定位（INFO-3 原形态）。
            """
            async def get_response(self, path, scope):
                from starlette.exceptions import HTTPException as _HTTPExc
                from starlette.responses import FileResponse as _FR
                try:
                    return await super().get_response(path, scope)
                except _HTTPExc as exc:
                    if exc.status_code != 404:
                        raise
                    resp = _FR(os.path.join(_VUE_DIST_DIR, "index.html"))
                    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
                    return resp
        app.mount("/static-vue", _VueSPAStaticFiles(directory=_VUE_DIST_DIR, html=True), name="static_vue")

    # Static assets: allow browser caching with ETag revalidation.
    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            if request.url.path.startswith("/static/"):
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    app.add_middleware(NoCacheStaticMiddleware)

    from src.domains.mgmt.auth.email_service import is_email_configured as _is_email_configured

    # ── i18n boost: 服务端注入翻译数据，消除首屏 i18n 闪动 ──
    _I18N_BOOST_CACHE = None

    def _get_i18n_boost():
        """启动时读取 locale JSON，提取 auth 页面 [data-i18n] 需要的 key。"""
        nonlocal _I18N_BOOST_CACHE
        if _I18N_BOOST_CACHE is not None:
            return _I18N_BOOST_CACHE

        try:
            import re as _re
            boost = {'en': {'app': {}}, 'zh': {'app': {}}}

            auth_keys = set()
            for fname in ('register.html', 'forgot.html', 'index.html'):
                fpath = os.path.join(STATIC_DIR, fname)
                if os.path.isfile(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        html = f.read()
                    for m in _re.finditer(r'data-i18n(?:-placeholder)?="([^"]+)"', html):
                        auth_keys.add(m.group(1))

            for lang in ('en', 'zh'):
                app_path = os.path.join(STATIC_DIR, 'locales', lang, 'app.json')
                if os.path.isfile(app_path):
                    with open(app_path, 'r', encoding='utf-8') as f:
                        app_data = _json_mod.load(f)
                    org_path = os.path.join(STATIC_DIR, 'locales', lang, 'organizations.json')
                    org_data = {}
                    if os.path.isfile(org_path):
                        with open(org_path, 'r', encoding='utf-8') as of:
                            org_data = _json_mod.load(of)
                    for k in auth_keys:
                        parts = k.split('.')
                        if parts[0] == 'app' and len(parts) >= 2:
                            section, leaf = parts[1], parts[-1]
                            if section not in app_data:
                                continue
                            if isinstance(app_data[section], dict):
                                if leaf in app_data[section]:
                                    boost[lang]['app'].setdefault(section, {})[leaf] = app_data[section][leaf]
                            elif parts[1] == leaf:
                                boost[lang]['app'][leaf] = app_data[section]
                        elif parts[0] == 'organizations' and len(parts) >= 2:
                            leaf = parts[-1]
                            if leaf in org_data:
                                boost[lang].setdefault('organizations', {})[leaf] = org_data[leaf]

                common_path = os.path.join(STATIC_DIR, 'locales', lang, 'common.json')
                if os.path.isfile(common_path):
                    with open(common_path, 'r', encoding='utf-8') as f:
                        common_data = _json_mod.load(f)
                    for ck in ('server_error', 'request_failed', 'session_expired', 'network_upload_error'):
                        if ck in common_data:
                            boost[lang].setdefault('common', {})[ck] = common_data[ck]

            _I18N_BOOST_CACHE = _json_mod.dumps(boost, ensure_ascii=False, separators=(',', ':'))
        except Exception as _e:
            import sys
            print(f"[WARNING] Failed to build i18n boost pack: {_e}", file=sys.stderr)
            _I18N_BOOST_CACHE = '{}'
        return _I18N_BOOST_CACHE

    # ── i18n early-translate: 内联翻译 script ──
    _EARLY_TRANSLATE_SCRIPT = '''<script>
(function(){if(!window.__I18N_BOOST__)return;var lang=localStorage.getItem('grapeLang')||((location.pathname.indexOf('/en')===0)?'en':'zh');if(lang==='zh'){document.documentElement.classList.remove('i18n-loading');return;}
var P=window.__I18N_BOOST__[lang];if(!P)return;
function L(k){var p=k.split('.'),c=P;for(var i=0;i<p.length;i++){if(c==null||typeof c!=='object')return;c=c[p[i]];}return c;}
var els=document.querySelectorAll('[data-i18n]');for(var i=0;i<els.length;i++){var k=els[i].getAttribute('data-i18n');if(k){var v=L(k);if(v&&v!==k)els[i].textContent=v;}}
var phs=document.querySelectorAll('[data-i18n-placeholder]');for(var i=0;i<phs.length;i++){var k=phs[i].getAttribute('data-i18n-placeholder');if(k){var v=L(k);if(v&&v!==k)phs[i].setAttribute('placeholder',v);}}
document.documentElement.classList.remove('i18n-loading');})();
</script>'''

    # UC15（#535）：早期兑票内联脚本（治本方案）
    _EARLY_EXCHANGE_SCRIPT = '''<script>
(function(){
  try {
    var m = location.search.match(/[?&]exchange_code=([0-9a-f]{16,})/);
    if (!m) return;
    var code = m[1];
    sessionStorage.setItem('__oauth_inline_pending__', '1');
    if (window.history.replaceState) window.history.replaceState(null, '', location.pathname);
    fetch('/api/auth/exchange', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: code}),
    }).then(function(r){
      if (!r.ok) {
        return r.json().catch(function(){ return null; }).then(function(b){
          var d = (b && b.detail) || {};
          throw {msg: d.message || d.error_code || ''};
        });
      }
      return r.json();
    }).then(function(d){
      try {
        localStorage.setItem('token', d.token);
        localStorage.setItem('app_cache_ver', '47');
        localStorage.removeItem('repo_member_cache');
        localStorage.removeItem('currentRepo');
        localStorage.removeItem('current_org_id');
        sessionStorage.removeItem('currentRepo');
        sessionStorage.setItem('__oauth_inline_login__', '1');
        if (d.first_login) sessionStorage.setItem('__oauth_inline_first_login__', '1');
      } catch (e) {}
      try { sessionStorage.removeItem('__oauth_inline_pending__'); } catch (e) {}
    }).catch(function(e){
      try {
        sessionStorage.setItem('__oauth_exchange_failed__', '1');
        var _em = (e && e.msg) || '';
        if (_em) sessionStorage.setItem('__oauth_exchange_failed_msg__', String(_em));
      } catch (e2) {}
      try { sessionStorage.removeItem('__oauth_inline_pending__'); } catch (e2) {}
    });
  } catch (e) {}
})();
</script>'''

    def _inject_email_configured(html: str) -> str:
        """在 </head> 前注入邮箱/OAuth 配置状态 + i18n boost 翻译数据。"""
        from src.domains.mgmt.auth.providers import get_provider, list_providers
        from src.domains.mgmt.user.db import get_meta_value
        configured = _is_email_configured()
        val = 'true' if configured else 'false'
        cls = 'email-configured' if configured else 'email-not-configured'
        boost_json = _get_i18n_boost()
        # D108（登录按钮零延迟）：注入完整渠道目录（固定展示序），app.js defer
        # 阶段首帧渲染前同步生成按钮，不再等 /auth/providers AJAX
        try:
            from src.domains.mgmt.auth.providers import providers_catalog
            catalog = providers_catalog()
        except Exception:
            catalog = []
        oauth_map = {p["provider"]: bool(p["configured"]) for p in catalog}
        if not oauth_map:
            for key in list_providers():
                client_id = get_meta_value(f"oauth_{key}_client_id") or ""
                oauth_map[key] = bool(client_id)
        oauth_classes = ' '.join(
            f'oauth-{key}-not-configured' for key, ok in oauth_map.items() if not ok
        )
        oauth_json = _json_mod.dumps(oauth_map)
        faro_flag = 'true' if faro_enabled() else 'false'
        head_inject = (
            f'<script>window.__EMAIL_CONFIGURED__={val};document.documentElement.className+=" {cls} {oauth_classes}";'
            f'window.__OAUTH_CONFIGURED__={oauth_json};'
            f'window.__OAUTH_PROVIDERS__={_json_mod.dumps(catalog)};'
            f'window.__FARO_ENABLED__={faro_flag};'
            f'window.__I18N_BOOST__={boost_json};</script>'
        )
        html = html.replace('</head>', head_inject + '</head>', 1)
        html = html.replace('</body>', _EARLY_TRANSLATE_SCRIPT + '</body>', 1)
        html = html.replace('</head>', _EARLY_EXCHANGE_SCRIPT + '</head>', 1)
        # Issue #3395：注入前端功能开关（flag=0 时移除侧边栏插件入口整块）
        html = apply_feature_flags(html)
        # 前端 RUM（Faro）：未配 GRAPE_FARO_UPSTREAM 时整块移除，配了自动恢复
        html = apply_faro_toggle(html)
        return html

    def _serve_auth_page(filename: str) -> HTMLResponse:
        with open(os.path.join(STATIC_DIR, filename), 'r', encoding='utf-8') as f:
            html = f.read()
        html = _inject_email_configured(html)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    # 官网落地页优先（同 server_common._landing_file；未配置时行为不变）
    _LANDING_DIR = os.environ.get("GRAPE_LANDING_DIR", "")

    _DOCS_DIR = os.environ.get("GRAPE_DOCS_DIR", "")

    def _landing_file(rel: str):
        if rel == "docs" or rel.startswith("docs/"):
            if not _DOCS_DIR or not os.path.isdir(_DOCS_DIR):
                return None
            droot = os.path.realpath(_DOCS_DIR)
            dfull = os.path.realpath(os.path.join(droot, rel[5:] or "index.html"))
            if dfull != droot and not dfull.startswith(droot + os.sep):
                return None
            if os.path.isdir(dfull):
                dfull = os.path.join(dfull, "index.html")
            return dfull if os.path.isfile(dfull) else None
        if not _LANDING_DIR or not os.path.isdir(_LANDING_DIR):
            return None
        root = os.path.realpath(_LANDING_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return None
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return full if os.path.isfile(full) else None

    def _spa_response():
        with open(os.path.join(STATIC_DIR, "index.html"), 'r', encoding='utf-8') as f:
            html = f.read()
        html = _inject_email_configured(html)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

    @_spa_post(app, "/faro/collect")
    async def faro_collect_proxy(request: Request):
        """Faro 上报反代 → Alloy faro.receiver（前端 RUM）。

        同源路径，避免浏览器直连内部地址 + 硬编码 host。
        上游必须由 GRAPE_FARO_UPSTREAM 提供；未配置时返回 503 disabled。
        透传 JSON body（gzip 由 SDK 自带 transport 处理），非 2xx 原样透传状态码。

        公开端点防护（架构守护 Medium-2）：
        - 同源校验：Origin/Referer 的 host 必须与请求 host 一致，否则 403
          （可用 GRAPE_FARO_STRICT_ORIGIN=0 关闭，便于本地调试/无 Origin 的探针）
        - 双重限体：content-length 预判 + 实际 body 校验，超限 413
        - 无鉴权（浏览器 RUM 无法携带 token）；限速建议在 nginx 层配置
        """
        if not _FARO_UPSTREAM:
            return Response(content='{"error":"faro relay disabled"}', status_code=503,
                            media_type="application/json")
        # 同源校验（Origin 优先，其次 Referer）
        if _FARO_STRICT_ORIGIN:
            _origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if _origin:
                from urllib.parse import urlparse
                _ohost = (urlparse(_origin).hostname or "").lower()
                _rhost = (request.headers.get("host") or request.url.hostname or "").lower()
                _rhost = _rhost.split(":")[0]
                if _ohost and _rhost and _ohost != _rhost:
                    return Response(content='{"error":"cross-origin denied"}', status_code=403,
                                    media_type="application/json")
        if int(request.headers.get("content-length") or 0) > _FARO_MAX_BODY:
            return Response(content='{"error":"payload too large"}', status_code=413,
                            media_type="application/json")
        import httpx
        # Issue #4248：读取请求体也可能因客户端在途断开而抛异常（页面卸载/切换），
        # 必须在本 handler 内兜住，避免直落全局兜底被记成服务端 500。
        try:
            body = await request.body()
        except (ClientDisconnect, asyncio.CancelledError):
            # 客户端主动中止（nginx 记 499）：不是服务端故障。返回 499 让访问日志
            # 与指标按状态码与 5xx 分离（499 不计入错误率）；仅 debug 留痕，避免刷屏。
            log.debug("faro collect: client disconnected while reading request body")
            return Response(content='{"error":"client disconnected"}', status_code=499,
                            media_type="application/json")
        if len(body) > _FARO_MAX_BODY:
            return Response(content='{"error":"payload too large"}', status_code=413,
                            media_type="application/json")
        headers = {"Content-Type": request.headers.get("content-type", "application/json")}
        if request.headers.get("content-encoding"):
            headers["Content-Encoding"] = request.headers["content-encoding"]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                upstream = await client.post(
                    _FARO_UPSTREAM, content=body, headers=headers)
            return Response(content=upstream.content, status_code=upstream.status_code,
                            media_type="application/json")
        except Exception as e:
            log.warning(f"faro collect proxy error: {type(e).__name__}: {e}")
            return Response(content='{"error":"faro upstream unavailable"}', status_code=502,
                            media_type="application/json")

    @_spa_get(app, "/")
    async def serve_spa():
        _lf = _landing_file("index.html")
        if _lf:
            from fastapi.responses import FileResponse as _LFR
            return _LFR(_lf)
        return _spa_response()

    @_spa_get(app, "/register")
    async def serve_register():
        """注册页面 — 独立 HTML。"""
        return _serve_auth_page("register.html")

    @_spa_get(app, "/forgot")
    async def serve_forgot():
        """忘记密码页面 — 独立 HTML。"""
        return _serve_auth_page("forgot.html")

    @_spa_get(app, "/en")
    @_spa_get(app, "/en/{path:path}")
    async def serve_spa_en(path: str = ""):
        """英文国际化路由——返回同一个 index.html。"""
        from fastapi import HTTPException
        if path.startswith("api/") or path.startswith("static/"):
            raise HTTPException(status_code=404)
        _lf = _landing_file(os.path.join("en", path, "index.html") if not path else os.path.join("en", path))
        if _lf:
            from fastapi.responses import FileResponse as _LFR
            return _LFR(_lf)
        if path == "register":
            return _serve_auth_page("register.html")
        if path == "forgot":
            return _serve_auth_page("forgot.html")
        if path == "admin" or path.startswith("admin/"):
            _admin_html = os.path.join(STATIC_DIR, "admin.html")
            if os.path.isfile(_admin_html):
                return FileResponse(_admin_html,
                                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return _spa_response()

    # SPA catch-all 延迟注册（不拦截 /health /healthz 等系统端点）
    @_spa_get(app, "/admin", include_in_schema=False)
    @_spa_get(app, "/admin/{path:path}", include_in_schema=False)
    async def serve_admin(path: str = ""):
        """Admin 独立页面。"""
        if path.startswith("api/") or path.startswith("static/"):
            raise HTTPException(status_code=404)
        _admin_html = os.path.join(STATIC_DIR, "admin.html")
        if os.path.isfile(_admin_html):
            resp = FileResponse(_admin_html)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp
        raise HTTPException(status_code=404, detail="admin.html not found")

    @_spa_get(app, "/account", include_in_schema=False)
    @_spa_get(app, "/account/{path:path}", include_in_schema=False)
    async def serve_account_redirect():
        """旧用户中心 URL 302 重定向到内嵌 SPA view。"""
        return RedirectResponse(url="/#account", status_code=302)

    @_spa_get(app, "/oauth-popup-callback.html", include_in_schema=False)
    async def serve_oauth_popup_callback():
        """OAuth 弹窗回调中间页。"""
        resp = FileResponse(os.path.join(STATIC_DIR, "oauth-popup-callback.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @_spa_get(app, "/favicon.ico", include_in_schema=False)
    async def serve_favicon():
        """页签图标：浏览器/书签直连 /favicon.ico，不能落到 SPA 兜底。

        兜底会把 favicon.ico 当成页面路由返回 200 + index.html，浏览器拿到非图片
        内容就只能显示默认空白图标（下线前浏览器还爱请求它）。
        """
        _fav = os.path.join(STATIC_DIR, "favicon.ico")
        if not os.path.isfile(_fav):
            raise HTTPException(status_code=404)
        resp = FileResponse(_fav, media_type="image/x-icon")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @_spa_get(app, "/{path:path}")
    async def spa_catch_all(path: str):
        if path.startswith("api/") or path.startswith("static/"):
            raise HTTPException(status_code=404)
        return _spa_response()
