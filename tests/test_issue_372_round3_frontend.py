"""Issue #372 第三轮前端深度审计：遗漏问题修复测试。

对应需求文档第十章 T3-01 ~ T3-11 共 11 个测试用例。

覆盖：
- P1 问题一: renderOAuthButtons 未配 OAuth 时隐藏按钮（T3-01, T3-02）
- P2 问题二: register.html 硬编码中文"注册"（T3-03, T3-04）
- P2 问题三: startCountdown 倒计时结束文案错误（T3-05）
- P1 问题四: account.js 绑定弹窗未实现 R7.4（T3-06, T3-07）
- P2 问题五: oauthLogin 提示文案不符合需求（T3-08, T3-09）
- P2 问题七: startCountdown 倒计时破坏 data-i18n（T3-10, T3-11）
- P0 问题八: organizations.js 重复渲染用户下拉导致 ID 冲突（T3-12, T3-13）

测试方式：源码检查（inspect source）+ locale JSON 文件读取验证。
前端 JS 无法直接在 pytest 中执行，因此用源码检查方式验证修复逻辑是否存在。
"""
import json
import os

import pytest


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")


def _read_file(rel_path):
    """读取静态文件内容。"""
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# P1 问题一：renderOAuthButtons 未配 OAuth 时隐藏按钮（T3-01, T3-02）
# ===========================================================================

class TestRenderOAuthButtonsNotHide:
    """T3-01, T3-02: 未配 OAuth App 时不隐藏按钮，oauthLogin 用新提示文案。"""

    def test_t3_01_render_oauth_buttons_not_hide_when_no_configured(self):
        """T3-01: renderOAuthButtons 在 configured.length === 0 时不设置 display='none'。"""
        source = _read_file("js/app.js")
        # 找到 renderOAuthButtons 函数
        assert "function renderOAuthButtons" in source
        # R7.5 修复后不再过滤 configured，改为检查 providers.length
        idx = source.find("function renderOAuthButtons")
        func_source = source[idx:idx + 1200]
        # 不应隐藏 section
        assert "display = 'none'" not in func_source, \
            "renderOAuthButtons should NOT hide section"
        # 应检查 providers.length（不是 configured.length）
        assert "providers.length === 0" in func_source, \
            "renderOAuthButtons should check providers.length (not configured.length)"

    def test_t3_02_oauth_login_uses_hint_text(self):
        """T3-02: oauthLogin catch 时使用 oauth_not_configured_hint 文案。"""
        source = _read_file("js/app.js")
        assert "oauth_not_configured_hint" in source, \
            "oauthLogin catch should use oauth_not_configured_hint"


# ===========================================================================
# P2 问题二：register.html 硬编码中文"注册"（T3-03, T3-04）
# ===========================================================================

class TestRegisterButtonI18n:
    """T3-03, T3-04: 无邮箱配置时按钮走 i18n 而非硬编码中文。"""

    def test_t3_03_register_btn_sets_data_i18n_to_register_btn(self):
        """T3-03: register.html 无邮箱配置时按钮 data-i18n 设为 app.login.register_btn。"""
        source = _read_file("register.html")
        # 找到无邮箱配置按钮修改逻辑
        assert "register_btn" in source, \
            "register.html should set data-i18n to app.login.register_btn"
        # 不应再有硬编码 textContent='注册'
        assert "textContent='注册'" not in source, \
            "register.html should NOT hardcode textContent='注册'"
        assert 'textContent=\"注册\"' not in source, \
            "register.html should NOT hardcode textContent=\"注册\""

    def test_t3_04_register_btn_uses_i18n(self):
        """T3-04: register.html 按钮文案走 i18n（data-i18n="app.login.register_btn"）。

        R7.7 修复后：按钮初始就是 data-i18n="app.login.register_btn"（无邮箱模式默认），
        不再需要在 checkEmailConfigured 中动态设置。
        """
        source = _read_file("register.html")
        # 按钮初始 data-i18n 应为 register_btn（无邮箱模式默认）
        assert 'data-i18n="app.login.register_btn"' in source, \
            "register button should have data-i18n='app.login.register_btn' (initial no-email mode)"
        # 不应有硬编码中文 textContent='注册'
        assert "textContent='注册'" not in source, \
            "register.html should NOT hardcode textContent='注册'"
        assert 'textContent="注册"' not in source, \
            "register.html should NOT hardcode textContent=\"注册\""


# ===========================================================================
# P2 问题三：startCountdown 倒计时结束文案错误（T3-05）
# ===========================================================================

class TestStartCountdownEndText:
    """T3-05: 倒计时结束后按钮文案为 resend_code，不是 toast_code_resent。"""

    def test_t3_05_countdown_end_not_use_toast_code_resent(self):
        """T3-05: startCountdown 倒计时结束后不使用 toast_code_resent 作为按钮文案。"""
        source = _read_file("js/auth.js")
        # 找到 startCountdown 函数
        assert "function startCountdown" in source
        idx = source.find("function startCountdown")
        func_source = source[idx:idx + 600]  # 函数体大约 600 字符
        # 不应直接用 toast_code_resent 作为按钮文案
        assert "toast_code_resent" not in func_source, \
            "startCountdown should NOT use toast_code_resent as button text after countdown"
        # 应该恢复原始 data-i18n + 对应文案
        assert "data-i18n" in func_source or "_origI18n" in func_source, \
            "startCountdown should restore original data-i18n after countdown"


# ===========================================================================
# P1 问题四：account.js 绑定弹窗未实现 R7.4（T3-06, T3-07）
# ===========================================================================

class TestAccountBindProviderR74:
    """T3-06, T3-07: 未配 OAuth App 时只显示手动令牌输入。"""

    def test_t3_06_bind_provider_checks_configured(self):
        """T3-06: _accountBindProvider 检查 provider configured 状态。"""
        source = _read_file("js/pages/account.js")
        assert "_accountBindProvider" in source
        # 验证检查 configured 逻辑存在
        assert "_oauthConfigured" in source or "configured" in source, \
            "_accountBindProvider should check provider configured status"
        # 验证未配时不生成 bindTabOauth
        assert "bindPanelToken" in source, \
            "_accountBindProvider should have bindPanelToken for manual token input"

    def test_t3_07_bind_provider_configured_true_shows_oauth_tab(self):
        """T3-07: _accountBindProvider configured=true 时生成 OAuth tab（回归保护）。"""
        source = _read_file("js/pages/account.js")
        assert "_accountBindProvider" in source
        # 验证有 OAuth tab 逻辑（configured=true 分支）
        assert "bindTabOauth" in source, \
            "_accountBindProvider should have bindTabOauth when configured=true"
        assert "bindPanelOauth" in source, \
            "_accountBindProvider should have bindPanelOauth when configured=true"


# ===========================================================================
# P2 问题五：oauthLogin 提示文案不符合需求（T3-08, T3-09）
# ===========================================================================

class TestOauthNotConfiguredHint:
    """T3-08, T3-09: locale 文件包含 oauth_not_configured_hint 且文案符合 R7.5。"""

    def test_t3_08_zh_locale_has_hint_with_username_password(self):
        """T3-08: zh/app.json 包含 oauth_not_configured_hint 且文案含"用户名+密码"。"""
        data = json.loads(_read_file("locales/zh/app.json"))
        login = data.get("login", {})
        assert "oauth_not_configured_hint" in login, \
            "zh login.oauth_not_configured_hint missing"
        hint = login["oauth_not_configured_hint"]
        assert "用户名" in hint and "密码" in hint, \
            f"zh hint should mention 用户名+密码, got: {hint}"

    def test_t3_09_en_locale_has_hint_with_username_password(self):
        """T3-09: en/app.json 包含 oauth_not_configured_hint 且文案含 "username + password"。"""
        data = json.loads(_read_file("locales/en/app.json"))
        login = data.get("login", {})
        assert "oauth_not_configured_hint" in login, \
            "en login.oauth_not_configured_hint missing"
        hint = login["oauth_not_configured_hint"]
        assert "username" in hint.lower() and "password" in hint.lower(), \
            f"en hint should mention username + password, got: {hint}"


# ===========================================================================
# P2 问题七：startCountdown 倒计时破坏 data-i18n（T3-10, T3-11）
# ===========================================================================

class TestStartCountdownDataI18nProtection:
    """T3-10, T3-11: 倒计时期间移除 data-i18n，结束后恢复。"""

    def test_t3_10_countdown_removes_data_i18n_during_countdown(self):
        """T3-10: startCountdown 倒计时期间按钮 data-i18n 被移除。"""
        source = _read_file("js/auth.js")
        assert "function startCountdown" in source
        idx = source.find("function startCountdown")
        func_source = source[idx:idx + 800]
        # 验证倒计时开始时移除 data-i18n
        assert "removeAttribute" in func_source and "data-i18n" in func_source, \
            "startCountdown should removeAttribute('data-i18n') during countdown"

    def test_t3_11_countdown_restores_data_i18n_after_end(self):
        """T3-11: startCountdown 倒计时结束后 data-i18n 恢复为原始值。"""
        source = _read_file("js/auth.js")
        assert "function startCountdown" in source
        idx = source.find("function startCountdown")
        func_source = source[idx:idx + 800]
        # 验证保存原始 data-i18n
        assert "_origI18n" in func_source, \
            "startCountdown should save original data-i18n (_origI18n)"
        # 验证结束后恢复
        assert "setAttribute" in func_source and "data-i18n" in func_source, \
            "startCountdown should setAttribute('data-i18n', _origI18n) after countdown ends"


# ===========================================================================
# 第六章：平台入口始终可见（T6-01, T6-02）
# ===========================================================================

class TestProviderAlwaysVisible:
    """T6-01, T6-02: 账户设置页 provider 列表不过滤 configured。"""

    def test_t6_01_account_provider_list_no_configured_filter(self):
        """T6-01: account.js provider 列表渲染不过滤 configured（所有 provider 都显示）。"""
        source = _read_file("js/pages/account.js")
        # 找到 provider 列表渲染逻辑
        assert "providersData.providers" in source
        # 不应过滤 configured
        assert "filter(function(p) { return p.configured; })" not in source, \
            "account.js should NOT filter providers by configured status"
        # 应该直接使用全部 providers
        assert "providersData.providers || []" in source, \
            "account.js should use all providers without filtering"

    def test_t6_02_unconfigured_provider_still_visible(self):
        """T6-02: 未配 OAuth App 时 GitCode/GitHub 仍出现在第三方账号列表中。"""
        source = _read_file("js/pages/account.js")
        # _accountBindProvider 已有 R7.4 检查（configured=false 时走手动令牌）
        assert "_oauthConfigured" in source, \
            "_accountBindProvider should check _oauthConfigured for R7.4"
        # provider 列表不过滤 → 未配平台也显示 → 点击时 _accountBindProvider 走手动令牌
        assert "filter" not in source.split("providersData.providers || []")[1][:100] if "providersData.providers || []" in source else True, \
            "No filter after providers assignment"


# ===========================================================================
# 第六章扩展：新手引导页 + 登录页部分配置场景（T6-03, T6-04）
# ===========================================================================

class TestBindingGuideAndRenderAllProviders:
    """T6-03, T6-04: 新手引导页 + 登录页不过滤 configured。"""

    def test_t6_03_binding_guide_no_configured_filter(self):
        """T6-03: _renderBindingGuide 不过滤 configured（新手引导页显示所有平台）。"""
        source = _read_file("js/app.js")
        assert "_renderBindingGuide" in source
        # 找到 _renderBindingGuide 函数体
        idx = source.find("function _renderBindingGuide")
        func_source = source[idx:idx + 500]
        # 不应过滤 configured
        assert "filter(function(p) { return p.configured; })" not in func_source, \
            "_renderBindingGuide should NOT filter providers by configured"
        # 应直接使用全部 providers
        assert "d.providers || []" in func_source, \
            "_renderBindingGuide should use all providers without filtering"

    def test_t6_04_render_oauth_buttons_uses_all_providers(self):
        """T6-04: renderOAuthButtons 用全部 providers（不丢失部分配置场景的按钮）。"""
        source = _read_file("js/app.js")
        assert "function renderOAuthButtons" in source
        idx = source.find("function renderOAuthButtons")
        func_source = source[idx:idx + 1200]
        # 不应过滤 configured
        assert "filter(function(p){return p.configured})" not in func_source, \
            "renderOAuthButtons should NOT filter providers by configured"
        # 用 providers（全部）而非 configured（过滤后）做渲染
        assert "providers.map" in func_source, \
            "renderOAuthButtons should use providers.map (all providers) for rendering"
        assert "providers.length" in func_source, \
            "renderOAuthButtons should check providers.length (not configured.length)"


# ===========================================================================
# 第十四章：用户菜单统一（T7-01~05）
# ===========================================================================

class TestUnifiedUserMenu:
    """T7-01~05: 所有页面用户菜单统一（反馈 + 帮助 + 用户设置下拉）。"""

    def test_t7_01_sidebar_menu_has_feedback_and_help(self):
        """T7-01: 侧边栏 _loadUserMenu 包含反馈 + 帮助菜单项。"""
        source = _read_file("js/app.js")
        assert "_buildUserMenuItems" in source, \
            "should have _buildUserMenuItems unified menu builder"
        assert "openFeedbackDialog" in source, \
            "menu should have feedback entry (openFeedbackDialog)"
        assert "app.openHelp" in source, \
            "menu should have help entry (app.openHelp)"

    def test_t7_02_lobby_menu_has_security_feedback_help(self):
        """T7-02: Lobby _loadLobbyUserMenu 包含账号安全 + 反馈 + 帮助。"""
        source = _read_file("js/app.js")
        assert "_loadLobbyUserMenu" in source
        idx = source.find("function _loadLobbyUserMenu")
        func_source = source[idx:idx + 200]
        # 应调 _buildUserMenuItems(true)（包含账号安全）
        assert "_buildUserMenuItems(true)" in func_source, \
            "_loadLobbyUserMenu should call _buildUserMenuItems(true)"

    def test_t7_03_organizations_page_uses_toolbar_user_dropdown(self):
        """T7-03: 组织管理页不重复渲染用户下拉（toolbar 全局用户下拉已覆盖）。

        修复前：organizations.js 在 pageContent 内渲染了 orgUserDropdown + tbUserMenu，
        与 index.html toolbar 中的 tbUserMenu ID 冲突。
        修复后：删除页面内重复渲染，toolbar 的 tbUserBtn 在所有页面可见。
        """
        source = _read_file("js/pages/organizations.js")
        assert "orgUserDropdown" not in source, \
            "organizations page should not render orgUserDropdown (toolbar has global user dropdown)"
        assert "_toggleTbUserMenu" not in source, \
            "organizations page should not call _toggleTbUserMenu (toolbar already has it)"

    def test_t7_04_toolbar_has_user_dropdown(self):
        """T7-04: 工作区 Toolbar 有用户设置下拉框（tbUserBtn）。"""
        source = _read_file("index.html")
        assert "tbUserBtn" in source, \
            "toolbar should have tbUserBtn"
        assert "tbUserMenu" in source, \
            "toolbar should have tbUserMenu dropdown container"
        assert "_toggleTbUserMenu" in source, \
            "tbUserBtn should call _toggleTbUserMenu"

    def test_t7_05_all_menus_use_unified_builder(self):
        """T7-05: 所有用户下拉框菜单项一致（通过 _buildUserMenuItems 统一生成）。"""
        source = _read_file("js/app.js")
        idx = source.find("function _buildUserMenuItems")
        func_source = source[idx:idx + 1500]
        assert "openFeedbackDialog" in func_source, \
            "unified menu should have feedback"
        assert "app.openHelp" in func_source, \
            "unified menu should have help"
        assert "app.logout" in func_source, \
            "unified menu should have logout"
        assert "_buildUserMenuItems(true)" in source, \
            "both _loadUserMenu and _loadLobbyUserMenu should call _buildUserMenuItems(true)"

    def test_t7_06_toolbar_no_duplicate_feedback_help(self):
        """T7-06: toolbar 不再有独立的反馈/帮助按钮（统一到用户下拉菜单）。"""
        html = _read_file("index.html")
        assert 'id="tbFeedback"' not in html, \
            "toolbar should not have tbFeedback (moved to user dropdown menu)"
        assert 'class="help-btn"' not in html, \
            "sidebar should not have help-btn (moved to user dropdown menu)"


# ===========================================================================
# T3-12, T3-13: organizations.js 重复用户下拉 ID 冲突修复
# ===========================================================================

class TestOrganizationsNoDuplicateUserMenu:
    """测试 organizations.js 不再重复渲染用户下拉（ID 冲突修复）。"""

    def test_t3_12_no_duplicate_tbUserMenu_in_organizations(self):
        """T3-12: organizations.js 不再渲染 id="tbUserMenu"（toolbar 已有全局用户下拉）。

        根因：renderOrganizations 在 pageContent 内部渲染了一个 tbUserMenu div，
        与 index.html toolbar 中的 tbUserMenu ID 冲突。
        getElementById 只返回第一个（toolbar），页面内的按钮和菜单不关联。
        """
        org_js = _read_file("js/pages/organizations.js")
        # 不应在 organizations.js 中渲染 id="tbUserMenu"
        assert 'id="tbUserMenu"' not in org_js, \
            "organizations.js should not render tbUserMenu (ID conflict with toolbar)"
        # 不应渲染 orgUserDropdown（已删除的重复下拉容器）
        assert 'id="orgUserDropdown"' not in org_js, \
            "organizations.js should not render orgUserDropdown (removed)"

    def test_t3_13_no_toggleTbUserMenu_in_organizations(self):
        """T3-13: organizations.js 不再调 app._toggleTbUserMenu()（toolbar 全局用户下拉已覆盖）。"""
        org_js = _read_file("js/pages/organizations.js")
        assert "app._toggleTbUserMenu" not in org_js, \
            "organizations.js should not call app._toggleTbUserMenu (toolbar already has it)"
        # 仍保留创建组织按钮
        assert "_showCreateOrgDialog" in org_js, \
            "organizations.js should still have create org button"


# ===========================================================================
# T3-14~17: 注册页无邮箱配置时无闪动
# ===========================================================================

class TestRegisterPageNoFlicker:
    """测试注册页无邮箱配置时不闪动（初始隐藏，有邮箱时才显示）。"""

    def test_t3_14_email_field_initially_hidden(self):
        """T3-14: register.html 邮箱字段初始 display:none（无邮箱模式默认）。"""
        html = _read_file("register.html")
        # emailField 应有 style="display:none"
        assert 'id="emailField" style="display:none"' in html or \
               'id="emailField" style="display: none"' in html, \
            "emailField should be initially hidden (display:none)"

    def test_t3_15_step_indicator_initially_hidden(self):
        """T3-15: register.html 步骤指示器初始 display:none。"""
        html = _read_file("register.html")
        assert 'id="stepIndicator" style="display:none"' in html or \
               'id="stepIndicator" style="display: none"' in html, \
            "stepIndicator should be initially hidden (display:none)"

    def test_t3_16_check_email_shows_elements_when_configured(self):
        """T3-16: checkEmailConfigured 返回 email_configured=true 时显示邮箱字段。"""
        html = _read_file("register.html")
        # 有邮箱时移除 display:none（设为空字符串）
        assert "$('emailField').style.display=''" in html or \
               '$(\'emailField\').style.display = \'\'' in html or \
               "emailField').style.display=''" in html, \
            "checkEmailConfigured should show emailField when email_configured=true"
        # 按钮改为"发送验证码"
        assert "app.login.send_code" in html, \
            "button should change to send_code when email configured"

    def test_t3_17_check_email_keeps_hidden_on_failure(self):
        """T3-17: checkEmailConfigured 返回失败时保持初始隐藏状态（无闪动）。"""
        html = _read_file("register.html")
        # catch 中应保持隐藏（不显示邮箱字段）
        # 查找 catch 块中的逻辑
        catch_idx = html.find(".catch(function()")
        if catch_idx > 0:
            catch_block = html[catch_idx:catch_idx + 300]
            # catch 中不应显示邮箱字段
            assert "emailField').style.display=''" not in catch_block, \
                "catch block should not show emailField (keep hidden on API failure)"

    def test_t3_18_index_forgot_link_initially_hidden(self):
        """T3-18: index.html 忘记密码链接初始 display:none（避免闪动）。"""
        html = _read_file("index.html")
        # forgotLink 应有 style="display:none"（可能与 data-i18n 在同一行）
        assert 'id="forgotLink"' in html and 'display:none' in html.split('id="forgotLink"')[1][:100], \
            "forgotLink should be initially hidden (display:none)"
        assert 'id="forgotDot"' in html and 'display:none' in html.split('id="forgotDot"')[1][:50], \
            "forgotDot should be initially hidden (display:none)"

    def test_t3_19_index_forgot_link_shown_when_email_configured(self):
        """T3-19: index.html 有邮箱配置时显示忘记密码链接。"""
        html = _read_file("index.html")
        # 有邮箱时移除 display:none（设为空字符串）
        assert "display=''" in html or "display = ''" in html, \
            "forgotLink should be shown when email_configured=true (remove display:none)"

    def test_t3_20_account_email_configured_initially_false(self):
        """T3-20: account.js _emailConfigured 初始 false（无邮箱模式默认）。"""
        js = _read_file("js/pages/account.js")
        # 应有初始 var _emailConfigured = false
        assert "var _emailConfigured = false" in js or \
               "_emailConfigured = false" in js, \
            "_emailConfigured should be initially false (no-email mode default)"

    def test_t3_21_forgot_page_step1_initially_hidden(self):
        """T3-21: forgot.html Step1 初始 display:none（避免无邮箱场景闪动）。"""
        html = _read_file("forgot.html")
        assert 'id="forgotStep1"' in html and 'display:none' in html.split('id="forgotStep1"')[1][:100], \
            "forgotStep1 should be initially hidden (display:none)"

    def test_t3_22_forgot_page_shows_form_when_email_configured(self):
        """T3-22: forgot.html 有邮箱配置时显示表单。"""
        html = _read_file("forgot.html")
        assert "forgotStep1').style.display='block'" in html or \
               "forgotStep1').style.display = 'block'" in html, \
            "forgotStep1 should be shown when email_configured=true"

    def test_t3_23_forgot_page_shows_message_when_no_email(self):
        """T3-23: forgot.html 无邮箱配置时显示提示（非表单）。"""
        html = _read_file("forgot.html")
        assert "forgot_email_not_configured" in html, \
            "forgot.html should show forgot_email_not_configured message when no email"

    def test_t3_24_forgot_email_not_configured_i18n_exists(self):
        """T3-24: forgot_email_not_configured i18n key 在 zh/en locale 文件中存在。"""
        import json
        for lang in ("zh", "en"):
            path = os.path.join(_STATIC_DIR, "locales", lang, "app.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            assert "login" in data, f"login namespace missing in {lang}"
            assert "forgot_email_not_configured" in data["login"], \
                f"forgot_email_not_configured missing in {lang}/app.json login namespace"
