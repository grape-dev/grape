const AGENT_META = {
  vod:{display:'VOD Agent',ico:'🎤',color:'var(--pk)',bg:'#fde8f0',bc:'var(--pk)',s:'Voice of Developer',
    sd:'从开发者声音中收集反馈, 聚类分析共性, 提炼价值, 自动创建 Issue'},
  issue:{display:'Issue Agent',ico:'🎫',color:'var(--r)',bg:'#fce5e5',bc:'var(--r)',s:'Issue Manager',
    sd:'处理 Issue, 分类打标签, 提炼为可开发需求, 指派负责人, 触发 Coding Agent'},
  coding:{display:'Coding Agent',ico:'⚙',color:'var(--a)',bg:'#e3edfd',bc:'var(--a)',s:'Developer',
    sd:'分析需求 → 设计方案 → 编码实现 → 自我审查 → 提交 Pull Request'},
  qa:{display:'QA Agent',ico:'✔',color:'var(--pu)',bg:'#f3e9fe',bc:'var(--pu)',s:'Quality Assurance',
    sd:'Code Review, 自动化测试, 功能验证, 安全检查, 确保代码质量'},
  sre:{display:'SRE Agent',ico:'🚀',color:'var(--or)',bg:'#fef0e4',bc:'var(--or)',s:'DevOps',
    sd:'构建部署, 发布管理, 监控运维, 故障排查, 合规检查'},
};

let _agentCfgVisible = false;
function toggleAgentCfg() {
  _agentCfgVisible = !_agentCfgVisible;
  const p = document.getElementById('agentCfgPanel');
  if (p) p.classList.toggle('hidden');
}

function renderAgent(name, data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const meta = AGENT_META[name] || {display:name,ico:'?',color:'var(--t2)',bg:'var(--bg)',bc:'var(--b)',s:'',sd:''};
  const m = data.metrics || {};
  const es = m.event_stats || {};
  const events = data.recent_events || [];
  const evtHtml = events.length > 0 ? events.map(ev =>
    `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
      <div class="t">${(ev.created_at || '').substring(11,16) || '--:--'}</div>
      <div class="ac">${ev.title || ev.event_type} <span class="ts ${ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'}">${ev.status}</span></div>
    </div>`
  ).join('') : '<div class="tx t2">暂无事件</div>';

  const mockEvts = [
    ['12:05','Signal: "CI pipeline too slow" → created <b>Issue #345</b>'],
    ['10:30','Analysis: 3 signals cluster around "test flakiness"'],
    ['08:00','Weekly report: <b>8 key signals</b>, 2 trending up'],
    ['Yest.','Collected 12 feedback from PR comments']
  ];
  const histHtml = (events.length > 0 ? evtHtml : mockEvts.map(e =>
    `<div class="ai"><div class="t">${e[0]}</div><div class="ac">${e[1]}</div></div>`
  ).join(''));

  const esKeys = Object.keys(es).filter(k => es[k] > 0);
  const metric1 = esKeys.length > 0 ? es.completed || 0 : (name === 'vod' ? '47' : name === 'issue' ? '3' : name === 'coding' ? '3' : name === 'qa' ? '4' : '3');
  const metric1Label = name === 'vod' ? 'Signals/Week' : name === 'issue' ? 'Active' : name === 'coding' ? 'Today' : name === 'qa' ? 'Today' : 'Deploys/Week';
  const metric2 = esKeys.length > 0 ? es.completed || 0 : (name === 'vod' ? '8' : name === 'issue' ? '12' : name === 'coding' ? '45s' : name === 'qa' ? '92%' : '99.9%');
  const metric2Label = name === 'vod' ? 'Issues Created' : name === 'issue' ? 'Resolved/Week' : name === 'coding' ? 'Avg Time' : name === 'qa' ? 'Pass Rate' : 'Uptime';
  const metric3 = esKeys.length > 0 ? m.total_events || 0 : (name === 'vod' ? '92%' : name === 'issue' ? '2m' : name === 'coding' ? '98%' : name === 'qa' ? '3' : '12s');
  const metric3Label = name === 'vod' ? 'Signal Quality' : name === 'issue' ? 'Avg Triage' : name === 'coding' ? 'First-Pass' : name === 'qa' ? 'Issues Caught' : 'MTTR';

  const backend = data.backend || (name === 'vod' ? 'Hermes' : name === 'coding' ? 'CodeArts' : 'OpenCode');
  const engineOptions = ['OpenCode','CodeArts','Hermes','Cursor'].map(e =>
    `<option ${backend === e ? 'selected' : ''}>${e}</option>`
  ).join('');

  const skillName = `Default · skills/${name}/SKILL.md`;
  const skillOptions = [`<option selected>${skillName}</option>`];
  if (name === 'issue') skillOptions.push('<option>Custom · custom-issue-skill</option>');
  skillOptions.push('<option>Custom · team/backend-service (uploaded)</option>');

  let html = `
<div class="cd" style="background:${meta.bg};border-color:${meta.bc}33">
  <div class="flex fc gap12">
    <div style="width:44px;height:44px;border-radius:10px;background:${meta.bc}22;display:flex;align-items:center;justify-content:center;font-size:20px">${meta.ico}</div>
    <div class="f1"><h2 style="font-size:16px;font-weight:600;margin-bottom:2px">${meta.display}</h2><div class="ts t2">${meta.s} · ${meta.sd}</div></div>
    <div class="flex gap6"><button class="bt bt-o bt-s" onclick="toggleAgentCfg()">Configure</button><button class="bt bt-p bt-s">Events</button></div>
  </div>
</div>
<div id="agentCfgPanel" class="cd hidden" style="padding:16px 20px;margin-bottom:14px">
  <h3 style="font-size:13px;font-weight:600;margin-bottom:12px">Backend Engine</h3>
  <div class="srow"><label>Engine</label><select style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)">${engineOptions}</select></div>
  <h3 style="font-size:13px;font-weight:600;margin:16px 0 12px">Skill（最多选一个，默认官方自带）</h3>
  <div class="srow"><label>Skill</label><select style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)">${skillOptions.join('')}</select></div>
  <div class="flex gap6 mt16"><button class="bt bt-p bt-s" onclick="saveAgentConfig('${name}')">Save</button><button class="bt bt-o bt-s" onclick="toggleAgentCfg()">Cancel</button></div>
</div>
<div class="stats mt16 mb16">
  <div class="st"><div class="v" style="color:${meta.color}">${metric1}</div><div class="l">${metric1Label}</div></div>
  <div class="st"><div class="v" style="color:${meta.color}">${metric2}</div><div class="l">${metric2Label}</div></div>
  <div class="st"><div class="v" style="color:${meta.color}">${metric3}</div><div class="l">${metric3Label}</div></div>
</div>
<div class="cd"><div class="cd-h"><h2>Event History</h2><span class="ts t3">Last 24h</span></div>${histHtml}</div>
<div class="cd"><div class="cd-h"><h2>Skill</h2></div><span class="tg tg-d" style="padding:4px 10px;font-size:11px">★ ${data.skills && data.skills.length > 0 ? data.skills[0].name : skillName}</span></div>`;

  el.innerHTML = html;
}

function saveAgentConfig(name) {
  const backend = document.querySelector('#agentCfgPanel select').value;
  const repo = app.currentRepo();
  if (!repo) return;
  API.put('/agents/' + name + '/' + repo.owner + '/' + repo.name, {backend}).then(r => {
    app.showToast('配置已保存');
    toggleAgentCfg();
  }).catch(e => app.showToast('保存失败: ' + e.message));
}
