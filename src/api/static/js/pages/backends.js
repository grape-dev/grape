function renderBackends(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const engines = data.backends || [];
  const defaultEngines = [
    {n:'OpenCode',v:'1.5.2',path:'/usr/local/bin/opencode',running:true,installed:true,hasAK:false},
    {n:'CodeArts',v:'2.1.0',path:'/usr/local/bin/codearts',running:true,installed:true,hasAK:true,ak:'AK-demo-access-key-xxxx',sk:'SK-demo-secret-key-xxxx'},
    {n:'Hermes',v:'0.9.1',path:'/usr/local/bin/hermes',running:true,installed:true,hasAK:false},
    {n:'Cursor',v:'',path:'',running:false,installed:false,hasAK:false},
  ];

  let html = '';
  const engineList = engines.length > 0 ? engines.map((e, i) => {
    const def = defaultEngines[i] || {};
    const running = e.healthy || def.running;
    const installed = e.installed !== false;
    return {name: e.name || def.n, version: e.version || def.v, path: e.cli_path || def.path, running, installed, hasAK: def.hasAK, ak: def.ak, sk: def.sk};
  }) : defaultEngines;

  engineList.forEach(e => {
    html += `<div class="cd" style="padding:16px 20px">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
        <span style="width:10px;height:10px;border-radius:50%;background:${e.running ? 'var(--g)' : 'var(--t3)'};display:inline-block"></span>
        <h3 style="font-size:14px;font-weight:600">${e.name}</h3>
        <span class="ts t2" style="margin-left:8px">${e.installed ? 'Version: ' + e.version : 'Not Installed'}</span>
      </div>
      ${!e.installed ? `
        <div class="srow"><label></label><button class="bt bt-o bt-s">Install Guide</button></div>
      ` : `
        <div class="srow"><label>Installed</label><span class="tgx">✓</span> Path: ${e.path}</div>
        ${e.hasAK ? `
          <div class="srow"><label>AK</label><input type="password" value="${e.ak || ''}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
          <div class="srow"><label>SK</label><input type="password" value="${e.sk || ''}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
        ` : ''}
        <div class="flex gap6" style="margin-left:120px">
          <button class="bt bt-o bt-s" onclick="app.showToast('连接测试功能开发中')">Test Connection</button>
          <button class="bt bt-o bt-s" onclick="app.showToast('日志功能开发中')">View Logs</button>
          ${e.hasAK ? '<button class="bt bt-p bt-s" onclick="app.showToast(\'AK/SK 已保存\')">Save AK/SK</button>' : ''}
        </div>
      `}
    </div>`;
  });
  html += '<button class="bt bt-o" style="margin-top:8px" onclick="app.nav(\'backends\')">⟳ Refresh All</button>';
  el.innerHTML = html;
}
