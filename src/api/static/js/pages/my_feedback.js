/**
 * my_feedback.js — 工作台"我的反馈"列表页
 * 在 app.js VIEWS 中注册为 'my-feedback'
 */
function renderMyFeedback() {
  var el = document.getElementById('pageContent');
  if (!el) return;

  var _page = 0;
  var _size = 10;

  function load() {
    el.innerHTML = '<div class="loading">加载中...</div>';
    API.get('/feedback/mine', { params: { page: _page, size: _size } })
      .then(function (resp) {
        renderList(resp);
      })
      .catch(function (err) {
        el.innerHTML = '<div class="ts t2" style="padding:20px">加载失败: ' + (err.message || '') + '</div>';
      });
  }

  function renderList(data) {
    var items = data.items || [];
    var total = data.total || 0;
    var totalPages = data.total_pages || 1;

    var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">' +
      '<h2 style="font-size:18px;font-weight:600;margin:0">我的反馈</h2>' +
      '<span class="ts t3" style="font-size:13px">共 ' + total + ' 条</span>' +
      '<div class="sp"></div>' +
      '<button class="bt bt-o" onclick="openFeedbackDialog()">+ 新建反馈</button>' +
      '</div>';

    if (!items.length) {
      html += '<div class="cd" style="padding:40px;text-align:center;color:var(--t3)">' +
        '<div style="font-size:32px;margin-bottom:8px">📝</div>' +
        '<div style="font-size:14px;margin-bottom:12px">暂无反馈记录</div>' +
        '<button class="bt bt-p" onclick="openFeedbackDialog()">提交第一条反馈</button>' +
        '</div>';
      el.innerHTML = html;
      return;
    }

    html += '<div class="cd" style="padding:0">';

    // 列表卡片
    items.forEach(function (item) {
      var st = item.status;
      var stColor = st === 'open' ? 'var(--a)' : st === 'in_progress' ? 'var(--y)' : st === 'resolved' ? 'var(--g)' : 'var(--t3)';
      var stLabel = st === 'open' ? '待处理' : st === 'in_progress' ? '处理中' : st === 'resolved' ? '已解决' : '已关闭';
      var catIcon = { bug: '🐛', suggestion: '💡', question: '❓', other: '📝' }[item.category] || '📝';
      var timeStr = (item.created_at || '').slice(0, 16).replace('T', ' ');

      html += '<div style="padding:14px 18px;border-bottom:1px solid var(--b)">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        '<span style="color:' + stColor + ';font-weight:600;font-size:13px">' + stLabel + '</span>' +
        '<span style="font-size:14px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (item.title || '—') + '</span>' +
        '<span class="ts t3" style="font-size:11px">' + timeStr + '</span>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2)">' +
        '<span>' + catIcon + ' ' + (item.category || '') + '</span>';

      if (item.attachment_url) {
        var isImg = item.attachment_url.match(/\.(png|jpg|jpeg|webp|gif)$/i);
        if (isImg) {
          html += '<span style="display:inline-flex;align-items:center;gap:4px">' +
            '<img src="' + item.attachment_url + '" style="width:24px;height:24px;border-radius:4px;object-fit:cover" />' +
            '<span class="ts t3">' + (item.attachment_name || '附件') + '</span></span>';
        } else {
          html += '<a href="' + item.attachment_url + '" target="_blank" style="color:var(--a);text-decoration:none">📎 ' + (item.attachment_name || '附件') + '</a>';
        }
      }

      if (item.handler_remark) {
        html += '<span class="ts t3" style="margin-left:auto">处理: ' + item.handler_remark.slice(0, 40) + (item.handler_remark.length > 40 ? '...' : '') + '</span>';
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
        '<span>第 ' + (_page + 1) + ' / ' + totalPages + ' 页</span>' +
        '<button class="bt bt-o bt-s" ' + (_page <= 0 ? 'disabled style="opacity:.4"' : '') + ' onclick="_myFbPage(' + (_page - 1) + ')">上一页</button>' +
        '<button class="bt bt-o bt-s" ' + (_page >= totalPages - 1 ? 'disabled style="opacity:.4"' : '') + ' onclick="_myFbPage(' + (_page + 1) + ')">下一页</button>' +
        '</div>';
    }

    el.innerHTML = html;
  }

  // 暴露分页函数
  window._myFbPage = function (p) {
    _page = p;
    load();
  };

  load();
}
