/**
 * i18n.js — Grape Internationalization Core Module
 *
 * IIFE pattern (not self-executing). Call I18N.init(cacheVer) from index.html
 * after the script loads, before app.js init.
 *
 * Features:
 * - SUPPORTED = ['zh', 'en'], DEFAULT_LANG = 'zh'
 * - URL pathname prefix detection (/en → en, else → zh)
 * - localStorage + cookie dual-write for language persistence
 * - Async fetch of 16 module JSON language packs
 * - Inline minimal packs for synchronous first paint (common + app.login + app.repo)
 * - {{name}} variable interpolation
 * - Fallback to default language (zh) when key missing in current language
 * - DOM element replacement via [data-i18n] attributes
 *
 * Usage:
 *   I18N.init(cacheVer)  // sync init with inline packs, async load full packs
 *   I18N.t('common.loading')
 *   I18N.t('app.login.username')
 *   I18N.setLang('en')  // switches language + reloads with /en prefix
 *   I18N.getLang()      // returns 'zh' or 'en'
 */
var I18N = (function () {
  'use strict';

  var SUPPORTED = ['zh', 'en'];
  var DEFAULT_LANG = 'zh';
  var STORAGE_KEY = 'grapeLang';

  // 16 module names — each corresponds to a JSON file in locales/<lang>/
  var MODULES = [
    'common', 'app', 'settings', 'knowledge', 'skills',
    'chat', 'events', 'agents', 'pipeline', 'runtimes',
    'tasks', 'issues', 'repos', 'dashboard', 'observability', 'templates',
    'organizations', 'autopilot', 'admin'
  ];

  var _currentLang = DEFAULT_LANG;
  var _packs = {};          // { zh: {common: {...}, app: {...}, ...}, en: {...} }
  var _loadedLangs = {};    // { zh: true, en: false } — whether full packs loaded
  var _cacheVer = '';

  // ── Inline minimal packs for synchronous first paint ──
  // Covers common.* + app.login.* + app.repo.* — enough for login screen + repo selection
  var _INLINE_PACKS = {
    zh: {
      common: {
        loading: '加载中...',
        load_failed: '加载失败',
        save: '保存',
        cancel: '取消',
        confirm: '确认',
        delete: '删除',
        login: '登录',
        logout: '登出',
        search: '搜索',
        no_data: '暂无数据',
        error: '错误',
        success: '成功',
        back: '返回',
        create: '创建',
        update: '更新',
        close: '关闭',
        refresh: '刷新'
      },
      app: {
        login: {
          gitcode_login: 'GitCode',
          github_login: 'GitHub',
          beta_apply: '申请内测',
          admin: '管理后台',
          username: '用户名',
          password: '密码',
          login_btn: '登录',
          register: '注册账号',
          forgot_password: '忘记密码',
          back_to_login: '返回登录',
          send_code: '发送验证码',
          resend_code: '重新发送',
          confirm_new_password_placeholder: '再次输入新密码',
          register_btn: '注册',
          reset_password_btn: '重置密码',
          or_oauth: '或使用第三方登录',
          failed: '登录失败',
          oauth_not_configured: 'OAuth未配置，请在管理界面配置',
          empty_input: '请输入用户名和密码',
          register_title: '注册 Grape 账号',
          register_sub: 'Multi-Agent DevOps',
          username_placeholder: '3-32字符，字母、数字、下划线',
          email: '邮箱',
          email_placeholder: 'you@example.com',
          password_placeholder: '至少8个字符',
          code_placeholder: '6位验证码',
          have_account: '已有账号？',
          forgot_title: '重置密码',
          forgot_sub: '输入注册邮箱获取验证码',
          remember: '想起来了？',
          associate_existing: '关联已有账号',
          register_new: '注册新账号',
          associate_btn: '关联并登录',
          account_or_email: '用户名或邮箱',
          title_login: '登录 — Grape',
          title_register: '注册 — Grape',
          title_forgot: '重置密码 — Grape',
          pwd_strength: '密码强度',
          pwd_strength_weak: '弱',
          pwd_strength_medium: '中',
          pwd_strength_strong: '强',
          pwd_req_length: '至少8个字符',
          pwd_req_alnum: '包含字母和数字',
          pwd_req_not_match: '不与用户名/邮箱相同',
          confirm_password: '确认密码',
          confirm_new_password: '确认新密码',
          err_username_required: '请输入用户名',
          err_username_length: '用户名长度3-32字符',
          err_username_format: '仅允许字母、数字、下划线',
          err_email_required: '请输入邮箱',
          err_email_format: '邮箱格式不正确，例如 you@example.com',
          err_password_required: '请输入密码',
          err_password_length: '密码至少8个字符',
          err_confirm_required: '请再次输入密码',
          err_confirm_mismatch: '两次密码输入不一致',
          err_code_required: '请输入验证码',
          err_code_format: '验证码为6位数字',
          err_new_password_required: '请输入新密码',
          err_confirm_new_required: '请再次输入新密码',
          err_identifier_required: '请输入用户名或邮箱',
          toast_code_sent: '验证码已发送到',
          toast_code_resent: '验证码已重新发送',
          toast_send_failed: '发送失败',
          toast_register_failed: '注册失败',
          toast_associate_failed: '关联失败',
          toast_reset_failed: '重置失败',
          toast_register_success: '注册成功',
          toast_associate_success: '关联成功',
          toast_logging_in: '正在登录...',
          toast_reset_success: '密码重置成功',
          toast_use_new_password: '请使用新密码登录',
          toast_password_reset_notice: '密码已重置，请使用新密码登录',
          back_to_edit_email: '← 返回修改邮箱',
          back_to_edit_info: '← 返回修改信息',
          lang_switch_en: 'EN',
          lang_switch_zh: '中文',
          step_fill_info: '填写信息',
          step_verify_code: '验证码',
          step_success: '完成',
          step_email: '邮箱',
          step_code_new_pass: '验证码 + 新密码',
          show_password: '显示密码',
          hide_password: '隐藏密码',
          verify_code: '验证码',
          code_sent_to_email: '验证码将发送到您的邮箱',
          new_password: '新密码',
          select_tab_hint: '选择注册新账号或关联已有账号',
          confirm_password_placeholder: '再次输入密码',
          associate_title: '关联 {provider} 账号',
          oauth_not_associated: '你的 {name}（{login}）尚未关联 Grape 账号',
          step_label_info: '填写信息',
          step_label_verify: '验证码',
          step_label_new_pass: '新密码',
          step_label_done: '完成',
          step_label_email: '邮箱',
          step_next: '下一步'
        },
        repo: {
          select: '选择仓库',
          load_failed: '无法加载仓库列表，请确认 GitCode Token 是否有效'
        },
        help: {
          drawer_title: '📖 使用帮助 & FAQ',
          loading: '加载中...'
        },
        loading: '加载中...'
      },
      admin: {
        title: 'Grape Admin — 配置中心',
        login: { subtitle: '系统管理 · 配置中心', admin_account: '管理员账号', password: '登录密码', login_btn: '登 录', enter_credentials: '请输入账号和密码', back_to_app: '← 返回用户界面' },
        nav: { config_center: '配置中心', credentials: '平台凭据', user_management: '用户管理', user_list: '用户列表', whitelist: '注册白名单', change_password: '修改密码', logout: '退出登录' }
      }
    },
    en: {
      common: {
        loading: 'Loading...',
        load_failed: 'Failed to load',
        save: 'Save',
        cancel: 'Cancel',
        confirm: 'Confirm',
        delete: 'Delete',
        login: 'Login',
        logout: 'Logout',
        search: 'Search',
        no_data: 'No data',
        error: 'Error',
        success: 'Success',
        back: 'Back',
        create: 'Create',
        update: 'Update',
        close: 'Close',
        refresh: 'Refresh'
      },
      app: {
        login: {
          gitcode_login: 'GitCode',
          github_login: 'GitHub',
          beta_apply: 'Apply for Beta',
          admin: 'Admin Console',
          username: 'Username',
          password: 'Password',
          login_btn: 'Sign In',
          register: 'Register',
          forgot_password: 'Forgot Password',
          back_to_login: 'Back to Login',
          send_code: 'Send Code',
          resend_code: 'Resend',
          confirm_new_password_placeholder: 'Enter new password again',
          register_btn: 'Register',
          reset_password_btn: 'Reset Password',
          or_oauth: 'or sign in with',
          failed: 'Login failed',
          oauth_not_configured: 'OAuth not configured, please configure it in the admin console',
          empty_input: 'Please enter username and password',
          register_title: 'Create Grape Account',
          register_sub: 'Multi-Agent DevOps',
          username_placeholder: '3-32 chars, letters, digits, underscore',
          email: 'Email',
          email_placeholder: 'you@example.com',
          password_placeholder: 'At least 8 characters',
          code_placeholder: '6-digit code',
          have_account: 'Already have an account?',
          forgot_title: 'Reset Password',
          forgot_sub: 'Enter your registered email to get a code',
          remember: 'Remembered?',
          associate_existing: 'Link Existing Account',
          register_new: 'Register New Account',
          associate_btn: 'Link & Sign In',
          account_or_email: 'Username or Email',
          title_login: 'Sign In — Grape',
          title_register: 'Register — Grape',
          title_forgot: 'Reset Password — Grape',
          pwd_strength: 'Password Strength',
          pwd_strength_weak: 'Weak',
          pwd_strength_medium: 'Medium',
          pwd_strength_strong: 'Strong',
          pwd_req_length: 'At least 8 characters',
          pwd_req_alnum: 'Contains letters and numbers',
          pwd_req_not_match: 'Not same as username/email',
          confirm_password: 'Confirm Password',
          confirm_new_password: 'Confirm New Password',
          err_username_required: 'Please enter username',
          err_username_length: 'Username must be 3-32 characters',
          err_username_format: 'Only letters, digits, and underscore allowed',
          err_email_required: 'Please enter email',
          err_email_format: 'Invalid email format, e.g. you@example.com',
          err_password_required: 'Please enter password',
          err_password_length: 'Password must be at least 8 characters',
          err_confirm_required: 'Please enter password again',
          err_confirm_mismatch: 'Passwords do not match',
          err_code_required: 'Please enter verification code',
          err_code_format: 'Code must be 6 digits',
          err_new_password_required: 'Please enter new password',
          err_confirm_new_required: 'Please enter new password again',
          err_identifier_required: 'Please enter username or email',
          toast_code_sent: 'Code sent to',
          toast_code_resent: 'Code resent',
          toast_send_failed: 'Send failed',
          toast_register_failed: 'Registration failed',
          toast_associate_failed: 'Link failed',
          toast_reset_failed: 'Reset failed',
          toast_register_success: 'Registration successful',
          toast_associate_success: 'Account linked',
          toast_logging_in: 'Logging in...',
          toast_reset_success: 'Password reset successful',
          toast_use_new_password: 'Please sign in with your new password',
          toast_password_reset_notice: 'Password has been reset. Please sign in with your new password.',
          back_to_edit_email: '← Back to edit email',
          back_to_edit_info: '← Back to edit info',
          lang_switch_en: 'EN',
          lang_switch_zh: '中文',
          step_fill_info: 'Info',
          step_verify_code: 'Verify',
          step_success: 'Done',
          step_email: 'Email',
          step_code_new_pass: 'Code + New Password',
          show_password: 'Show password',
          hide_password: 'Hide password',
          verify_code: 'Verification Code',
          code_sent_to_email: 'A verification code will be sent to your email',
          new_password: 'New Password',
          select_tab_hint: 'Register a new account or link an existing one',
          confirm_password_placeholder: 'Enter password again',
          associate_title: 'Link {provider} Account',
          oauth_not_associated: 'Your {name} ({login}) is not yet linked to a Grape account',
          step_label_info: 'Info',
          step_label_verify: 'Verify',
          step_label_new_pass: 'New Password',
          step_label_done: 'Done',
          step_label_email: 'Email',
          step_next: 'Next'
        },
        repo: {
          select: 'Select Repository',
          load_failed: 'Failed to load repository list. Please check your GitCode Token.'
        },
        help: {
          drawer_title: '📖 Help & FAQ',
          loading: 'Loading...'
        },
        loading: 'Loading...'
      },
      admin: {
        title: 'Grape Admin — Config Center',
        login: { subtitle: 'System Admin · Config Center', admin_account: 'Admin account', password: 'Password', login_btn: 'Sign In', enter_credentials: 'Please enter account and password', back_to_app: '← Back to app' },
        nav: { config_center: 'Config Center', credentials: 'Credentials', user_management: 'User Management', user_list: 'Users', whitelist: 'Signup Whitelist', change_password: 'Change Password', logout: 'Sign Out' }
      }
    }
  };

  // ── detectLang(): detect language from URL pathname or localStorage ──
  function detectLang() {
    var path = window.location.pathname || '';
    // /en/ or /en at start → en
    if (path === '/en' || path.indexOf('/en/') === 0) return 'en';
    // No URL prefix → check localStorage
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      if (stored && SUPPORTED.indexOf(stored) >= 0) return stored;
    } catch (e) {}
    return DEFAULT_LANG;
  }

  // ── loadPacks(lang): async fetch 15 JSON packs for a language ──
  // 注意：hotfix简化实现，部分包失败时标记语言未加载
  // 用户切换到其他语言再切回来时重拉全部15个包；或点击当前语言按钮触发重试（selectLang 早退判断检查 isLangLoaded）
  // 不做单包增量重试；存在极端场景：某个包永久404会反复触发全量请求，后续迭代可优化
  function loadPacks(lang) {
    if (!SUPPORTED.indexOf || SUPPORTED.indexOf(lang) < 0) lang = DEFAULT_LANG;
    if (_loadedLangs[lang]) return Promise.resolve({failed: 0, total: 0, cached: true});

    var base = '/static/locales/' + lang + '/';
    var ver = _cacheVer ? '?v=' + _cacheVer : '';
    var failedCount = 0;
    var total = MODULES.length;

    var promises = MODULES.map(function (mod) {
      // 每个 fetch 加 15 秒超时——弱网下不让单个请求无限挂起
      var controller = new AbortController();
      var timer = setTimeout(function() {
        controller.abort();
      }, 15000);

      return fetch(base + mod + '.json' + ver, { signal: controller.signal })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + mod);
          return r.json();
        })
        .then(function (data) {
          if (!_packs[lang]) _packs[lang] = {};
          _packs[lang][mod] = data;
        })
        .catch(function (e) {
          // 覆盖所有失败路径：fetch reject(网络错误)、!r.ok(HTTP 4xx/5xx)、r.json() parse 失败、超时 abort
          failedCount++;
          console.warn('[i18n] Failed to load pack: ' + lang + '/' + mod + '.json', e.message);
        })
        .finally(function () {
          // 无论成功或失败，都清理定时器——与 api.js _fetchWithTimeout 模式一致
          clearTimeout(timer);
        });
    });

    return Promise.all(promises).then(function () {
      // 只有全部成功才标记为已加载——部分失败保持 false，下次切回会重试缺失包
      if (failedCount === 0) {
        _loadedLangs[lang] = true;
      }
      // Re-apply DOM translations after full packs load
      applyI18nToDOM();
      if (failedCount > 0) {
        console.warn('[i18n] lang=' + lang + ' load packs partial failed ' + failedCount + '/' + total);
      }
      return {failed: failedCount, total: total};
    });
  }

  // ── _lookup(pack, key): dot-separated key lookup in a pack object ──
  function _lookup(pack, key) {
    if (!pack || !key) return undefined;
    var parts = key.split('.');
    var cur = pack;
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object') return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  // ── _interpolate(str, vars): replace {name} and {{name}} placeholders ──
  function _interpolate(str, vars) {
    if (!str || typeof str !== 'string' || !vars) return str;
    return str.replace(/\{\{?(\w+)\}?\}/g, function (match, name) {
      return vars.hasOwnProperty(name) ? String(vars[name]) : match;
    });
  }

  // ── t(key, vars): translate a key with optional variable interpolation ──
  // Lookup order: current lang pack → default lang pack → key itself
  function t(key, vars) {
    var val;

    // Try current language
    if (_packs[_currentLang]) {
      val = _lookup(_packs[_currentLang], key);
    }

    // Fallback to default language if not found
    if (val === undefined && _currentLang !== DEFAULT_LANG && _packs[DEFAULT_LANG]) {
      val = _lookup(_packs[DEFAULT_LANG], key);
    }

    // Last resort: return the key itself
    if (val === undefined || val === null) return key;

    // Interpolate variables
    return _interpolate(val, vars);
  }

  // ── setLang(lang): switch language in-place (no page reload) ──
  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) < 0) lang = DEFAULT_LANG;
    if (lang === _currentLang && _loadedLangs[lang]) return Promise.resolve({failed: 0, total: 0, skipped: true});

    // 记录发起切换时的 view——loadPacks 完成后若用户已导航到其他页面，跳过整页重建
    // 仅做 applyI18nToDOM 更新 data-i18n 元素，不打断用户在新页面的操作
    var prevView = (window.history.state && window.history.state.view) || '';
    var prevHash = window.location.hash || '';

    // 状态变更（_currentLang/localStorage/cookie/URL）放在 loadPacks 成功之后
    // 从源头避免"状态已改、视图未改"的中间态——全部失败时无需回滚
    return loadPacks(lang).then(function (result) {
      // 全部成功(failed<total)、部分成功(failed<total)、缓存命中(cached) 都提交状态
      // 全部失败(failed===total && !cached) 不提交
      if (result.failed < result.total || result.cached) {
        _currentLang = lang;
        try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
        _setCookie(STORAGE_KEY, lang, 31536000, '/');
        _updateUrlLang(lang);
        applyI18nToDOM();   // 含 document.documentElement.lang 更新

        // 检查用户是否在 loadPacks 期间导航到其他页面
        var curView = (window.history.state && window.history.state.view) || '';
        var curHash = window.location.hash || '';
        if (curView === prevView && curHash === prevHash) {
          // 仍在原页面——安全重渲染
          var rendered = _rerenderCurrentPage();
          if (!rendered) {
            // rerender 失败——DOM 未更新，状态已提交（_currentLang 已切换）
            // 返回部分失败而非全部成功，让 selectLang 弹警告而非成功 Toast
            return {failed: 1, total: result.total, rerenderFailed: true};
          }
        }
        // else: 用户已导航——跳过 _rerenderCurrentPage，不打断新页面操作
        // 语言切换已通过 applyI18nToDOM 更新了 data-i18n 元素，新页面下次渲染时自然用新语言
      }
      // 全部失败：不提交任何状态变更，_currentLang/localStorage/cookie/URL 保持原值
      // DOM 不重建，旧按钮仍禁用，selectLang 手动恢复
      return result;
    });
  }

  // ── _updateUrlLang(lang): adjust URL prefix without reload ──
  // Skip URL prefix change on /admin (independent page, no /en/ prefix routing)
  function _updateUrlLang(lang) {
    var path = window.location.pathname || '/';
    // Admin page uses localStorage + applyI18nToDOM only — no URL prefix
    if (path === '/admin' || path.indexOf('/admin/') === 0) return;
    var hash = window.location.hash || '';
    var search = window.location.search || '';

    if (lang === 'en') {
      if (path !== '/en' && path.indexOf('/en/') !== 0) {
        path = '/en' + (path === '/' ? '' : path);
      }
    } else {
      if (path === '/en') {
        path = '/';
      } else if (path.indexOf('/en/') === 0) {
        path = path.substring(3);
      }
    }

    try {
      window.history.replaceState(window.history.state, '', path + search + hash);
    } catch (e) {}
  }

  // ── _rerenderCurrentPage(): re-navigate to current view after lang switch ──
  // 返回 true 表示成功重渲染，false 表示渲染失败（异常被 try/catch 吞掉）
  function _rerenderCurrentPage() {
    // Recover current view from history state or hash
    var state = window.history.state;
    var view = null, param = null;
    if (state && state.view) {
      view = state.view;
      param = state.param;
    } else {
      var hash = window.location.hash;
      if (hash && hash.length > 1) {
        var parts = hash.substring(1).split('/');
        view = parts[0];
        param = parts[1] ? decodeURIComponent(parts[1]) : null;
      }
    }
    if (!view || typeof app === 'undefined' || !app.nav) return true;  // 无视图不算失败

    // For settings page, preserve the active sub-tab across re-render
    if (view === 'settings' && typeof renderSettings === 'function') {
      try {
        var activeTab = document.querySelector('.stabs a.ac');
        var tabId = activeTab ? (activeTab.getAttribute('onclick') || '').match(/'([^']+)'/) : null;
        var savedTab = tabId ? tabId[1] : null;
        renderSettings(savedTab || 'gitcode');
        return true;
      } catch (e) {
        console.warn('[i18n] _rerenderCurrentPage renderSettings failed:', e.message);
        return false;
      }
    }

    // For account page, preserve the active sub-tab across re-render
    if (view === 'account' && typeof renderAccount === 'function') {
      try {
        var acctTab = document.querySelector('.stabs a.ac');
        var acctTabId = acctTab ? (acctTab.getAttribute('onclick') || '').match(/'([^']+)'/) : null;
        var savedAcctTab = acctTabId ? acctTabId[1] : null;
        renderAccount(savedAcctTab || 'profile');
        return true;
      } catch (e) {
        console.warn('[i18n] _rerenderCurrentPage renderAccount failed:', e.message);
        return false;
      }
    }

    try { app.nav(view, param); return true; }
    catch (e) {
      console.warn('[i18n] _rerenderCurrentPage app.nav failed:', e.message);
      return false;
    }
  }

  // ── _setCookie(name, value, maxAge, path) ──
  function _setCookie(name, value, maxAge, path) {
    try {
      document.cookie = name + '=' + value +
        '; max-age=' + (maxAge || 31536000) +
        '; path=' + (path || '/') +
        '; SameSite=Lax';
    } catch (e) {}
  }

  // ── _applyUrlLang(lang): adjust URL prefix and reload ──
  // Skip on /admin (independent page, no /en/ prefix routing)
  function _applyUrlLang(lang) {
    var path = window.location.pathname || '/';
    // Admin page: no URL prefix change, just reload as-is
    if (path === '/admin' || path.indexOf('/admin/') === 0) return;
    var hash = window.location.hash || '';
    var search = window.location.search || '';

    if (lang === 'en') {
      // Add /en prefix (if not already present)
      if (path !== '/en' && path.indexOf('/en/') !== 0) {
        path = '/en' + (path === '/' ? '' : path);
      }
    } else {
      // Strip /en prefix for zh (default)
      if (path === '/en') {
        path = '/';
      } else if (path.indexOf('/en/') === 0) {
        path = path.substring(3); // remove '/en'
      }
    }

    // Reload with new URL (preserve hash + search)
    window.location.href = path + search + hash;
  }

  // ── getLang(): return current language ──
  function getLang() {
    return _currentLang;
  }

  // ── applyI18nToDOM(): set document lang + replace [data-i18n] elements ──
  function applyI18nToDOM() {
    // Set <html lang="...">
    var html = document.documentElement;
    if (html) html.setAttribute('lang', _currentLang === 'en' ? 'en' : 'zh-CN');

    // Replace text content for [data-i18n] elements
    var els = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var key = el.getAttribute('data-i18n');
      if (!key) continue;
      // Support data-i18n-vars for JSON variable map
      var varsAttr = el.getAttribute('data-i18n-vars');
      var vars = null;
      if (varsAttr) {
        try { vars = JSON.parse(varsAttr); } catch (e) {}
      }
      var translated = t(key, vars);
      if (translated && translated !== key) {
        el.textContent = translated;
      }
    }

    // Support [data-i18n-placeholder] for input placeholders
    var phEls = document.querySelectorAll('[data-i18n-placeholder]');
    for (var j = 0; j < phEls.length; j++) {
      var phEl = phEls[j];
      var phKey = phEl.getAttribute('data-i18n-placeholder');
      if (!phKey) continue;
      var phTranslated = t(phKey);
      if (phTranslated && phTranslated !== phKey) {
        phEl.setAttribute('placeholder', phTranslated);
      }
    }

    // Support [data-i18n-title] for title attributes
    var titleEls = document.querySelectorAll('[data-i18n-title]');
    for (var k = 0; k < titleEls.length; k++) {
      var titleEl = titleEls[k];
      var titleKey = titleEl.getAttribute('data-i18n-title');
      if (!titleKey) continue;
      var titleTranslated = t(titleKey);
      if (titleTranslated && titleTranslated !== titleKey) {
        titleEl.setAttribute('title', titleTranslated);
      }
    }

    // Remove i18n pending flag — translations applied, reveal text
    var html = document.documentElement;
    if (html && html.hasAttribute('data-i18n-pending')) {
      html.removeAttribute('data-i18n-pending');
    }
  }

  // ── init(cacheVer): synchronous init with inline packs, then async load full packs ──
  function init(cacheVer) {
    _cacheVer = cacheVer || '';

    // Detect language
    _currentLang = detectLang();

    // Fill _packs with inline minimal packs (synchronous)
    _packs[DEFAULT_LANG] = _INLINE_PACKS[DEFAULT_LANG] || {};
    if (_currentLang !== DEFAULT_LANG) {
      _packs[_currentLang] = _INLINE_PACKS[_currentLang] || {};
    }
    _loadedLangs[DEFAULT_LANG] = false;
    _loadedLangs[_currentLang] = false;

    // Apply DOM translations immediately (inline packs cover critical UI)
    applyI18nToDOM();

    // Async load full packs for current language
    loadPacks(_currentLang).then(function () {
      // Also load default language packs for fallback (if different)
      if (_currentLang !== DEFAULT_LANG) {
        loadPacks(DEFAULT_LANG);
      }
    }).catch(function () {});

    // Set cookie to match detected language (ensures server-side consistency)
    _setCookie(STORAGE_KEY, _currentLang, 31536000, '/');
  }

  // ── isLangLoaded(lang): check if language packs are fully loaded ──
  function isLangLoaded(lang) {
    return !!_loadedLangs[lang];
  }

  // ── Public API ──
  return {
    t: t,
    setLang: setLang,
    getLang: getLang,
    init: init,
    detectLang: detectLang,
    applyI18nToDOM: applyI18nToDOM,
    isLangLoaded: isLangLoaded,
    SUPPORTED: SUPPORTED,
    DEFAULT_LANG: DEFAULT_LANG
  };
})();
