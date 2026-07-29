let _skillDetailVisible = false;

function renderSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';

  API.get('/skills').then(d => {
    const skills = d.skills || [];
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
        html += `<div class="cd skill-card" style="padding:14px;cursor:pointer" data-name="${s.name}" data-agent="${at}" onclick="app.nav('skill','${s.name}')">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="color:${color}">${ico}</span>
            <span style="font-size:12px;font-weight:600">${s.display_name || s.name}</span>
            <span class="tg ${tagClass}" style="margin-left:auto;font-size:9px">${s.type}</span>
          </div>
          <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:8px">${s.description}</div>
          <div><button class="bt bt-o bt-s" onclick="event.stopPropagation();app.nav('skill','${s.name}')">View</button></div>
        </div>`;
      });
      html += `</div></div>`;
    });

    el.innerHTML = html;
  }).catch(() => {
    el.innerHTML = '<div class="error">加载技能列表失败</div>';
  });
}

function renderSkillDetail(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const META = _AGENT_TYPES || {};
  const meta = META[data.agent_type] || {};
  const color = meta.color || 'var(--t2)';
  const ico = meta.icon || '◈';
  const tagClass = data.type === 'official' ? 'tg-c' : 'tg-d';

  const content = (data.content || '').replace(/`/g, '\\`').replace(/\${/g, '\\${');
  el.innerHTML = `
    <button class="bt bt-o bt-s" onclick="app.nav('skills')" style="margin-bottom:12px">← 返回 Skills Library</button>
    <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
      <div class="cd" style="padding:16px 20px;overflow-y:auto">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <span style="color:${color};font-size:22px">${ico}</span>
          <div>
            <h3 style="font-size:15px;font-weight:600">${data.display_name || data.name}</h3>
            <span class="tg ${tagClass}" style="font-size:9px">${data.type}</span>
          </div>
        </div>
        <div style="font-size:12px;color:var(--t2);margin-bottom:14px;line-height:1.5">${data.description}</div>
        <h3 style="font-size:13px;font-weight:600;margin-bottom:6px">SKILL.md</h3>
        <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${content || '(无详细内容)'}</pre>
      </div>
      <div class="cd" style="padding:16px 20px;font-size:12px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">Skill Info</h3>
        <div class="srow"><label>Name</label><span>${data.name}</span></div>
        <div class="srow"><label>Agent Type</label><span>${data.agent_type}</span></div>
        <div class="srow"><label>Type</label><span>${data.type}</span></div>
        <div class="srow"><label>Description</label><span class="t2">${data.description || '—'}</span></div>
      </div>
    </div>`;
}

function filterSkills(input) {
  const q = (input.value || '').toLowerCase();
  document.querySelectorAll('.skill-card').forEach(card => {
    const name = (card.dataset.name || '').toLowerCase();
    const agent = (card.dataset.agent || '').toLowerCase();
    card.style.display = (name.includes(q) || agent.includes(q)) ? '' : 'none';
  });
}