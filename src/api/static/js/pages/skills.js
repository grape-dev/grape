function renderSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';

  API.get('/skills').then(d => {
    const skills = d.skills || [];
    // Group by agent_type
    const groups = {};
    skills.forEach(s => {
      const at = s.agent_type || '__uncategorized__';
      if (!groups[at]) groups[at] = [];
      groups[at].push(s);
    });

    const META = _AGENT_TYPES || {};
    const LABELS = {vod:'VOD Agent',issue:'Issue Agent',coding:'Coding Agent',qa:'QA Agent',sre:'SRE Agent',chat:'Chat Agent'};

    let html = '<div style="margin-bottom:12px"><input placeholder="搜索技能..." id="skillSearch" onkeyup="filterSkills(this)" style="padding:8px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:260px"></div>';

    Object.keys(groups).forEach(at => {
      const meta = META[at] || {};
      const label = meta.display_name || LABELS[at] || at;
      const color = meta.color || 'var(--t2)';
      const ico = meta.icon || '◈';
      const groupSkills = groups[at];
      if (!groupSkills || groupSkills.length === 0) return;

      html += `<div class="skill-group" style="margin-bottom:18px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px;color:${color}">${ico} ${label} Skills</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">`;
      groupSkills.forEach(s => {
        const tagClass = s.type === 'official' ? 'tg-c' : 'tg-d';
        html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${s.name}" data-agent="${at}">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="color:${color}">${ico}</span>
            <span style="font-size:12px;font-weight:600">${s.display_name || s.name}</span>
            <span class="tg ${tagClass}" style="margin-left:auto;font-size:9px">${s.type}</span>
          </div>
          <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:8px">${s.description}</div>
          <div style="display:flex;gap:6px"><button class="bt bt-o bt-s">View</button></div>
        </div>`;
      });
      html += `</div></div>`;
    });

    el.innerHTML = html;
  }).catch(() => {
    el.innerHTML = '<div class="error">加载技能列表失败</div>';
  });
}

function filterSkills(input) {
  const q = (input.value || '').toLowerCase();
  document.querySelectorAll('.skill-card').forEach(card => {
    const name = (card.dataset.name || '').toLowerCase();
    const agent = (card.dataset.agent || '').toLowerCase();
    card.style.display = (name.includes(q) || agent.includes(q)) ? '' : 'none';
  });
}