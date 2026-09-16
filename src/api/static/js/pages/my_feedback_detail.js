/**
 * my_feedback_detail.js — 工作台"反馈详情"页面
 * 通过 app.nav('my-feedback-detail', id) 导航进入
 */
function renderMyFeedbackDetail(feedbackId) {
  var el = document.getElementById('pageContent');
  if (!el) return;

  if (!feedbackId) {
    el.innerHTML = '<div class="ts t2" style="padding:20px">缺少反馈 ID</div>';
    return;
  }

  el.innerHTML = '<div class="loading">加载中...</div>';

  // 从 /feedback/mine 拉取列表后在内存中查找（当前用户只能看自己的反馈）
  API.get('/feedback/mine?page=0&size=100')
    .then(function (resp) {
      var items = resp.items || [];
      var item = null;
      for (var i = 0; i < items.length; i++) {
        if (String(items[i].id) === String(feedbackId)) { item = items[i]; break; }
      }
      if (!item) {
        el.innerHTML = '<div class="ts t2" style="padding:20px">反馈记录不存在或无权查看</div>';
        return;
      }
      renderDetail(item);
    })
    .catch(function (err) {
      el.innerHTML = '<div class="ts t2" style="padding:20px">加载失败: ' + (err.message || '') + '</div>';
    });

  function renderDetail(item) {
    var st = item.status;
    var stColor = st === 'open' ? 'var(--a)' : st === 'in_progress' ? 'var(--y)' : st === 'resolved' ? 'var(--g)' : 'var(--t3)';
    var stLabel = st === 'open' ? '待处理' : st === 'in_progress' ? '处理中' : st === 'resolved' ? '已解决' : '已关闭';
    var catIcon = { bug: '🐛', suggestion: '💡', question: '❓', other: '📝' }[item.category] || '📝';
    var catLabel = { bug: 'Bug 缺陷', suggestion: '功能建议', question: '使用疑问', other: '其他' }[item.category] || item.category;
    var timeStr = (item.created_at || '').slice(0, 19).replace('T', ' ');
    var updateTimeStr = (item.updated_at || '').slice(0, 19).replace('T', ' ');

    var html = '';

    // 返回按钮
    html += '<button class="bt bt-o bt-s" onclick="app.nav(\'my-feedback\')" style="margin-bottom:12px">← 返回我的反馈</button>';

    // 标题行
    html += '<div class="cd" style="padding:20px 24px;margin-bottom:14px">' +
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">' +
      '<span style="color:' + stColor + ';font-weight:600;font-size:14px;flex-shrink:0">' + stLabel + '</span>' +
      '<h2 style="font-size:17px;font-weight:600;margin:0;flex:1">' + (item.title || '—') + '</h2>' +
      '</div>' +

      // 基本信息网格
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 20px;font-size:13px;color:var(--t2);margin-bottom:16px">' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">类型</span> ' + catIcon + ' ' + catLabel + '</div>' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">状态</span> <span style="color:' + stColor + ';font-weight:600">' + stLabel + '</span></div>' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">提交时间</span> ' + timeStr + '</div>' +
      '<div><span style="color:var(--t3);display:inline-block;min-width:60px">更新时间</span> ' + updateTimeStr + '</div>' +
      '</div>' +
      '</div>';

    // 详细描述
    html += '<div class="cd" style="padding:16px 24px;margin-bottom:14px">' +
      '<div style="font-size:13px;font-weight:600;color:var(--t2);margin-bottom:8px">详细描述</div>' +
      '<div style="font-size:14px;color:var(--t1);white-space:pre-wrap;line-height:1.7">' + (item.description || '—') + '</div>' +
      '</div>';

    // 附件
    if (item.attachment_url) {
      var isImg = item.attachment_url.match(/\.(png|jpg|jpeg|webp|gif)$/i);
      html += '<div class="cd" style="padding:16px 24px;margin-bottom:14px">' +
        '<div style="font-size:13px;font-weight:600;color:var(--t2);margin-bottom:10px">附件</div>' +
        '<div style="display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap">';
      if (isImg) {
        html += '<img src="' + item.attachment_url + '" alt="' + (item.attachment_name || '附件') + '" ' +
          'style="max-width:280px;max-height:280px;border-radius:8px;border:1px solid var(--b);cursor:pointer;object-fit:contain" ' +
          'onclick="window.open(\'' + item.attachment_url + '\',\'_blank\')" />';
      }
      html += '<div style="display:flex;align-items:center;gap:8px">' +
        '<span style="font-size:22px">' + (isImg ? '🖼️' : '📎') + '</span>' +
        '<a href="' + item.attachment_url + '" target="_blank" style="color:var(--a);font-weight:500;text-decoration:none">' +
        (item.attachment_name || '附件') + '</a></div>';
      html += '</div></div>';
    }

    // 处理信息
    if (item.handler_name || item.handler_remark) {
      html += '<div class="cd" style="padding:16px 24px;margin-bottom:14px">' +
        '<div style="font-size:13px;font-weight:600;color:var(--t2);margin-bottom:10px">处理信息</div>' +
        '<div style="font-size:13px;color:var(--t2);line-height:1.8">';
      if (item.handler_name) {
        html += '<div><span style="color:var(--t3)">处理人：</span>' + item.handler_name + '</div>';
      }
      if (item.handler_remark) {
        html += '<div><span style="color:var(--t3)">备注：</span>' + item.handler_remark + '</div>';
      }
      html += '</div></div>';
    }

    el.innerHTML = html;
  }
}
