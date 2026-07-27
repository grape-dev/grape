function renderSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `<div class="cd"><div class="cd-h"><h2>DevOps Skills Library</h2></div>
  <div id="skillsList"><div class="loading">加载中...</div></div></div>`;

  API.get('/skills/').then(d => {
    const skills = d.skills || [];
    let html = '';
    const groups = {};
    skills.forEach(s => {
      const g = s.agent_type || 'other';
      if (!groups[g]) groups[g] = [];
      groups[g].push(s);
    });
    Object.entries(groups).forEach(([g, items]) => {
      html += `<div class="cd-h" style="margin-top:12px"><h2 class="t2">${g.toUpperCase()}</h2></div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">`;
      items.forEach(s => {
        html += `<div style="border:1px solid var(--b);border-radius:8px;padding:8px 12px;width:calc(33% - 8px);min-width:180px">
          <div style="font-size:12px;font-weight:600;margin-bottom:2px">${s.name}</div>
          <div style="font-size:10px;color:var(--t2)">${s.description || ''}</div>
          <div style="font-size:9px;color:var(--t3);margin-top:4px"><span class="tg tg-d">${s.type || 'official'}</span></div>
        </div>`;
      });
      html += `</div>`;
    });
    document.getElementById('skillsList').innerHTML = html || '<div class="ts t2">暂无技能</div>';
  }).catch(e => document.getElementById('skillsList').innerHTML = `<div class="error">加载失败: ${e.message}</div>`);
}
