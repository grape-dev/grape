/**
 * feedback.js — 问题反馈弹窗
 * 全局函数 openFeedbackDialog(opts)，由顶栏 tbFeedback 按钮调用（无参，原行为）；
 * events 页失败事件行带 {event_seq} 调用 → 自动拉取任务上下文（feedback-task-context-design.md）。
 * 弹窗样式参考 _grapeConfirm（overlay + dialog，圆角，苹果风格）。
 */
(function () {
  'use strict';

  var _selectedCategory = 'bug';
  var _attachmentUrl = '';
  var _attachmentName = '';
  var _contextSeq = 0; // 任务上下文关联的事件 seq（0=手动反馈，无上下文）

  // i18n 辅助
  function t(key) {
    try {
      return I18N.t('app.feedback.' + key);
    } catch (e) {
      return '';
    }
  }

  // 任务上下文卡片 HTML（只读，浅灰底圆角，样式对齐弹窗 iOS 风格）
  function _contextCardHtml(ctx, state) {
    if (state === 'loading') {
      return '<div id="fbContext" style="background:rgba(0,0,0,.04);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:var(--t3,#86868b);">📋 ' + (t('context_loading') || '正在获取任务上下文...') + '</div>';
    }
    if (state === 'error') {
      return '<div id="fbContext" style="background:rgba(255,59,48,.06);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#d32f2f;">⚠️ ' + (t('context_failed') || '任务上下文获取失败，可直接填写提交（提交时服务端会再次尝试获取）') + '</div>';
    }
    if (!ctx) return '';
    var task = ctx.task || null;
    var engine = ctx.engine || null;
    var rowStyle = 'display:flex;gap:8px;line-height:1.7;';
    var labelStyle = 'color:var(--t3,#86868b);flex:0 0 48px;font-size:12px;';
    var valStyle = 'color:var(--t1,#1d1d1f);font-size:12px;word-break:break-all;';
    // 失败摘要：默认折叠（点击展开/收起）
    var summary = ctx.fail_summary || '';
    var summaryShort = summary ? summary.substring(0, 200) : '';
    var summaryHtml = summary
      ? '<div style="margin-top:6px;padding-top:6px;border-top:1px dashed rgba(0,0,0,.1);font-size:12px;color:var(--t2,#3a3a3c);line-height:1.6;">' +
        '<b>' + (t('context_fail_summary') || '失败摘要') + '：</b>' +
        '<span id="fbSummaryShort" style="color:var(--t2,#3a3a3c);">' + _esc(summaryShort) + (summary.length > 200 ? '… ' : '') + '</span>' +
        (summary.length > 200 ? '<a id="fbSummaryToggle" style="color:#0071e3;text-decoration:none;cursor:pointer;font-weight:500;white-space:nowrap;"> ' + (t('context_expand') || '展开') + '</a>' : '') +
        '<div id="fbSummaryFull" style="display:none;white-space:pre-wrap;">' + _esc(summary) + '</div>' +
        '</div>'
      : '';
    var taskLine = task
      ? (task.task_id || '—') + (task.task_status ? '（' + _esc(task.task_status) + (task.exit_code !== null && task.exit_code !== undefined ? '，exit ' + task.exit_code : '') + '）' : '')
      : '未派发';
    var engineLine = engine
      ? '<b>' + _esc(engine.runtime || '—') + '</b>' +
        (engine.engine_instance ? ' · ' + _esc(engine.engine_instance) : '') +
        (engine.engine_ip ? ' · ' + _esc(engine.engine_ip) : '') +
        (engine.health_score !== null && engine.health_score !== undefined ? ' · 健康分 ' + engine.health_score : '')
      : '未派发';
    return '<div id="fbContext" style="background:rgba(0,0,0,.04);border-radius:10px;padding:10px 14px;margin-bottom:16px;">' +
      '<div style="font-size:12px;font-weight:600;color:var(--t2,#3a3a3c);margin-bottom:4px;">📋 ' + (t('context_title') || '任务上下文（自动搜集，只读）') + '</div>' +
      '<div style="' + rowStyle + '"><span style="' + labelStyle + '">仓库</span><span style="' + valStyle + '">' + _esc(ctx.repo_key || '—') + '</span></div>' +
      (ctx.issue_number ? '<div style="' + rowStyle + '"><span style="' + labelStyle + '">Issue</span><span style="' + valStyle + '">#' + _esc(String(ctx.issue_number)) + '</span></div>' : '') +
      (ctx.agent_type ? '<div style="' + rowStyle + '"><span style="' + labelStyle + '">Agent</span><span style="' + valStyle + '">' + _esc(ctx.agent_type) + '</span></div>' : '') +
      '<div style="' + rowStyle + '"><span style="' + labelStyle + '">任务</span><span style="' + valStyle + '">' + _esc(taskLine) + '</span></div>' +
      '<div style="' + rowStyle + '"><span style="' + labelStyle + '">引擎</span><span style="' + valStyle + '">' + engineLine + '</span></div>' +
      summaryHtml +
      '</div>';
  }

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  window.openFeedbackDialog = function (opts) {
    // 移除已有弹窗
    var existing = document.getElementById('feedbackOverlay');
    if (existing) {
      existing.remove();
      return;
    }

    opts = opts || {};
    _contextSeq = opts.event_seq || 0;
    _selectedCategory = 'bug';
    _attachmentUrl = '';
    _attachmentName = '';

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
      // 任务上下文卡片（feedback-task-context：失败事件触发时自动搜集）
      (_contextSeq ? _contextCardHtml(null, 'loading') : '') +
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
      '</div>' +
      // 我的反馈入口链接
      '<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--b,rgba(0,0,0,.08));text-align:center;">' +
      '<a id="fbMyFeedback" style="font-size:12px;color:#0071e3;text-decoration:none;cursor:pointer;font-weight:500;">' +
      (t('view_my_feedback') || '查看我的反馈 →') +
      '</a>' +
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
    // 我的反馈入口 — 关闭弹窗后跳转
    var myFeedbackLink = document.getElementById('fbMyFeedback');
    if (myFeedbackLink) {
      myFeedbackLink.onclick = function () {
        _close();
        app.nav('my-feedback');
      };
    }

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

    // 任务上下文加载（feedback-task-context：服务端权威现查）
    if (_contextSeq) {
      var _ctxEl = function () { return document.getElementById('fbContext'); };
      API.get('/feedback/context/' + _contextSeq)
        .then(function (ctx) {
          var el = _ctxEl();
          if (el && ctx) {
            el.outerHTML = _contextCardHtml(ctx, 'ok');
            // 摘要折叠/展开
            var tg = document.getElementById('fbSummaryToggle');
            if (tg) {
              tg.onclick = function (ev) {
                ev.preventDefault();
                var full = document.getElementById('fbSummaryFull');
                var short = document.getElementById('fbSummaryShort');
                var expanded = full && full.style.display !== 'none';
                if (full && short) {
                  full.style.display = expanded ? 'none' : '';
                  short.style.display = expanded ? '' : 'none';
                  tg.textContent = expanded ? (' ' + (t('context_expand') || '展开')) : (' ' + (t('context_collapse') || '收起'));
                }
              };
            }
          } else if (el) {
            el.outerHTML = _contextCardHtml(null, 'error');
          }
        })
        .catch(function () {
          var el = _ctxEl();
          if (el) el.outerHTML = _contextCardHtml(null, 'error');
        });
    }

    // 附件上传
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
        if (app && app.showToast) app.showToast('附件不能超过 5MB（当前 ' + Math.round(file.size / 1024) + 'KB）');
        fileInput.value = '';
        return;
      }
      fileNameSpan.textContent = file.name + '（上传中...）';
      // 使用 API.upload（自动带 JWT Authorization header + token 刷新）
      var fd = new FormData();
      fd.append('file', file);
      API.upload('/feedback/upload', fd)
        .then(function (resp) {
          if (resp && resp.error) {
            _attachmentUrl = '';
            fileNameSpan.textContent = '';
            if (app && app.showToast) app.showToast('上传失败: ' + resp.error);
          } else {
            _attachmentUrl = resp.url || '';
            _attachmentName = resp.original_name || file.name || '';
            fileNameSpan.textContent = _attachmentName + ' ✓';
          }
        })
        .catch(function (err) {
          _attachmentUrl = '';
          // 展示具体错误信息（HTTP 状态码 + 后端 detail）
          var msg = '上传失败';
          if (err && err.message) {
            msg = '上传失败(' + err.message + ')';
          }
          fileNameSpan.textContent = msg;
          if (app && app.showToast) app.showToast(msg);
        });
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
        attachment_name: _attachmentName || '',
        event_seq: _contextSeq || 0,
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
