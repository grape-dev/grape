/**
 * auth.js — 认证页面共享逻辑（登录/注册/重置密码）
 * 消除三个页面之间的代码重复：showToast / togglePwd / showFieldError /
 * clearFieldError / setBtnLoading / 密码强度计算 / i18n 初始化 / startCountdown
 *
 * i18n 改造：复用 I18N 模块（如果已加载），fallback 独立 fetch。
 * 提供 t() 快捷函数和 toggleLang() 语言切换。
 */
var Auth = (function () {

  // ── SVG 常量 ──
  var EYE_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_CLOSED = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

  // ── i18n ──
  // 认证页面完全委托 I18N 模块，不维护独立翻译逻辑
  // auth.js 只负责 I18N 不管的两件事：语言切换按钮互斥显示 + 页面动态文案回调

  function _getLang() {
    if (typeof I18N !== 'undefined' && I18N.getLang) return I18N.getLang();
    var lang = localStorage.getItem('grapeLang');
    if (!lang) {
      lang = (navigator.language || 'zh').startsWith('zh') ? 'zh' : 'en';
      localStorage.setItem('grapeLang', lang);
    }
    return lang;
  }

  // t(): 快捷翻译函数，内联 JS 调用 Auth.t('app.login.xxx')
  function t(key) {
    if (typeof I18N !== 'undefined' && I18N.t) {
      var val = I18N.t(key);
      if (val && val !== key) return val;
    }
    return key;  // 返回 key 本身，不报错
  }

  function initI18n() {
    // I18N.init() 同步应用 inline 包（95 个 login key，零闪烁）+ 异步加载全量包
    if (typeof I18N !== 'undefined' && I18N.init) {
      I18N.init('34');
      _updateLangSwitch();
      return;
    }
    // 无 I18N 模块时，仅更新按钮（HTML 硬编码中文作为 fallback）
    _updateLangSwitch();
  }

  // ── 语言切换 ──
  function toggleLang() {
    var newLang = _getLang() === 'zh' ? 'en' : 'zh';
    if (typeof I18N !== 'undefined' && I18N.setLang) {
      // I18N.setLang: loadPacks + applyI18nToDOM + URL 前缀 + cookie 双写
      I18N.setLang(newLang).then(function () {
        _updateLangSwitch();
        if (typeof _onLangChange === 'function') _onLangChange();
      });
      return;
    }
    // 无 I18N 模块的 fallback（不应发生，register/forgot 都加载 i18n.js）
    localStorage.setItem('grapeLang', newLang);
    _updateLangSwitch();
  }

  // 语言切换按钮互斥显示：当前 zh → 显示 "EN"，当前 en → 显示 "中文"
  function _updateLangSwitch() {
    var lang = _getLang();
    var switches = document.querySelectorAll('.lang-switch span');
    switches.forEach(function (el) {
      var val = t('app.login.' + (lang === 'zh' ? 'lang_switch_en' : 'lang_switch_zh'));
      if (!val || val.indexOf('app.login.') === 0) val = lang === 'zh' ? 'EN' : '中文';
      el.textContent = val;
    });
  }

  // ── Toast ──
  function showToast(msg) {
    var t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () {
      t.classList.add('fade-out');
      setTimeout(function () { t.remove() }, 300);
    }, 2500);
  }

  // ── 密码可见性切换 ──
  function togglePwd(id, btn) {
    var el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'password') {
      el.type = 'text';
      btn.innerHTML = EYE_CLOSED;
      btn.setAttribute('aria-label', t('app.login.hide_password'));
    } else {
      el.type = 'password';
      btn.innerHTML = EYE_OPEN;
      btn.setAttribute('aria-label', t('app.login.show_password'));
    }
  }

  // ── 字段错误 ──
  function showFieldError(fieldId, msg) {
    var errEl = document.getElementById(fieldId + 'Err');
    var inputEl = document.getElementById(fieldId);
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
    if (inputEl) { inputEl.setAttribute('aria-invalid', 'true'); }
  }

  function clearFieldError(fieldId) {
    var errEl = document.getElementById(fieldId + 'Err');
    var inputEl = document.getElementById(fieldId);
    if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
    if (inputEl) { inputEl.removeAttribute('aria-invalid'); }
  }

  function clearErrorOnInput(fieldId) {
    var inputEl = document.getElementById(fieldId);
    if (!inputEl) return;
    if (inputEl.getAttribute('aria-invalid') === 'true') {
      clearFieldError(fieldId);
    }
  }

  function blurValidate(fieldId, validateFn) {
    var el = document.getElementById(fieldId);
    if (!el) return true;
    var val = el.value.trim();
    if (!val) {
      clearFieldError(fieldId);
      return true;
    }
    var err = validateFn(val);
    if (err) {
      showFieldError(fieldId, err);
      return false;
    }
    clearFieldError(fieldId);
    return true;
  }

  function submitValidate(fieldId, validateFn) {
    var el = document.getElementById(fieldId);
    if (!el) return true;
    var val = el.value.trim();
    var err = validateFn(val);
    if (err) {
      showFieldError(fieldId, err);
      return false;
    }
    clearFieldError(fieldId);
    return true;
  }

  // ── 按钮 loading ──
  function setBtnLoading(btn, loading) {
    if (!btn) return;
    if (loading) {
      btn.disabled = true;
      btn.dataset.originalHtml = btn.innerHTML;
      btn.innerHTML = '<div class="spinner"></div>';
    } else {
      btn.disabled = false;
      if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
    }
  }

  // ── 密码强度计算 ──
  function updatePwdStrength(pwdInputId, opts) {
    var pwd = (document.getElementById(pwdInputId) || {}).value || '';
    var strengthEl = document.getElementById(opts.strengthId);
    var fillEl = document.getElementById(opts.fillId);
    var labelEl = document.getElementById(opts.labelId);
    var reqsEl = document.getElementById(opts.reqsId);

    if (!pwd) {
      if (strengthEl) strengthEl.classList.remove('visible');
      if (reqsEl) reqsEl.classList.remove('visible');
      return;
    }

    if (strengthEl) strengthEl.classList.add('visible');
    if (reqsEl) reqsEl.classList.add('visible');

    var score = 0;
    if (pwd.length >= 8) score++;
    if (pwd.length >= 12) score++;
    if (/[a-zA-Z]/.test(pwd) && /\d/.test(pwd)) score++;
    if (/[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/;'`~]/.test(pwd)) score++;

    if (pwd.length < 8) score = 0;

    if (fillEl) fillEl.className = 'pwd-strength-fill';
    if (labelEl) labelEl.className = 'pwd-strength-label';
    if (score <= 1) {
      if (fillEl) fillEl.classList.add('weak');
      if (labelEl) { labelEl.classList.add('weak'); labelEl.textContent = t('app.login.pwd_strength_weak'); }
    } else if (score <= 2) {
      if (fillEl) fillEl.classList.add('medium');
      if (labelEl) { labelEl.classList.add('medium'); labelEl.textContent = t('app.login.pwd_strength_medium'); }
    } else {
      if (fillEl) fillEl.classList.add('strong');
      if (labelEl) { labelEl.classList.add('strong'); labelEl.textContent = t('app.login.pwd_strength_strong'); }
    }

    if (opts.reqs) {
      opts.reqs.forEach(function (r) {
        var li = document.getElementById(r.id);
        if (li) li.classList.toggle('met', r.test(pwd));
      });
    }
  }

  // ── 倒计时 ──
  function startCountdown(btnId, opts) {
    opts = opts || {};
    var sb = document.getElementById(btnId);
    if (!sb) return;
    var remaining = 60;
    var suffix = opts.suffix || 's';
    // 保护原始 data-i18n 属性和文案，倒计时结束后恢复（语言切换时 I18N.apply 能正确更新）
    var _origI18n = sb.getAttribute('data-i18n') || '';
    var _origText = sb.textContent || '';
    sb.disabled = true;
    sb.removeAttribute('data-i18n');  // 倒计时期间移除，避免 I18N.apply 覆盖倒计时数字
    sb.textContent = remaining + suffix;
    var timer = setInterval(function () {
      remaining--;
      if (remaining <= 0) {
        clearInterval(timer);
        sb.disabled = false;
        // 恢复原始 data-i18n + 文案（resend_code "重新发送"）
        if (_origI18n) sb.setAttribute('data-i18n', _origI18n);
        sb.textContent = t(_origI18n || 'app.login.resend_code') || _origText;
      } else {
        sb.textContent = remaining + suffix;
      }
    }, 1000);
  }

  // ── HTML 转义 ──
  function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return {
    EYE_OPEN: EYE_OPEN,
    EYE_CLOSED: EYE_CLOSED,
    initI18n: initI18n,
    t: t,
    toggleLang: toggleLang,
    showToast: showToast,
    togglePwd: togglePwd,
    showFieldError: showFieldError,
    clearFieldError: clearFieldError,
    clearErrorOnInput: clearErrorOnInput,
    blurValidate: blurValidate,
    submitValidate: submitValidate,
    setBtnLoading: setBtnLoading,
    updatePwdStrength: updatePwdStrength,
    startCountdown: startCountdown,
    escHtml: escHtml
  };
})();
