/**
 * my_feedback.js — 工作台"我的反馈"列表页
 * 点击条目内联展开详情（不使用弹窗），支持分页
 */
function renderMyFeedback() {
  var el = document.getElementById('pageContent');
  if (!el) return;

  var _page = 0;
  var _size = 10;
  var _expandedId = 0;

  function load() {
    el.innerHTML = '<div class="loading">加载中...</div>';
    API.get('/feedback/mine?page=' + _page + '&size=' + _size)
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

    items.forEach(function (item) {
      var st = item.status;
      var stColor = st === 'open' ? 'var(--a)' : st === 'in_progress' ? 'var(--y)' : st === 'resolved' ? 'var(--g)' : 'var(--t3)';
      var stLabel = st === 'open' ? '待处理' : st === 'in_progress' ? '处理中' : st === 'resolved' ? '已解决' : '已关闭';
      var catIcon = { bug: '🐛', suggestion: '💡', question: '❓', other: '📝' }[item.category] || '📝';
      var catLabel = { bug: 'Bug 缺陷', suggestion: '功能建议', question: '使用疑问', other: '其他' }[item.category] || item.category;
      var timeStr = (item.created_at || '').slice(0, 16).replace('T', ' ');
      var isExpanded = _expandedId === item.id;

      // 卡片头（可点击展开/折叠）
      html += '<div style="border-bottom:1px solid var(--b)">' +
        '<div onclick="_myFbToggle(' + item.id + ')" style="padding:14px 18px;cursor:pointer;display:flex;align-items:center;gap:8px;transition:background .15s" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'\'">' +
        '<span style="color:' + stColor + ';font-weight:600;font-size:13px;flex-shrink:0">' + stLabel + '</span>' +
        '<span style="font-size:14px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (item.title || '—') + '</span>' +
        '<span class="ts t3" style="font-size:11px;flex-shrink:0">' + timeStr + '</span>' +
        '<span style="font-size:10px;color:var(--t3);transition:transform .2s;display:inline-block;transform:rotate(' + (isExpanded ? '90deg' : '0deg') + ')">▶</span>' +
        '</div>';

      // 详情展开区
      if (isExpanded) {
        html += '<div style="padding:0 18px 16px;background:var(--bg)">' +
          // 基本信息
          '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:12px;color:var(--t2);margin-bottom:12px;padding-top:12px">' +
          '<div><span style="color:var(--t3)">类型</span> ' + catIcon + ' ' + catLabel + '</div>' +
          '<div><span style="color:var(--t3)">状态</span> <span style="color:' + stColor + ';font-weight:600">' + stLabel + '</span></div>' +
          '<div><span style="color:var(--t3)">提交时间</span> ' + timeStr + '</div>' +
          '<div><span style="color:var(--t3)">更新时间</span> ' + ((item.updated_at || '').slice(0, 16).replace('T', ' ') || '—') + '</div>' +
          '</div>';

        // 详细描述
        if (item.description) {
          html += '<div style="font-size:12px;color:var(--t3);margin-bottom:4px">详细描述</div>' +
            '<div style="font-size:13px;color:var(--t1);padding:10px 12px;background:var(--c);border-radius:8px;border:1px solid var(--b);white-space:pre-wrap;line-height:1.6;margin-bottom:12px">' + item.description + '</div>';
        }

        // 附件
        if (item.attachment_url) {
          var isImg = item.attachment_url.match(/\.(png|jpg|jpeg|webp|gif)$/i);
          html += '<div style="font-size:12px;color:var(--t3);margin-bottom:4px">附件</div>' +
            '<div style="padding:10px 12px;background:var(--c);border-radius:8px;border:1px solid var(--b);margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">';
          if (isImg) {
            html += '<img src="' + item.attachment_url + '" alt="' + (item.attachment_name || '附件') + '" ' +
              'style="max-width:180px;max-height:180px;border-radius:6px;border:1px solid var(--b);cursor:pointer;object-fit:contain" ' +
              'onclick="window.open(\'' + item.attachment_url + '\',\'_blank\')" />';
          }
          html += '<div style="display:flex;align-items:center;gap:6px">' +
            '<span style="font-size:18px">' + (isImg ? '🖼️' : '📎') + '</span>' +
            '<a href="' + item.attachment_url + '" target="_blank" style="color:var(--a);font-weight:500;text-decoration:none">' +
            (item.attachment_name || '附件') + '</a></div>';
          html += '</div>';
        }

        // 处理信息
        if (item.handler_name || item.handler_remark) {
          html += '<div style="font-size:12px;color:var(--t3);margin-bottom:4px">处理信息</div>' +
            '<div style="padding:10px 12px;background:var(--c);border-radius:8px;border:1px solid var(--b);font-size:12px;color:var(--t2);line-height:1.8">';
          if (item.handler_name) {
            html += '<div><span style="color:var(--t3)">处理人：</span>' + item.handler_name + '</div>';
          }
          if (item.handler_remark) {
            html += '<div><span style="color:var(--t3)">备注：</span>' + item.handler_remark + '</div>';
          }
          html += '</div>';
        }

        html += '</div>';
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

  // 展开/折叠
  window._myFbToggle = function (id) {
    _expandedId = (_expandedId === id) ? 0 : id;
    load();
  };

  // 分页
  window._myFbPage = function (p) {
    _page = p;
    _expandedId = 0;
    load();
  };

  load();
}
