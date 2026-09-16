/**
 * feedback.js — 问题反馈弹窗
 * 全局函数 openFeedbackDialog()，由顶栏 tbFeedback 按钮调用。
 * 弹窗样式参考 _grapeConfirm（overlay + dialog，圆角，苹果风格）。
 */
(function () {
  'use strict';

  var _selectedCategory = 'bug';
  var _attachmentUrl = '';

  // i18n 辅助
  function t(key) {
    try {
      return I18N.t('app.feedback.' + key);
    } catch (e) {
      return '';
    }
  }

  window.openFeedbackDialog = function () {
    // 移除已有弹窗
    var existing = document.getElementById('feedbackOverlay');
    if (existing) {
      existing.remove();
      return;
    }

    _selectedCategory = 'bug';
    _attachmentUrl = '';

    var overlay = document.createElement('div');
    overlay.id = 'feedbackOverlay';
    overlay.style.cssText =
      'position:fixed;top:0;left:0;width:100%;height:100%;' +
      'background:rgba(0,0,0,.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);' +
      'z-index:99999;opacity:0;transition:opacity .25s cubic-bezier(.4,0,.2,1);' +
      'display:flex;align-items:center;justify-content:center;';

    var dlg = document.createElement('div');
    dlg.style.cssText =
      'width:480px;max-width:92vw;max-height:90vh;overflow-y:auto;' +
      'background:var(--c,#fff);border-radius:16px;padding:24px 28px;' +
      'box-shadow:0 12px 50px rgba(0,0,0,.18);' +
      'font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",sans-serif;' +
      'transform:scale(1.04);transition:transform .25s cubic-bezier(.4,0,.2,1);';

    // ---- 类型选择按钮 ----
    var categories = [
      { key: 'bug', label: t('category_bug') || 'Bug 缺陷', icon: '🐛' },
      { key: 'suggestion', label: t('category_suggestion') || '功能建议', icon: '💡' },
      { key: 'question', label: t('category_question') || '使用疑问', icon: '❓' },
      { key: 'other', label: t('category_other') || '其他', icon: '📝' },
    ];

    var catBtnsHtml = categories
      .map(function (c, i) {
        return (
          '<button data-cat="' + c.key + '" class="fb-cat-btn' + (i === 0 ? ' active' : '') + '" ' +
          'style="flex:1;padding:10px 4px;border-radius:10px;border:1.5px solid var(--b,rgba(0,0,0,.1));' +
          'background:var(--c,#fff);font-size:12px;font-weight:500;color:var(--t2,#3a3a3c);' +
          'cursor:pointer;font-family:inherit;transition:all .15s;display:flex;flex-direction:column;align-items:center;gap:4px;">' +
          '<span style="font-size:18px">' + c.icon + '</span>' +
          '<span>' + c.label + '</span>' +
          '</button>'
        );
      })
      .join('');

    dlg.innerHTML =
      // 标题
      '<div style="font-size:17px;font-weight:600;color:var(--t1,#1d1d1f);margin-bottom:16px;">' +
      (t('title') || '问题反馈') +
      '</div>' +
      // 类型选择
      '<div style="display:flex;gap:8px;margin-bottom:16px;">' + catBtnsHtml + '</div>' +
      // 标题输入
      '<input id="fbTitle" type="text" placeholder="' + (t('placeholder_title') || '一句话概述问题') + '" ' +
      'style="width:100%;box-sizing:border-box;padding:10px 14px;border-radius:10px;' +
      'border:1.5px solid var(--b,rgba(0,0,0,.12));background:var(--c,#fff);' +
      'font-size:14px;color:var(--t1,#1d1d1f);font-family:inherit;margin-bottom:12px;outline:none;" />' +
      // 描述 textarea
      '<textarea id="fbDesc" rows="4" placeholder="' + (t('placeholder_desc') || '请详细描述遇到的问题、复现步骤、期望行为...') + '" ' +
      'style="width:100%;box-sizing:border-box;padding:10px 14px;border-radius:10px;' +
      'border:1.5px solid var(--b,rgba(0,0,0,.12));background:var(--c,#fff);' +
      'font-size:13px;color:var(--t1,#1d1d1f);font-family:inherit;margin-bottom:12px;outline:none;resize:vertical;line-height:1.5;"></textarea>' +
      // 截图上传
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;">' +
      '<input id="fbFile" type="file" accept="image/*,.zip" style="display:none" />' +
      '<button id="fbFileBtn" type="button" ' +
      'style="padding:8px 14px;border-radius:8px;border:1.5px solid var(--b,rgba(0,0,0,.12));' +
      'background:var(--c,#fff);font-size:12px;font-weight:500;color:var(--t2,#3a3a3c);' +
      'cursor:pointer;font-family:inherit;">📎 选择附件</button>' +
      '<span id="fbFileName" style="font-size:12px;color:var(--t3,#86868b);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;"></span>' +
      '</div>' +
      // 页面信息
      '<div style="font-size:11px;color:var(--t3,#86868b);margin-bottom:18px;padding:8px 10px;' +
      'background:var(--b,rgba(0,0,0,.03));border-radius:8px;">' +
      '📍 ' + (window.location.hash || '#') +
      '</div>' +
      // 按钮行
      '<div style="display:flex;gap:10px;justify-content:flex-end;">' +
      '<button id="fbCancel" type="button" ' +
      'style="padding:9px 20px;border-radius:10px;border:none;' +
      'background:var(--b,rgba(0,0,0,.06));font-size:13px;font-weight:500;' +
      'color:var(--t1,#1d1d1f);cursor:pointer;font-family:inherit;">取消</button>' +
      '<button id="fbSubmit" type="button" ' +
      'style="padding:9px 24px;border-radius:10px;border:none;' +
      'background:#0071e3;font-size:13px;font-weight:600;color:#fff;' +
      'cursor:pointer;font-family:inherit;">' + (t('submit') || '提交反馈') + '</button>' +
      '</div>';

    overlay.appendChild(dlg);
    document.body.appendChild(overlay);

    // 动画入场
    requestAnimationFrame(function () {
      overlay.style.opacity = '1';
      dlg.style.transform = 'scale(1)';
    });

    // ---- 关闭函数 ----
    function _close() {
      overlay.style.opacity = '0';
      dlg.style.transform = 'scale(1.04)';
      setTimeout(function () {
        overlay.remove();
      }, 250);
    }

    // ---- 事件绑定 ----
    // 点击 overlay 空白关闭
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) _close();
    });
    // 取消按钮
    document.getElementById('fbCancel').onclick = _close;

    // 类型选择
    var catBtns = dlg.querySelectorAll('.fb-cat-btn');
    catBtns.forEach(function (btn) {
      btn.onclick = function () {
        catBtns.forEach(function (b) {
          b.classList.remove('active');
          b.style.borderColor = 'var(--b,rgba(0,0,0,.1))';
          b.style.background = 'var(--c,#fff)';
          b.style.color = 'var(--t2,#3a3a3c)';
        });
        btn.classList.add('active');
        btn.style.borderColor = '#0071e3';
        btn.style.background = 'rgba(0,113,227,.06)';
        btn.style.color = '#0071e3';
        _selectedCategory = btn.getAttribute('data-cat');
      };
    });
    // 初始选中第一个
    if (catBtns.length > 0) {
      catBtns[0].style.borderColor = '#0071e3';
      catBtns[0].style.background = 'rgba(0,113,227,.06)';
      catBtns[0].style.color = '#0071e3';
    }

    // 截图上传
    var fileInput = document.getElementById('fbFile');
    var fileBtn = document.getElementById('fbFileBtn');
    var fileNameSpan = document.getElementById('fbFileName');
    fileBtn.onclick = function () {
      fileInput.click();
    };
    fileInput.onchange = function () {
      var file = fileInput.files[0];
      if (!file) return;
      // 限制 5MB
      if (file.size > 5 * 1024 * 1024) {
        if (app && app.showToast) app.showToast('附件不能超过 5MB');
        fileInput.value = '';
        return;
      }
      fileNameSpan.textContent = file.name + '（上传中...）';
      // 通过后端 API 上传（OBS 模式存 OBS 桶，非 OBS 模式存本地）
      var fd = new FormData();
      fd.append('file', file);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/feedback/upload');
      xhr.onload = function () {
        if (xhr.status === 200) {
          try {
            var resp = JSON.parse(xhr.responseText);
            if (resp.error) {
              _attachmentUrl = '';
              fileNameSpan.textContent = '';
              if (app && app.showToast) app.showToast('上传失败: ' + resp.error);
            } else {
              _attachmentUrl = resp.url || '';
              fileNameSpan.textContent = file.name + ' ✓';
            }
          } catch (e) {
            _attachmentUrl = '';
            fileNameSpan.textContent = '上传失败';
          }
        } else {
          _attachmentUrl = '';
          fileNameSpan.textContent = '上传失败';
          if (app && app.showToast) app.showToast('附件上传失败');
        }
      };
      xhr.onerror = function () {
        _attachmentUrl = '';
        fileNameSpan.textContent = '上传失败';
        if (app && app.showToast) app.showToast('附件上传失败');
      };
      xhr.send(fd);
    };

    // 提交
    var submitBtn = document.getElementById('fbSubmit');
    submitBtn.onclick = function () {
      var title = document.getElementById('fbTitle').value.trim();
      var desc = document.getElementById('fbDesc').value.trim();

      if (!title) {
        if (app && app.showToast) app.showToast(t('placeholder_title') || '请输入标题');
        document.getElementById('fbTitle').focus();
        return;
      }
      if (!desc) {
        if (app && app.showToast) app.showToast(t('placeholder_desc') || '请输入描述');
        document.getElementById('fbDesc').focus();
        return;
      }

      // 获取当前 repo_key
      var repoKey = '';
      try {
        if (window._currentRepo && typeof window._currentRepo === 'function') {
          var r = window._currentRepo();
          if (r) repoKey = (r.owner || '') + '/' + (r.name || '');
        } else if (window._currentRepo) {
          var r2 = window._currentRepo;
          if (r2) repoKey = (r2.owner || '') + '/' + (r2.name || '');
        }
      } catch (e) {}

      submitBtn.disabled = true;
      submitBtn.textContent = '...';
      submitBtn.style.opacity = '0.6';

      API.post('/feedback', {
        category: _selectedCategory,
        title: title,
        description: desc,
        page_url: window.location.hash || '',
        repo_key: repoKey,
        attachment_url: _attachmentUrl || '',
      })
        .then(function (resp) {
          if (resp && resp.error) {
            if (app && app.showToast) app.showToast(resp.error);
            submitBtn.disabled = false;
            submitBtn.textContent = t('submit') || '提交反馈';
            submitBtn.style.opacity = '1';
            return;
          }
          _close();
          if (app && app.showToast) app.showToast(t('success') || '反馈已提交，感谢您的反馈！');
        })
        .catch(function (err) {
          var msg = (err && err.message) || t('failed') || '反馈提交失败，请稍后重试';
          if (app && app.showToast) app.showToast(msg);
          submitBtn.disabled = false;
          submitBtn.textContent = t('submit') || '提交反馈';
          submitBtn.style.opacity = '1';
        });
    };
  };
})();
