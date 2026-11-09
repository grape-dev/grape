"""Issue #535：OAuth 未配置的登录提示与状态前置（B+ 方案）测试。

对应需求文档 docs/issue-535/spec.md 场景用例 UC1–UC8。

覆盖（源码检查方式——前端 JS 无法在 pytest 中执行，
与 test_issue_372_round3_frontend.py 同模式；浏览器端到端
实测记录见 Issue #535 评论）：
- UC1/UC3: renderOAuthButtons 按 configured 置灰 + 常显说明行
- UC2/UC5: oauthLogin 前置拦截（状态已知未配置）+ A 兜底（catch 400 分流）
- UC6: 非配置类失败走通用文案（不误导查配置）
- UC7/UC8: 说明行 data-i18n 化，语言切换/包晚到自动刷新
"""
import json
import re
import os

import pytest


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")


def _read_file(rel_path):
    """读取静态文件内容。"""
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


def _read_json(rel_path):
    """读取 locale JSON。"""
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, encoding="utf-8") as f:
        return json.load(f)


# ===========================================================================
# UC1/UC3：置灰 + 常显说明（renderOAuthButtons）
# ===========================================================================

class TestRenderDisabledWithHint:
    """UC1/UC3: 按 configured 置灰且常显说明行，全部已配置时移除。"""

    def test_uc1_state_map_filled_from_providers(self):
        """UC1: providers 返回后填充 _oauthConfiguredMap（状态表）。"""
        source = _read_file("js/app.js")
        assert "_oauthConfiguredMap = {}" in source, \
            "renderOAuthButtons should fill _oauthConfiguredMap from providers"
        assert "_oauthConfiguredMap[p.provider] = !!p.configured" in source

    def test_uc1_unconfigured_button_gets_aria_disabled(self):
        """UC1: 重渲染路径未配置按钮带 aria-disabled（不用真 disabled）。"""
        source = _read_file("js/app.js")
        assert 'var ariaVar = p.configured ? \'\'' in source and \
            'aria-disabled="true"' in source, \
            "rendered button should carry aria-disabled when not configured"

    def test_uc3_prerendered_buttons_sync_aria(self):
        """UC3: 预渲染按钮同步 aria-disabled（setAttribute/removeAttribute）。"""
        source = _read_file("js/app.js")
        assert "existingBtns[j].setAttribute('aria-disabled', 'true')" in source, \
            "prerendered buttons should sync aria-disabled"
        assert "existingBtns[j].removeAttribute('aria-disabled')" in source, \
            "configured buttons should drop aria-disabled"

    def test_uc1_hint_function_exists_and_removes_when_all_configured(self):
        """UC1: 说明行渲染函数存在；全部已配置时隐藏（UC12 后 hint 为预置 DOM，隐藏而非 remove）。"""
        source = _read_file("js/app.js")
        assert "function _renderOAuthUnconfiguredHint" in source
        idx = source.find("function _renderOAuthUnconfiguredHint")
        func = source[idx:idx + 2000]
        assert "providers.filter(function(p) { return !p.configured; })" in func
        assert "unconfigured.length === 0" in func and "style.display = 'none'" in func, \
            "hint should be hidden when all providers configured"

    def test_uc1_all_providers_still_rendered(self):
        """UC1: 不过滤 configured——所有平台按钮始终渲染（R7.5 保持）。"""
        source = _read_file("js/app.js")
        idx = source.find("function renderOAuthButtons")
        func = source[idx:idx + 2500]
        assert "filter(function(p){return p.configured})" not in func, \
            "renderOAuthButtons should NOT filter providers by configured"
        assert "providers.map" in func, \
            "renderOAuthButtons should use providers.map (all providers)"

    def test_uc1_css_greyed_state(self):
        """UC1: 置灰样式存在（aria-disabled 选择器 + 说明行样式）。"""
        css = _read_file("css/auth.css")
        assert '.oauth-btn[aria-disabled="true"]' in css
        assert "opacity: 0.45" in css
        assert ".oauth-unconfigured-hint" in css

    def test_uc1_hint_links_to_help_doc(self):
        """UC1: 说明行含文档深链（help.html 开发者集成章节）。"""
        source = _read_file("js/app.js")
        idx = source.find("function _oauthDocUrl")
        func = source[idx:idx + 400]
        assert "/static/help.html#/developer/01-gitcode-integration" in func
        assert "/static/help.html#/developer/02-github-integration" in func


# ===========================================================================
# UC2/UC5：前置拦截 + A 兜底（oauthLogin）
# ===========================================================================

class TestOauthLoginGateAndFallback:
    """UC2: 状态已知未配置前置拦截；UC5: 状态未知走 API A 兜底。"""

    def test_uc2_known_unconfigured_short_circuits(self):
        """UC2: _oauthConfiguredMap[provider] === false 时不发请求直接 toast。"""
        source = _read_file("js/app.js")
        assert "_oauthConfiguredMap[provider] === false" in source, \
            "oauthLogin should short-circuit when provider known unconfigured"

    def test_uc5_unknown_state_does_not_block(self):
        """UC5: 前置判断为严格 === false——状态未知（null/undefined）不拦截。"""
        source = _read_file("js/app.js")
        assert "=== false" in source.split("function oauthLogin")[1][:800], \
            "gate must be strict === false so unknown state falls through"

    def test_uc5_catch_distinguishes_not_configured_400(self):
        """UC5: catch 按 status=400 + 「未配置」匹配走配置文案。"""
        source = _read_file("js/app.js")
        assert "e.status === 400" in source
        assert "/未配置|not configured/i" in source, \
            "not-configured regex should cover zh and en backend messages"

    def test_uc6_other_failures_use_generic_message(self):
        """UC6: 非未配置失败走 oauth_request_failed 通用文案。"""
        source = _read_file("js/app.js")
        assert "app.login.oauth_request_failed" in source, \
            "non-config failures should use generic oauth_request_failed"
        # 通用分支不携带文档链接（不误导查配置）
        idx = source.find("app.login.oauth_request_failed")
        seg = source[idx - 400:idx + 400]
        assert "_oauthDocUrl" not in seg, \
            "generic failure branch should not attach config help link"


# ===========================================================================
# UC7/UC8：说明行语言跟随（data-i18n 化）
# ===========================================================================

class TestHintI18nFollowsLanguage:
    """UC7/UC8: 说明行 data-i18n + data-i18n-vars，语言切换/包晚到自动刷新。"""

    def test_uc7_hint_uses_data_i18n_attributes(self):
        """UC7: 说明行文本挂 data-i18n（setLang/applyI18nToDOM 自动刷新）。"""
        source = _read_file("js/app.js")
        idx = source.find("function _renderOAuthUnconfiguredHint")
        func = source[idx:idx + 2500]
        assert 'data-i18n="app.login.oauth_needs_config"' in func, \
            "hint text should be driven by data-i18n"
        assert "data-i18n-vars" in func, \
            "hint should pass {name} via data-i18n-vars"

    def test_uc7_all_text_escaped(self):
        """UC7: 动态更新的文本用 textContent 赋值（等价 XSS 防御；UC12 后预置 DOM 结构固定，
        JS 只写 textContent/attribute，不拼 innerHTML）。"""
        source = _read_file("js/app.js")
        idx = source.find("function _renderOAuthUnconfiguredHint")
        end = source.find("function openBetaApply", idx)
        func = source[idx:end if end > 0 else idx + 1600]
        assert "innerHTML" not in func, \
            "hint updates must not use innerHTML (textContent only)"
        assert func.count("textContent") >= 2, \
            "hint text and link text should be set via textContent"

    def test_uc7_keys_present_in_all_three_i18n_sources(self):
        """UC7: 新 key 在 zh/en locale + js/i18n.js 内置兜底三处同步。"""
        zh = _read_json("locales/zh/app.json")["login"]
        en = _read_json("locales/en/app.json")["login"]
        builtin = _read_file("js/i18n.js")
        for key in ("oauth_needs_config", "oauth_request_failed",
                    "oauth_not_configured", "oauth_not_configured_hint",
                    "oauth_view_config_help"):
            assert key in zh, f"zh/app.json login.{key} missing"
            assert key in en, f"en/app.json login.{key} missing"
            assert key in builtin, f"js/i18n.js builtin {key} missing"

    def test_uc7_needs_config_has_name_placeholder(self):
        """UC7: oauth_needs_config 带 {name} 占位符（多平台拼接）。"""
        zh = _read_json("locales/zh/app.json")["login"]
        en = _read_json("locales/en/app.json")["login"]
        assert "{name}" in zh["oauth_needs_config"]
        assert "{name}" in en["oauth_needs_config"]


# ===========================================================================
# UC4：正常态不受影响
# ===========================================================================

class TestAllConfiguredNormal:
    """UC4: 全部已配置时无置灰、无说明行，登录走原流程。"""

    def test_uc4_no_hint_element_in_static_html(self):
        """UC4/UC12: index.html 预置说明行但 display:none——CSS 按注入类首帧显示
        （零闪动）；预置按钮不带 aria-disabled（aria 由 JS 按权威状态补）。"""
        html = _read_file("index.html")
        assert 'id="oauthUnconfiguredHint"' in html, \
            "hint should be prerendered in static HTML (FOUC fix)"
        assert 'style="display:none"' in html.split('id="oauthUnconfiguredHint"')[1][:80], \
            "prerendered hint must start hidden until the injected class shows it"
        assert 'aria-disabled' not in html, \
            "prerendered buttons should not carry aria-disabled"

    def test_uc4_oauth_redirect_flow_untouched(self):
        """UC4: redirect_url 重定向逻辑保持原样。"""
        source = _read_file("js/app.js")
        assert "if (d.redirect_url) window.location.href = d.redirect_url;" in source


# ===========================================================================
# UC9/UC10：同类「未配置」提示的体验对齐（S7 P1 收口）
# ===========================================================================

class TestEmailNotConfiguredFriendlyHint:
    """UC9/UC10: 忘记密码页与账号页三入口的邮箱未配置提示——可行动指引 + 文档链接。"""

    def test_uc9_forgot_page_links_to_email_guide(self):
        """UC9: 忘记密码页未配置提示带 help.html 邮件配置深链。"""
        html = _read_file("forgot.html")
        assert "04-storage-and-email" in html, \
            "forgot page should deep-link to email setup guide"
        # 提示文案与链接文字均挂 data-i18n（随语言切换，不残留）
        assert 'data-i18n="app.login.forgot_email_not_configured"' in html
        assert 'data-i18n="app.login.oauth_view_config_help"' in html

    def test_uc10_account_helper_uses_showtoast_link(self):
        """UC10: 统一辅助函数用 showToast 主/副文案 + link 深链。"""
        source = _read_file("js/pages/account.js")
        assert "function _accountShowEmailNotConfiguredToast" in source
        idx = source.find("function _accountShowEmailNotConfiguredToast")
        func = source[idx:idx + 900]
        assert "app.showToast(" in func
        assert "/static/help.html#/developer/04-storage-and-email" in func, \
            "toast should deep-link email setup guide"
        assert "email_view_config_help" in func
        # 副文案给可行动指引（不只说不能做什么）
        assert "email_not_configured_hint" in func

    def test_uc10_three_entry_points_use_helper(self):
        """UC10: 重置/设置密码、换邮箱三入口都走统一辅助函数（各自 context key）。"""
        source = _read_file("js/pages/account.js")
        for key in ("reset_pwd_blocked_by_email",
                    "set_pwd_blocked_by_email",
                    "change_email_blocked_by_email"):
            assert key in source, f"account.js should use context key {key}"
        # 旧的裸 toast 调用不应残留（email_not_configured 直调）
        assert "I18N.t('app.account.email_not_configured') ||" not in source, \
            "raw single-line toast should be replaced by helper"

    def test_uc10_context_keys_in_both_locales(self):
        """UC10: 三个 context key + hint/链接 key 在 zh/en 同步。"""
        zh = _read_json("locales/zh/app.json")["account"]
        en = _read_json("locales/en/app.json")["account"]
        for key in ("reset_pwd_blocked_by_email",
                    "set_pwd_blocked_by_email",
                    "change_email_blocked_by_email",
                    "email_not_configured_hint",
                    "email_view_config_help"):
            assert key in zh, f"zh account.{key} missing"
            assert key in en, f"en account.{key} missing"

    def test_uc11_email_card_degrades_not_hides(self):
        """UC11: 邮箱未配置时账号页邮箱卡片降级展示（说明+链接），不再整卡隐藏。"""
        source = _read_file("js/pages/account.js")
        idx = source.find("卡片2")
        seg = source[idx:idx + 2500]
        # 降级分支存在：说明文案 + 深链
        assert "email_service_not_configured_card" in seg, \
            "unconfigured email card should degrade with explanation"
        assert "/static/help.html#/developer/04-storage-and-email" in seg, \
            "degraded card should link the setup guide"
        # 卡片不再凭空消失：两个分支都渲染 id=accEmailCard
        assert seg.count('id="accEmailCard"') == 2, \
            "email card should render in both configured and unconfigured states"

    def test_uc11_card_key_in_both_locales(self):
        """UC11: 卡片降级文案 zh/en 同步。"""
        zh = _read_json("locales/zh/app.json")["account"]
        en = _read_json("locales/en/app.json")["account"]
        assert "email_service_not_configured_card" in zh
        assert "email_service_not_configured_card" in en


# ===========================================================================
# UC13：引导页绑定弹窗的 OAuth tab（配置状态数据流断裂修复）
# ===========================================================================

class TestBindModalOauthTab:
    """UC13: 已配置 OAuth 时，新手引导页的绑定弹窗应显示 OAuth + 手动令牌双页签。

    根因：_accountProvidersData 只在账号安全 tab 填充，引导页路径缓存为 null
    时被保守猜成「未配置」——已配置场景也只有 token 一个页签。
    修复两层：①引导页渲染时写入共享缓存；②弹窗缓存缺失时主动 fetch 权威状态。
    """

    def test_uc13_guide_writes_shared_cache(self):
        """UC13 层1: 引导页 _renderBindingGuide 把 providers 数据写入共享缓存。"""
        source = _read_file("js/app.js")
        idx = source.find("function _renderBindingGuide")
        func = source[idx:source.find("function _renderOnboardingShell", idx)]
        assert "_accountProvidersData = d" in func, \
            "binding guide should populate the shared providers cache"

    def test_uc13_modal_fetches_when_cache_missing(self):
        """UC13 层2: 绑定弹窗缓存缺失时主动 fetch /auth/providers，不保守猜未配置。"""
        source = _read_file("js/pages/account.js")
        idx = source.find("function _accountBindProvider")
        end = source.find("function _accountSwitchBindTab", idx)
        func = source[idx:end]
        # 缓存缺失 → fetch 权威状态再渲染
        assert "API.get('/auth/providers')" in func, \
            "bind modal should fetch providers when cache is missing"
        # 不再出现旧的无条件空回退（保守猜未配置）
        assert "_accountProvidersData || { providers: [] }" not in func, \
            "stale conservative fallback must be gone"
        # fetch 失败才降级为未配置（与旧行为一致的安全网）
        catch_seg = func[func.find("}).catch(function() {"):]
        assert "{ providers: [] }" in catch_seg, \
            "fetch failure should degrade to unconfigured, not break the modal"

    def test_uc13_modal_uses_cache_without_extra_request(self):
        """UC13: 缓存已有（层1填充/安全tab填充）时直接渲染，不发额外请求。"""
        source = _read_file("js/pages/account.js")
        idx = source.find("function _accountBindProvider")
        end = source.find("function _accountSwitchBindTab", idx)
        func = source[idx:end]
        assert "if (_providersData)" in func, \
            "cached path must render synchronously without fetching"


# ===========================================================================
# UC12：首帧零闪动（服务端注入 + CSS 驱动，注册页同模式）
# ===========================================================================

class TestFirstFrameNoFouc:
    """UC12: OAuth 配置状态随 HTML 注入，CSS 首帧置灰，无「先常亮后置灰」闪动。"""

    def test_uc12_server_injects_oauth_state(self):
        """UC12: main.py 注入 __OAUTH_CONFIGURED__ + 按平台粒度的 html 类。"""
        source = _read_file_py("src/api/main.py")
        assert "window.__OAUTH_CONFIGURED__" in source, \
            "server should inject OAuth config state into HTML head"
        assert "oauth-{key}-not-configured" in source, \
            "injected class must be per-provider (single-configured must not grey the other)"

    def test_uc12_css_greys_on_first_frame(self):
        """UC12: CSS 按注入的按平台类在首帧（defer JS 前）弱化对应按钮。"""
        css = _read_file("css/auth.css")
        assert 'html[class*="oauth-gitcode-not-configured"] #oauthBtns .oauth-btn' in css, \
            "first-frame greying must be CSS-driven on the per-provider injected class"

    def test_uc12_js_sync_block_reads_injection(self):
        """UC12: app.js 同步块读注入状态补 aria/说明行；注入缺失则跳过。"""
        source = _read_file("js/app.js")
        assert "_applyInjectedOAuthState" in source
        idx = source.find("function _applyInjectedOAuthState") if "function _applyInjectedOAuthState" in source \
            else source.find("(function _applyInjectedOAuthState")
        seg = source[idx:idx + 1600]
        assert "window.__OAUTH_CONFIGURED__" in seg
        assert "_renderOAuthUnconfiguredHint" in seg
        # 注入异常 → 静默跳过（A 兜底），不抛错
        assert "catch (e)" in seg

    def test_uc12_runtime_state_corrects_html_class(self):
        """UC12: providers 权威状态按平台纠正 html 类——注入旧值不残留。"""
        source = _read_file("js/app.js")
        assert "var _cls = 'oauth-' + p.provider + '-not-configured'" in source, \
            "renderOAuthButtons must correct the per-provider snapshot classes"
        assert "classList.toggle('oauth-configured'" in source, \
            "oauth-configured stays the all-configured anchor for hint visibility"

    def test_uc12c_prerendered_hint_is_neutral(self):
        """UC12c: 预置说明用 generic key（不点名平台）——首帧不出现错误平台名单。"""
        html = _read_file("index.html")
        seg = html.split('id="oauthUnconfiguredHint"')[1][:400]
        assert 'data-i18n="app.login.oauth_needs_config_generic"' in seg, \
            "prerendered hint must use the neutral generic key"
        assert "GitCode / GitHub" not in seg, \
            "prerendered hint must not hardcode a platform list"

    def test_uc12c_generic_key_in_all_three_i18n_sources(self):
        """UC12c: generic key 在 zh/en locale + js/i18n.js 内置兜底三处同步。"""
        zh = _read_json("locales/zh/app.json")["login"]
        en = _read_json("locales/en/app.json")["login"]
        builtin = _read_file("js/i18n.js")
        assert "oauth_needs_config_generic" in zh
        assert "oauth_needs_config_generic" in en
        assert "oauth_needs_config_generic" in builtin
        # generic 文案不点名（无 {name} 占位符）
        assert "{name}" not in zh["oauth_needs_config_generic"]

    def test_uc12c_js_switches_generic_to_named_key(self):
        """UC12c: JS 拿到真实名单后把 span 从 generic key 切到 {name} key。"""
        source = _read_file("js/app.js")
        idx = source.find("function _renderOAuthUnconfiguredHint")
        end = source.find("function openBetaApply", idx)
        func = source[idx:end if end > 0 else idx + 1600]
        assert "oauth_needs_config_generic" in func, \
            "hint updater must select the prerendered generic-keyed span"
        assert "span.setAttribute('data-i18n', 'app.login.oauth_needs_config')" in func, \
            "updater must switch the span to the named key so later language switches render precisely"


def _read_file_py(rel_path):
    """读取静态目录之外的仓库文件（main.py 等后端源码）。"""
    full = os.path.join(os.path.dirname(__file__), "..", rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# S8（UC14/UC15）：OAuth 回调兑票链路——TTL 60s + 内联兑票脚本
# ===========================================================================

def _read_src(rel_path):
    """读取仓库源码文件（src/ 下，非 static）。"""
    full = os.path.join(os.path.dirname(__file__), "..", rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


class TestUC14ExchangeCodeTTL:
    """UC14: exchange code TTL 10s→60s，原子消费不变。"""

    def test_uc14_ttl_60s_on_both_insert_paths(self):
        """UC14: 两条 INSERT 路径（首插 + 冲突重试）都写 now+60。"""
        src = _read_src("src/user/db.py")
        idx = src.find("def save_exchange_code")
        func = src[idx:idx + 2500]
        assert func.count("_t.time() + 60") == 2, \
            "both INSERT paths must use a 60-second expiry window"
        assert "_t.time() + 10" not in func, "old 10s window must be gone"

    def test_uc14_atomic_consume_keeps_expiry_guard(self):
        """UC14: 一次性原子消费不变——DELETE 带 expires > now 条件。"""
        src = _read_src("src/user/db.py")
        idx = src.find("def validate_exchange_code")
        func = src[idx:idx + 1500]
        assert "DELETE FROM exchange_codes WHERE code=? AND expires > ?" in func, \
            "atomic consume DELETE must keep the expiry condition"


class TestUC15InlineExchangeScript:
    """UC15: HTML 解析期内联兑票——注入链路 + app.js flag 接力。"""

    def test_uc15_script_defined_and_injected_before_head_close(self):
        """UC15: _EARLY_EXCHANGE_SCRIPT 存在且经 _inject 注入 </head> 前。"""
        src = _read_src("src/api/main.py")
        assert "_EARLY_EXCHANGE_SCRIPT = '''<script>" in src
        idx = src.find("def _inject_email_configured")
        func = src[idx:idx + 3000]
        assert "html.replace('</head>', _EARLY_EXCHANGE_SCRIPT + '</head>', 1)" in func, \
            "inject must land before </head> so it runs before any defer JS"

    def test_uc15_script_posts_exchange_and_strips_url(self):
        """UC15: 脚本 POST /api/auth/exchange + replaceState strip URL。"""
        src = _read_src("src/api/main.py")
        idx = src.find("_EARLY_EXCHANGE_SCRIPT = ")
        script = src[idx:idx + 2600]
        assert "fetch('/api/auth/exchange'" in script
        assert "method: 'POST'" in script
        assert "history.replaceState" in script, \
            "code must be stripped from URL immediately (bar/history/referer)"
        assert "localStorage.setItem('token', d.token)" in script

    def test_uc15_script_sets_relay_flags(self):
        """UC15: 成功/失败都通过 sessionStorage flag 接力给 app.js。"""
        src = _read_src("src/api/main.py")
        idx = src.find("_EARLY_EXCHANGE_SCRIPT = ")
        script = src[idx:idx + 2600]
        for flag in ("__oauth_inline_login__", "__oauth_inline_first_login__",
                     "__oauth_exchange_failed__"):
            assert flag in script, f"relay flag {flag} missing in inline script"

    def test_uc15_app_js_success_relay_block(self):
        """UC15: app.js init() 成功接力块补 DOM/I18N/API 依赖。"""
        src = _read_file("js/app.js")
        assert "sessionStorage.getItem('__oauth_inline_login__')" in src
        assert "API.scheduleProactiveRefresh()" in src
        # 接力块必须在原 exchangeCode 处理分支之前（URL 已被 strip，走 flag）；
        # 参数读取本身在前是合理的（读参→flag 接力→老分支）
        assert src.find("__oauth_inline_login__") < src.find("if (exchangeCode) {")

    def test_uc15_app_js_failure_relay_block(self):
        """UC15: 失败兜底 toast + showLogin，与原兑票失败同款 UX。"""
        src = _read_file("js/app.js")
        assert "sessionStorage.getItem('__oauth_exchange_failed__')" in src
        assert "app.login.oauth_exchange_failed" in src

    def test_uc15b_exchange_failed_key_in_all_three_i18n_sources(self):
        """UC15b: oauth_exchange_failed 三层同步（zh/en locale + 内置兜底）。"""
        for lang in ("zh", "en"):
            pack = _read_json(f"locales/{lang}/app.json")
            assert pack["login"]["oauth_exchange_failed"], f"{lang} full pack missing key"
        inline = _read_file("js/i18n.js")
        assert inline.count("oauth_exchange_failed:") == 2, \
            "inline fallback packs (zh+en) must both carry the key"

    def test_uc15b_cache_bust_versions_bumped(self):
        """UC15b: app.js 与 i18n.js 引用页 ?v= 递增（锁当前值防回退）。"""
        html = _read_file("index.html")
        assert re.search(r'app\.js\?v=210"', html), "app.js cache-bust must be v207"
        assert re.search(r'i18n\.js\?v=20261030-4"', html), "index i18n.js bust must be 20261030-4"
        for page, ver in (("forgot.html", 18), ("register.html", 18)):
            src = _read_file(page)
            assert re.search(rf'i18n\.js\?v={ver}"', src), f"{page} i18n.js bust must be v{ver}"
        assert re.search(r'i18n\.js\?v=20261016-3"', _read_file("admin.html")), \
            "admin.html i18n.js bust must be 20261016-3"


class TestUC15cFailureDetailRelay:
    """UC15c: 内联失败路径透传后端 detail（对齐原分支 R5），不留降级尾巴。"""

    def test_uc15c_inline_script_parses_error_detail(self):
        """UC15c: 内联脚本解析 r.json() 的 detail.message/error_code 并抛出。"""
        src = _read_src("src/api/main.py")
        idx = src.find("_EARLY_EXCHANGE_SCRIPT = ")
        script = src[idx:idx + 2000]
        assert "return r.json().catch(function(){ return null; })" in script
        assert "d.message || d.error_code" in script, \
            "inline failure path must surface backend-specific reason"

    def test_uc15c_failure_msg_flag_stored(self):
        """UC15c: 失败时把详情存 __oauth_exchange_failed_msg__ 供接力块消费。"""
        src = _read_src("src/api/main.py")
        idx = src.find("_EARLY_EXCHANGE_SCRIPT = ")
        script = src[idx:idx + 2600]
        assert "__oauth_exchange_failed_msg__" in script

    def test_uc15c_app_js_prefers_detail_over_generic(self):
        """UC15c: app.js 失败接力块优先展示后端详情，无则回退 i18n 通用文案。"""
        src = _read_file("js/app.js")
        assert "sessionStorage.getItem('__oauth_exchange_failed_msg__')" in src
        # 锚定 msg 消费语句（UC15f 等待块也引用这两个 flag，避免误锚）
        idx = src.find("sessionStorage.getItem('__oauth_exchange_failed_msg__')")
        relay = src[idx:idx + 500]
        assert "removeItem('__oauth_exchange_failed_msg__')" in relay
        idx2 = src.find("if (!exchangeCode && sessionStorage.getItem('__oauth_exchange_failed__'))")
        relay2 = src[idx2:idx2 + 700]
        assert "_exMsg ||" in relay2, "toast must prefer the backend detail message"


class TestUC15dPopupLoginConsumer:
    """UC15d: popup 登录 postMessage 消费方补齐，休眠路径激活。"""

    def test_uc15d_listener_registered_with_idempotent_guard(self):
        """UC15d: app.js 注册 action='login' 消息监听，幂等守卫防重复注册。"""
        src = _read_file("js/app.js")
        assert "window.__oauthPopupLoginListener" in src, \
            "idempotent guard must prevent duplicate listeners across init() calls"
        assert "d.action !== 'login'" in src

    def test_uc15d_listener_validates_origin_and_type(self):
        """UC15d: origin 校验 + type 白名单（与 account.js 监听器同款防护）。"""
        src = _read_file("js/app.js")
        idx = src.find("window.__oauthPopupLoginListener = true")
        block = src[idx:idx + 1200]
        assert "event.origin !== window.location.origin" in block
        assert "d.type !== 'oauth_result'" in block

    def test_uc15d_success_path_redeems_and_reloads(self):
        """UC15d: 成功路径兑票→存 token→清缓存→整页重载走回访用户路径（语言前缀保留，UC15g）。"""
        src = _read_file("js/app.js")
        idx = src.find("window.__oauthPopupLoginListener = true")
        block = src[idx:idx + 2200]
        assert "d.exchange_code" in block
        assert "location.replace(" in block, "must reload after redemption"
        assert "localStorage.removeItem('repo_member_cache')" in block

    def test_uc15d_failure_path_parses_detail_too(self):
        """UC15d: 监听器失败路径同样解析后端 detail（与 UC15c 一致）。"""
        src = _read_file("js/app.js")
        idx = src.find("window.__oauthPopupLoginListener = true")
        block = src[idx:idx + 2000]
        assert "det.message || det.error_code" in block


class TestUC16JwtSecretPersistence:
    """S9（UC16）：JWT 签名密钥持久化——代码层结构与门禁红绿（行为级）。

    Ops 载体（security.json / .jwt_secret / start-grape.sh）不入库，
    属部署验收（spec UC16d 金标准），此处锁代码层的可回归面。
    """

    def test_uc16_ephemeral_fallback_is_last_resort_only(self):
        """UC16: 随机兜底必须是最后回退（kid 池 → 强 JWT_SECRET → 随机），两层在前。"""
        src = _read_src("src/api/jwt_utils.py")
        idx = src.find("def _get_signing_secret")
        func = src[idx:idx + 700]
        pool = func.find("if _SIGNING_SECRET and _SIGNING_KID:")
        env = func.find("if _is_strong_secret(JWT_SECRET):")
        eph = func.find("_ensure_dev_ephemeral_secret()")
        assert 0 <= pool < env < eph, \
            "fallback order must be kid pool -> strong JWT_SECRET -> ephemeral"

    def test_uc16_verify_fallback_keeps_legacy_no_kid_branch(self):
        """UC16: 无 kid 旧 token 回退 JWT_SECRET 的兼容分支保留（向前兼容）。"""
        src = _read_src("src/api/jwt_utils.py")
        idx = src.find("def _get_verify_secret")
        func = src[idx:idx + 1200]
        body = func[func.find('"""', func.find('"""') + 3) + 3:]  # skip docstring
        assert "if _is_strong_secret(JWT_SECRET):" in body
        assert "_ensure_dev_ephemeral_secret()" in body
        # 有 kid 且池中查无 → 拒绝（None），不静默回退
        assert "return _SECRET_KEYS.get(kid)" in body

    def test_uc16_production_gate_rejects_weak_config(self):
        """UC16: 生产门禁红测——无密钥 + production → validate False（拒绝启动）。"""
        import subprocess, sys, os
        venv_py = sys.executable
        code = (
            "from src.auth import security_config as sc\n"
            "sc._config = {'env': 'production'}\n"
            "from src.api import jwt_utils\n"
            "jwt_utils._SECRET_KEYS = {}\n"
            "jwt_utils._SIGNING_KID = ''\n"
            "jwt_utils._SIGNING_SECRET = ''\n"
            "jwt_utils.JWT_SECRET = ''\n"
            "import sys\n"
            "sys.exit(0 if not jwt_utils.validate_jwt_secret() else 1)\n"
        )
        r = subprocess.run([venv_py, "-c", code], cwd=os.path.join(os.path.dirname(__file__), ".."),
                           capture_output=True, text=True,
                           env={**os.environ, "JWT_SECRET": ""})
        assert r.returncode == 0, f"gate must reject weak config in production: {r.stderr[-300:]}"

    def test_uc16_production_gate_accepts_strong_secret(self):
        """UC16: 生产门禁绿测——强 JWT_SECRET → validate True。"""
        import subprocess, sys, os
        venv_py = sys.executable
        code = (
            "from src.auth import security_config as sc\n"
            "sc._config = {'env': 'production'}\n"
            "from src.api import jwt_utils\n"
            "import sys\n"
            "sys.exit(0 if jwt_utils.validate_jwt_secret() else 1)\n"
        )
        r = subprocess.run([venv_py, "-c", code], cwd=os.path.join(os.path.dirname(__file__), ".."),
                           capture_output=True, text=True,
                           env={**os.environ, "JWT_SECRET": "a" * 64})
        assert r.returncode == 0, f"gate must accept strong secret: {r.stderr[-300:]}"

    def test_uc16_dev_ephemeral_allowed_but_warned_in_log(self):
        """UC16: dev 无密钥允许随机兜底但必须打告警（运维检查点，spec UC16c 的日志锚）。"""
        src = _read_src("src/api/jwt_utils.py")
        idx = src.find("def _ensure_dev_ephemeral_secret")
        func = src[idx:idx + 600]
        assert "log.warning" in func, "ephemeral fallback must log a warning (ops detection anchor)"

    def test_uc16_security_config_prefers_file_over_env(self):
        """UC16a: security.json 文件通道优先于环境变量（配置层级与 spec 一致）。"""
        src = _read_src("src/auth/security_config.py")
        idx = src.find("def get_credential_keys")
        func = src[idx:idx + 800]
        # 代码顺序：先取 cfg.get(...credential_keys)（文件），val 为空才走 JWT_SECRET 派生
        cfg_get = func.find('cfg.get("credential_keys"')
        env_fallback = func.find('os.environ.get("JWT_SECRET"')
        assert 0 <= cfg_get < env_fallback, \
            "file (security.json) must take precedence over env fallback"


class TestUC16ePollingStopsOnKick:
    """UC16e: 被踢回登录页时停后台轮询（修「数据悄悄不动、不引导重登」）。"""

    def test_uc16e_showlogin_clears_refresh_timer(self):
        """UC16e: showLogin 统一收口清理 _refreshTimer（所有踢人路径覆盖）。"""
        src = _read_file("js/app.js")
        idx = src.find("function showLogin")
        func = src[idx:idx + 900]
        assert "clearInterval(_refreshTimer)" in func, \
            "showLogin must stop the dashboard polling timer on kick-to-login"

    def test_uc16e_timer_declared_before_showlogin(self):
        """UC16e: _refreshTimer 声明置于 showLogin 之前（防 TDZ，防未来重构踩雷）。"""
        src = _read_file("js/app.js")
        decl = src.find("let _refreshTimer")
        fn = src.find("function showLogin")
        assert 0 < fn < decl, \
            "function may be hoisted but a future sync early call would hit the TDZ"
        # 声明前必须有前置守卫注释（同文件约定）
        assert "UC16e" in src[decl - 120:decl]

    def test_uc16e_cache_bust_bumped(self):
        """UC16e: app.js ?v= 递增至 207。"""
        html = _read_file("index.html")
        assert re.search(r'app\.js\?v=210"', html), "app.js cache-bust must be v207"


class TestUC15fInlinePendingRace:
    """UC15f: 内联兑票慢于 app.js init 的竞态——pending 标记 + 等待窗口。"""

    def test_uc15f_inline_script_sets_pending_synchronously(self):
        """UC15f: 内联脚本在发起 fetch 前同步置 pending（strip URL 之前同块）。"""
        src = _read_src("src/api/main.py")
        idx = src.find("_EARLY_EXCHANGE_SCRIPT = ")
        script = src[idx:idx + 2000]
        pending_set = script.find("setItem('__oauth_inline_pending__'")
        strip = script.find("history.replaceState")
        fetch_call = script.find("fetch('/api/auth/exchange'")
        assert 0 < pending_set < strip < fetch_call, \
            "pending must be set before URL strip and the fetch call"

    def test_uc15f_pending_cleared_on_both_outcomes(self):
        """UC15f: 成功与失败终态都摘 pending（app.js 等待窗口的退出条件）。"""
        src = _read_src("src/api/main.py")
        idx = src.find("_EARLY_EXCHANGE_SCRIPT = ")
        script = src[idx:idx + 2400]
        assert script.count("removeItem('__oauth_inline_pending__')") == 2, \
            "both .then and .catch must clear the pending marker"

    def test_uc15f_app_js_waits_for_pending_with_timeout(self):
        """UC15f: app.js init 见 pending 轮询等终态 flag，5s 兜底防死等。"""
        src = _read_file("js/app.js")
        assert "sessionStorage.getItem('__oauth_inline_pending__')" in src
        idx = src.find("getItem('__oauth_inline_pending__')")
        block = src[idx:idx + 900]
        assert "__oauth_inline_login__" in block and "__oauth_exchange_failed__" in block, \
            "wait loop must exit on either terminal flag"
        assert "5000" in block, "bounded wait — no unbounded hang"

    def test_uc15f_cache_bust_v208(self):
        """UC15f: app.js ?v= 递增至 208。"""
        html = _read_file("index.html")
        assert re.search(r'app\.js\?v=210"', html), "app.js cache-bust must be v208"


class TestUC15fReentryGuard:
    """UC15f 补强：等待块双重重入守卫（interval 与 5s 超时只放行一次）。"""

    def test_uc15f_single_reentry_guard_present(self):
        """UC15f: _pfDone 守卫存在——T1 interval 重入后 5s 超时不再二次 init。"""
        src = _read_file("js/app.js")
        idx = src.find("getItem('__oauth_inline_pending__')")
        block = src[idx:idx + 1200]
        assert "_pfDone" in block, "single-reentry guard must gate both triggers"
        assert block.count("_pfReenter") >= 3, "both interval and timeout route through the guard"

    def test_uc15f_cache_bust_v209(self):
        """UC15f 补强: app.js ?v= 递增至 209。"""
        html = _read_file("index.html")
        assert re.search(r'app\.js\?v=210"', html), "app.js cache-bust must be v209"


class TestUC15gPopupLangPrefix:
    """UC15g: popup 登录成功跳转保留 /en 语言前缀（外审发现）。"""

    def test_uc15g_redirect_preserves_en_prefix(self):
        """UC15g: location.replace 不再硬编码 '/'，英文用户不被打回中文首页。"""
        src = _read_file("js/app.js")
        idx = src.find("window.__oauthPopupLoginListener = true")
        block = src[idx:idx + 2200]
        assert "location.replace('/')" not in block, \
            "hardcoded '/' loses the /en language prefix"
        assert "(_lg === 'en' ? '/en' : '') + '/'" in block, \
            "redirect must derive the prefix from I18N.getLang like the original branch"

    def test_uc15g_cache_bust_v210(self):
        """UC15g: app.js ?v= 递增至 210。"""
        html = _read_file("index.html")
        assert re.search(r'app\.js\?v=210"', html), "app.js cache-bust must be v210"
