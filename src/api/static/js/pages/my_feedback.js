/**
 * my_feedback.js — 工作台"我的反馈"列表页
 * 点击条目跳转到详情页（app.nav('my-feedback-detail', id)），支持分页
 */
function renderMyFeedback() {
  var el = document.getElementById('pageContent');
  if (!el) return;

  var _page = 0;
  var _size = 10;

  function t(key, params) {
    var val = I18N.t('app.feedback.' + key, params);
    return val;
  }

  function statusLabel(st) {
    var map = { open: t('status_open'), in_progress: t('status_in_progress'), resolved: t('status_resolved'), closed: t('status_closed') };
    return map[st] || st;
  }

  function load() {
    el.innerHTML = '<div class="loading">' + t('loading') + '</div>';
    API.get('/feedback/mine?page=' + _page + '&size=' + _size)
      .then(function (resp) {
        renderList(resp);
      })
      .catch(function (err) {
        el.innerHTML = '<div class="ts t2" style="padding:20px">' + t('load_failed', { msg: err.message || '' }) + '</div>';
      });
  }

  function renderList(data) {
    var items = data.items || [];
    var total = data.total || 0;
    var totalPages = data.total_pages || 1;

    var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">' +
      '<h2 style="font-size:18px;font-weight:600;margin:0">' + t('my_feedback') + '</h2>' +
      '<span class="ts t3" style="font-size:13px">' + t('total_count', { n: total }) + '</span>' +
      '<div class="sp"></div>' +
      '<button class="bt bt-o" onclick="openFeedbackDialog()">' + t('new_feedback') + '</button>' +
      '</div>';

    if (!items.length) {
      html += '<div class="cd" style="padding:40px;text-align:center;color:var(--t3)">' +
        '<div style="font-size:32px;margin-bottom:8px">📝</div>' +
        '<div style="font-size:14px;margin-bottom:12px">' + t('no_feedback') + '</div>' +
        '<button class="bt bt-p" onclick="openFeedbackDialog()">' + t('submit_first') + '</button>' +
        '</div>';
      el.innerHTML = html;
      return;
    }

    html += '<div class="cd" style="padding:0">';

    items.forEach(function (item) {
      var st = item.status;
      var stColor = st === 'open' ? 'var(--a)' : st === 'in_progress' ? 'var(--y)' : st === 'resolved' ? 'var(--g)' : 'var(--t3)';
      var catIcon = { bug: '🐛', suggestion: '💡', question: '❓', other: '📝' }[item.category] || '📝';
      var timeStr = (item.created_at || '').slice(0, 16).replace('T', ' ');

      html += '<div onclick="app.nav(\'my-feedback-detail\',' + item.id + ')" ' +
        'style="padding:14px 18px;border-bottom:1px solid var(--b);cursor:pointer;transition:background .15s" ' +
        'onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'\'">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        '<span style="color:' + stColor + ';font-weight:600;font-size:13px;flex-shrink:0">' + statusLabel(st) + '</span>' +
        '<span style="font-size:14px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (item.title || '—') + '</span>' +
        '<span class="ts t3" style="font-size:11px;flex-shrink:0">' + timeStr + '</span>' +
        '<button class="bt bt-o bt-s" onclick="event.stopPropagation();_myFbDelete(' + item.id + ', event)" ' +
        'style="flex-shrink:0;color:#ff3b30;border-color:rgba(255,59,48,.3);font-size:11px">' + t('delete') + '</button>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2)">' +
        '<span>' + catIcon + ' ' + (item.category || '') + '</span>';

      if (item.attachment_url) {
        var isImg = item.attachment_url.match(/\.(png|jpg|jpeg|webp|gif)$/i);
        if (isImg) {
          html += '<span style="display:inline-flex;align-items:center;gap:4px">' +
            '<img src="' + item.attachment_url + '" style="width:24px;height:24px;border-radius:4px;object-fit:cover" />' +
            '<span class="ts t3">' + (item.attachment_name || t('attachment_label')) + '</span></span>';
        } else {
          html += '<span class="ts t3">📎 ' + (item.attachment_name || t('attachment_label')) + '</span>';
        }
      }

      if (item.handler_remark) {
        html += '<span class="ts t3" style="margin-left:auto">' + t('field_handler_remark') + ': ' + item.handler_remark.slice(0, 40) + (item.handler_remark.length > 40 ? '...' : '') + '</span>';
      }

      html += '</div>';

      if (item.description) {
        html += '<div class="ts t2" style="font-size:12px;margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + item.description + '</div>';
      }

      html += '</div>';
    });

    html += '</div>';

    // 分页
    if (totalPages > 1) {
      html += '<div style="display:flex;align-items:center;gap:12px;padding:12px 0;font-size:12px;color:var(--t2)">' +
        '<span>' + t('page_info', { cur: _page + 1, total: totalPages }) + '</span>' +
        '<button class="bt bt-o bt-s" ' + (_page <= 0 ? 'disabled style="opacity:.4"' : '') + ' onclick="_myFbPage(' + (_page - 1) + ')">' + t('prev_page') + '</button>' +
        '<button class="bt bt-o bt-s" ' + (_page >= totalPages - 1 ? 'disabled style="opacity:.4"' : '') + ' onclick="_myFbPage(' + (_page + 1) + ')">' + t('next_page') + '</button>' +
        '</div>';
    }

    el.innerHTML = html;
  }

  window._myFbPage = function (p) {
    _page = p;
    load();
  };

  window._myFbDelete = function (fid, event) {
    if (window._grapeConfirm) {
      _grapeConfirm({
        title: t('delete_confirm_title'),
        message: t('delete_confirm_msg'),
        confirmText: t('delete'),
        danger: true,
        onConfirm: function () {
          API.deleteFeedback(fid)
            .then(function () {
              load();
            })
            .catch(function (err) {
              el.innerHTML = '<div class="ts t2" style="padding:20px">' + t('delete_failed', { msg: err.message || '' }) + '</div>';
              setTimeout(load, 2000);
            });
        },
      }, event);
    } else {
      app.showConfirm(t('delete_confirm_msg'), function() {
        API.deleteFeedback(fid).then(function () { load(); });
      }, {danger: true});
    }
  };

  load();
}
