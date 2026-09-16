/**
 * my_feedback_detail.js — 工作台"反馈详情"页面
 * 通过 app.nav('my-feedback-detail', id) 导航进入
 */
function renderMyFeedbackDetail(feedbackId) {
  var el = document.getElementById('pageContent');
  if (!el) return;

  function t(key, params) {
    return I18N.t('app.feedback.' + key, params);
  }

  function statusLabel(st) {
    var map = { open: t('status_open'), in_progress: t('status_in_progress'), resolved: t('status_resolved'), closed: t('status_closed') };
    return map[st] || st;
  }

  if (!feedbackId) {
    el.innerHTML = '<div class="ts t2" style="padding:20px">' + t('missing_id') + '</div>';
    return;
  }

  el.innerHTML = '<div class="loading">' + t('loading') + '</div>';

  API.get('/feedback/mine?page=0&size=100')
    .then(function (resp) {
      var items = resp.items || [];
      var item = null;
      for (var i = 0; i < items.length; i++) {
        if (String(items[i].id) === String(feedbackId)) { item = items[i]; break; }
      }
      if (!item) {
        el.innerHTML = '<div class="ts t2" style="padding:20px">' + t('not_found') + '</div>';
        return;
      }
      renderDetail(item);
    })
    .catch(function (err) {
      el.innerHTML = '<div class="ts t2" style="padding:20px">' + t('load_failed', { msg: err.message || '' }) + '</div>';
    });

  function renderDetail(item) {
    var st = item.status;
    var stColor = st === 'open' ? 'var(--a)' : st === 'in_progress' ? 'var(--y)' : st === 'resolved' ? 'var(--g)' : 'var(--t3)';
    var catIcon = { bug: '🐛', suggestion: '💡', question: '❓', other: '📝' }[item.category] || '📝';
    var catLabel = { bug: t('category_bug'), suggestion: t('category_suggestion'), question: t('category_question'), other: t('category_other') }[item.category] || item.category;
    var timeStr = (item.created_at || '').slice(0, 19).replace('T', ' ');
    var updateTimeStr = (item.updated_at || '').slice(0, 19).replace('T', ' ');

    var html = '';

    // 返回按钮
    html += '<button class="bt bt-o bt-s" onclick="app.nav(\'my-feedback\')" style="margin-bottom:12px">' + t('back_to_list') + '</button>';

    // 标题行
    html += '<div class="cd" style="padding:20px 24px;margin-bottom:14px">' +
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">' +
      '<span style="color:' + stColor + ';font-weight:600;font-size:14px;flex-shrink:0">' + statusLabel(st) + '</span>' +
      '<h2 style="font-size:17px;font-weight:600;margin:0;flex:1">' + (item.title || '—') + '</h2>' +
      '</div>' +

      // 基本信息网格
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 20px;font-size:13px;color:var(--t2);margin-bottom:16px">' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">' + t('field_type') + '</span> ' + catIcon + ' ' + catLabel + '</div>' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">' + t('field_status') + '</span> <span style="color:' + stColor + ';font-weight:600">' + statusLabel(st) + '</span></div>' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">' + t('field_created_at') + '</span> ' + timeStr + '</div>' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">' + t('field_updated_at') + '</span> ' + updateTimeStr + '</div>' +
      '</div>' +
      '</div>';

    // 详细描述
    html += '<div class="cd" style="padding:16px 24px;margin-bottom:14px">' +
      '<div style="font-size:13px;font-weight:600;color:var(--t2);margin-bottom:8px">' + t('field_description') + '</div>' +
      '<div style="font-size:14px;color:var(--t1);white-space:pre-wrap;line-height:1.7">' + (item.description || '—') + '</div>' +
      '</div>';

    // 附件
    if (item.attachment_url) {
      var isImg = item.attachment_url.match(/\.(png|jpg|jpeg|webp|gif)$/i);
      html += '<div class="cd" style="padding:16px 24px;margin-bottom:14px">' +
        '<div style="font-size:13px;font-weight:600;color:var(--t2);margin-bottom:10px">' + t('field_attachment') + '</div>' +
        '<div style="display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap">';
      if (isImg) {
        html += '<img src="' + item.attachment_url + '" alt="' + (item.attachment_name || t('attachment_label')) + '" ' +
          'style="max-width:280px;max-height:280px;border-radius:8px;border:1px solid var(--b);cursor:pointer;object-fit:contain" ' +
          'onclick="window.open(\'' + item.attachment_url + '\',\'_blank\')" />';
      }
      html += '<div style="display:flex;align-items:center;gap:8px">' +
        '<span style="font-size:22px">' + (isImg ? '🖼️' : '📎') + '</span>' +
        '<a href="' + item.attachment_url + '" target="_blank" style="color:var(--a);font-weight:500;text-decoration:none">' +
        (item.attachment_name || t('attachment_label')) + '</a></div>';
      html += '</div></div>';
    }

    // 处理信息
    if (item.handler_name || item.handler_remark) {
      html += '<div class="cd" style="padding:16px 24px;margin-bottom:14px">' +
        '<div style="font-size:13px;font-weight:600;color:var(--t2);margin-bottom:10px">' + t('field_handler') + '</div>' +
        '<div style="font-size:13px;color:var(--t2);line-height:1.8">';
      if (item.handler_name) {
        html += '<div><span style="color:var(--t3)">' + t('field_handler_name') + '：</span>' + item.handler_name + '</div>';
      }
      if (item.handler_remark) {
        html += '<div><span style="color:var(--t3)">' + t('field_handler_remark') + '：</span>' + item.handler_remark + '</div>';
      }
      html += '</div></div>';
    }

    el.innerHTML = html;
  }
}
