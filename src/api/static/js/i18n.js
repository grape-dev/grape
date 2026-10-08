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
    'tasks', 'issues', 'repos', 'dashboard', 'observability', 'templates'
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
          gitcode_login: '使用 GitCode 登录',
          pwd_login: '用户名密码登录',
          username: '用户名',
          password: '密码',
          login_btn: '登录',
          back_gitcode: '← 返回 GitCode 登录',
          failed: '登录失败: {{msg}}'
        },
        repo: {
          select: '选择仓库',
          load_failed: '无法加载仓库列表，请确认 GitCode Token 是否有效'
        }
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
          gitcode_login: 'Login with GitCode',
          pwd_login: 'Username & Password Login',
          username: 'Username',
          password: 'Password',
          login_btn: 'Login',
          back_gitcode: '← Back to GitCode Login',
          failed: 'Login failed: {{msg}}'
        },
        repo: {
          select: 'Select Repository',
          load_failed: 'Failed to load repository list. Please check your GitCode Token.'
        }
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

  // ── _interpolate(str, vars): replace {{name}} placeholders ──
  function _interpolate(str, vars) {
    if (!str || typeof str !== 'string' || !vars) return str;
    return str.replace(/\{\{(\w+)\}\}/g, function (match, name) {
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
  function _updateUrlLang(lang) {
    var path = window.location.pathname || '/';
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
        renderSettings(savedTab || 'credentials');
        return true;
      } catch (e) {
        console.warn('[i18n] _rerenderCurrentPage renderSettings failed:', e.message);
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
  // zh → strip /en prefix; en → add /en prefix
  function _applyUrlLang(lang) {
    var path = window.location.pathname || '/';
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
