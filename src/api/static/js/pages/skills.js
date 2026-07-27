const SKILL_CATEGORIES = [
  {name:'VOD Agent',ico:'🎤',c:'#ec4899',tag:'tg-v',skills:[
    {n:'声音分析',desc:'收集、聚类、提炼开发者声音中的共性问题',type:'官方技能'},
    {n:'反馈收集',desc:'从 IM 和 PR 评论中收集反馈信息',type:'用户上传'},
  ]},
  {name:'Issue Agent',ico:'🎫',c:'#ef4444',tag:'tg-i',skills:[
    {n:'Issue 管理',desc:'分类、标签、指派、A2A 触发 Coding Agent',type:'官方技能'},
    {n:'自定义 Issue 处理',desc:'包含特定项目的 Issue 处理规则',type:'用户上传'},
  ]},
  {name:'Coding Agent',ico:'⚙',c:'#3b82f6',tag:'tg-c',skills:[
    {n:'代码实现',desc:'需求分析→方案设计→编码实现→PR提交',type:'官方技能'},
  ]},
  {name:'QA Agent',ico:'✔',c:'#8b5cf6',tag:'tg-q',skills:[
    {n:'PR Review',desc:'Code Review、自动化测试、功能验证',type:'官方技能'},
    {n:'自定义审查规则',desc:'特定项目代码审查规则',type:'用户上传'},
  ]},
  {name:'SRE Agent',ico:'🚀',c:'#f97316',tag:'tg-s',skills:[
    {n:'部署发布',desc:'CHANGELOG生成→构建→部署→监控',type:'官方技能'},
    {n:'合规检查',desc:'部署前安全与合规门禁',type:'官方技能'},
  ]},
];

function renderSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  let html = '<div style="margin-bottom:12px"><input placeholder="搜索技能..." style="padding:8px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:260px"></div>';
  SKILL_CATEGORIES.forEach(cat => {
    html += `<div style="margin-bottom:18px">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:10px;color:${cat.c}">${cat.ico} ${cat.name} Skills</h3>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">`;
    cat.skills.forEach(s => {
      html += `<div class="cd" style="padding:14px;cursor:pointer">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="color:${cat.c}">${cat.ico}</span>
          <span style="font-size:12px;font-weight:600">${s.n}</span>
          <span class="tg ${cat.tag}" style="margin-left:auto;font-size:9px">${s.type}</span>
        </div>
        <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:8px">${s.desc}</div>
        <div style="display:flex;gap:6px"><button class="bt bt-o bt-s">View</button><button class="bt bt-o bt-s">Edit</button></div>
      </div>`;
    });
    html += `</div></div>`;
  });
  el.innerHTML = html;
}
