/**
 * System Health 页面——系统级健康监控
 * 调 /api/health/detail + /api/dashboard/* 渲染
 */

function renderHealth(data) {
  const pc = document.getElementById('pageContent');
  if (!pc) return;

  const h = data.health || {};
  const r = data.runtime || {};
  const td = data.taskDaily || {};
  const ta = data.taskByAgent || {};
  const fa = data.failuresByAgent || {};

  // 健康状态灯
  const light = (ok, label) => `<div class="health-light ${ok ? 'ok' : 'error'}">${ok ? '🟢' : '🔴'} ${label}</div>`;
  const dbOk = h.database && h.database.status === 'ok';
  const obsOk = h.obs && h.obs.enabled ? h.obs.reachable : true;
  const workersOk = (h.workers || {}).online > 0;

  // Worker 槽位利用率
  const busy = r.busy_slots || 0;
  const maxC = r.max_concurrent || 1;
  const util = maxC > 0 ? Math.round(busy / maxC * 100) : 0;

  // 任务统计汇总
  const tdRows = (td.data || []).map(d => 
    `<tr><td>${d.date}</td><td>${d.agent_type}</td><td>${d.total}</td><td style="color:var(--gn)">${d.completed}</td><td style="color:var(--r)">${d.failed}</td><td>${d.avg_duration}s</td></tr>`
  ).join('');

  const taRows = (ta.data || []).map(d =>
    `<tr><td>${d.agent_type}</td><td>${d.total}</td><td style="color:var(--gn)">${d.completed}</td><td style="color:var(--r)">${d.failed}</td><td>${d.avg_duration}s</td></tr>`
  ).join('');

  const faRows = (fa.data || []).map(d =>
    `<tr><td>${d.agent_type}</td><td>${d.exit_code}</td><td style="color:var(--r)">${d.total}</td></tr>`
  ).join('');

  pc.innerHTML = `
    <div class="health-grid">
      <div class="health-card">
        <h3>系统健康</h3>
        <div class="health-lights">
          ${light(true, '服务端')}
          ${light(dbOk, '数据库')}
          ${light(obsOk, h.obs && h.obs.enabled ? 'OBS' : 'OBS (未启用)')}
          ${light(workersOk, `Worker (${(h.workers||{}).online||0}/${(h.workers||{}).total||0})`)}
        </div>
        <div class="health-ts">${h.timestamp || ''}</div>
      </div>

      <div class="health-card">
        <h3>Worker 状态</h3>
        <div class="worker-stats">
          <div class="ws-row"><span>在线</span><b style="color:var(--gn)">${r.online || 0}</b></div>
          <div class="ws-row"><span>离线</span><b style="color:var(--r)">${r.offline || 0}</b></div>
          <div class="ws-row"><span>繁忙槽位</span><b>${busy} / ${maxC}</b></div>
          <div class="ws-bar-wrap"><div class="ws-bar" style="width:${util}%"></div></div>
          <div class="ws-util">利用率 ${util}%</div>
        </div>
      </div>
    </div>

    <div class="health-section">
      <h3>每日任务统计（${td.days || 7} 天）</h3>
      ${tdRows ? `<table class="stat-table"><thead><tr><th>日期</th><th>Agent</th><th>总数</th><th>成功</th><th>失败</th><th>平均时长</th></tr></thead><tbody>${tdRows}</tbody></table>` : '<div class="empty">暂无数据</div>'}
    </div>

    <div class="health-section">
      <h3>Agent 统计（${ta.days || 7} 天）</h3>
      ${taRows ? `<table class="stat-table"><thead><tr><th>Agent</th><th>总数</th><th>成功</th><th>失败</th><th>平均时长</th></tr></thead><tbody>${taRows}</tbody></table>` : '<div class="empty">暂无数据</div>'}
    </div>

    <div class="health-section">
      <h3>失败分析（${fa.days || 7} 天）</h3>
      ${faRows ? `<table class="stat-table"><thead><tr><th>Agent</th><th>Exit Code</th><th>失败数</th></tr></thead><tbody>${faRows}</tbody></table>` : '<div class="empty">暂无失败记录</div>'}
    </div>
  `;
}
