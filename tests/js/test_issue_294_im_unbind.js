/*
 * Issue #294 回归测试：IM 渠道配置支持解绑
 *
 * 运行：node tests/js/test_issue_294_im_unbind.js
 *
 * 验证点（对应 Issue #294 验收标准）：
 *   1. im_channel.js：已绑定（app_id 非空）时渲染「解绑」按钮，未绑定时不渲染；
 *   2. unbindImChannelConfig 行为：confirm 二次确认 → PUT /chat/config（app_id/app_secret 置空）
 *      → 成功 toast + 重新拉取配置刷新状态；取消确认时不发请求；
 *   3. agents.js：VOD 已绑定时渲染「已绑定渠道」卡片 + 解绑按钮；未绑定不渲染；
 *   4. unbindVodIm 行为：PUT /agents/vod/{owner}/{repo} 且 body 仅含 im_unbind=true；
 *   5. 后端 chat.py：解绑分支补齐 global_im_bot_config / chat_conversations / unified_user_mapping 清理，
 *      且仅在「无其他用户引用」时删注册表行；
 *   6. 后端 agents.py：im_unbind 显式标记 + upsert 三字段显式传空串（None=保留，必须 ""）；
 *   7. locales zh/en：解绑文案键齐全（按钮/确认/成功/失败）。
 *
 * 策略：源码级断言（与 tests/js 现行范式一致）+ 解绑请求载荷行为断言。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const imSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/im_channel.js'), 'utf8');
const agentsSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/agents.js'), 'utf8');
const chatPy = fs.readFileSync(path.join(ROOT, 'src/api/routes/chat.py'), 'utf8');
const agentsPy = fs.readFileSync(path.join(ROOT, 'src/api/routes/agents.py'), 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #294: IM 渠道配置支持解绑 — 回归测试\n');

// ─── 测试 1：前端解绑入口仅在已绑定时渲染 ───
test('im_channel.js：isConfigured 时才渲染「解绑」按钮（未绑定无入口）', () => {
  const btnRe = /\$\{isConfigured \? `<button class="bt bt-r" onclick="unbindImChannelConfig\(\)"/;
  assert.ok(btnRe.test(imSrc), '解绑按钮应以 isConfigured 为条件渲染，并调用 unbindImChannelConfig()');
  assert.ok(imSrc.indexOf("${_imt('unbind')}") >= 0, '解绑按钮文案应走 i18n（chat.im_channel.unbind）');
  // 确认与保存按钮同排（同一操作行内）
  const row = imSrc.match(/<div style="display:flex;gap:10px">[\s\S]*?<\/div>/);
  assert.ok(row && row[0].indexOf('unbindImChannelConfig()') >= 0, '解绑按钮应与扫码/保存按钮同排渲染');
});

// ─── 测试 2：解绑请求载荷与刷新行为 ───
function extractFn(src, name) {
  const m = src.match(new RegExp('function ' + name + '\\(\\) \\{[\\s\\S]*?\\n\\}'));
  if (!m) throw new Error('未找到函数 ' + name);
  return m[0];
}

function runUnbind(confirmResult) {
  const calls = { put: null, toasts: [], reloads: 0 };
  const API = {
    put: (url, body) => {
      calls.put = { url: url, body: body };
      return { then: cb => { cb({}); return { catch: () => {} }; } };
    }
  };
  const app = {
    showToast: t => calls.toasts.push(t),
    showConfirm: (msg, onConfirm, opts) => { if (confirmResult) onConfirm(); },
    currentRepo: () => ({ fullName: 'grape-dev/grape' })
  };
  const fn = new Function(
    'API', 'app', '_imCurrentProvider', '_imt', 'loadImChannelConfig',
    extractFn(imSrc, 'unbindImChannelConfig') + '\nreturn unbindImChannelConfig;'
  )(API, app, 'feishu', k => 'T:' + k, () => { calls.reloads++; });
  fn();
  return calls;
}

test('unbindImChannelConfig：确认后 PUT /chat/config 置空 app_id/app_secret 并刷新状态', () => {
  const calls = runUnbind(true);
  assert.ok(calls.put, '确认后应发起 PUT /chat/config');
  assert.strictEqual(calls.put.url, '/chat/config?repo=grape-dev%2Fgrape', '应带当前仓库查询参数');
  assert.deepStrictEqual(calls.put.body, { im: { feishu: { app_id: '', app_secret: '' } } },
    '解绑应走 app_id 置空语义（复用既有 PUT 端点）');
  assert.deepStrictEqual(calls.toasts, ['T:unbind_success'], '成功后应提示 unbind_success');
  assert.strictEqual(calls.reloads, 1, '成功后应重新拉取配置刷新渠道状态');
});

test('unbindImChannelConfig：取消二次确认时不发请求', () => {
  const calls = runUnbind(false);
  assert.strictEqual(calls.put, null, '取消确认不应发起请求');
  assert.strictEqual(calls.reloads, 0, '取消确认不应刷新');
});

// ─── 测试 3/4：VOD 侧解绑入口与载荷 ───
test('agents.js：VOD 已绑定时渲染解绑卡片，未绑定不渲染', () => {
  assert.ok(/function _agentVodImSection\(data\)/.test(agentsSrc), '应存在 _agentVodImSection');
  assert.ok(/\$\{name === 'vod' \? _agentVodImSection\(data\) : ''\}/.test(agentsSrc), 'VOD Agent 页应接入该区块');
  assert.ok(/if \(!im\.im_app_id\) return '';/.test(agentsSrc), '未绑定（无 im_app_id）时应返回空，不渲染');
  assert.ok(agentsSrc.indexOf('onclick="unbindVodIm()"') >= 0, '卡片内应有解绑按钮');
});

test('unbindVodIm：PUT /agents/vod/{owner}/{repo}，body 仅含 im_unbind=true', () => {
  const body = extractFn(agentsSrc, 'unbindVodIm');
  assert.ok(/API\.put\('\/agents\/vod\/' \+ repo\.owner \+ '\/' \+ repo\.name, \{im_unbind: true\}\)/.test(body),
    '解绑应只带 im_unbind 标记，避免误清引擎/技能/订阅配置');
  assert.ok(/app\.showConfirm\(I18N\.t\('agents\.config\.unbind_confirm'\)/.test(body), 'VOD 解绑应二次确认');
});

// ─── 测试 5：Chat 后端解绑清理 ───
test('chat.py：解绑补齐注册表/会话/映射清理，且仅无其他引用时删注册表行', () => {
  assert.ok(/def _purge_chat_im_binding\(user_id: int, gitcode_user: str, old_app_id: str\)/.test(chatPy), '应存在解绑清理函数');
  const fn = chatPy.match(/def _purge_chat_im_binding[\s\S]*?\n\n\n@router\.put\("\/config"/);
  assert.ok(fn, '应能定位解绑清理实现');
  const body = fn[0];
  assert.ok(body.indexOf("SELECT COUNT(*) FROM chat_agent_session WHERE im_app_id=? AND user_id!=? AND im_app_id!=''") >= 0, '应按其他用户引用数判断');
  assert.ok(/if other_refs == 0:\s*\n\s*c\.execute\("DELETE FROM global_im_bot_config WHERE bot_app_id=\? AND bot_type='chat_bot'"/.test(body),
    '无其他引用时才删除 chat_bot 注册行');
  assert.ok(body.indexOf("DELETE FROM chat_conversations WHERE im_channel_id=?") >= 0, '应清理该渠道会话');
  assert.ok(body.indexOf("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id=''") >= 0, '应对账 unified_user_mapping');
  assert.ok(/"im" in body and not feishu\.get\("app_id"\) and old_app_id:\s*\n\s*_purge_chat_im_binding\(/.test(chatPy),
    '解绑请求（显式携带 im 且 app_id 置空）应触发清理');
  assert.ok(/kwargs\["im_provider"\] = ""/.test(chatPy), '解绑应同时清空 im_provider');
});

// ─── 测试 6：VOD 后端解绑 ───
test('agents.py：im_unbind 显式标记 + 三字段显式传空串（None 为保留语义）', () => {
  assert.ok(/im_unbind: bool = False/.test(agentsPy), 'AgentConfig 应含 im_unbind 显式标记（防普通保存误清）');
  assert.ok(/def _unbind_vod_im\(repo_key: str\) -> tuple\[str, str, bool\]/.test(agentsPy), '应存在 VOD 解绑清理函数');
  const fn = agentsPy.match(/def _unbind_vod_im[\s\S]*?\n\n\ndef _get_agent_type_meta/);
  assert.ok(fn, '应能定位 _unbind_vod_im 实现');
  const body = fn[0];
  assert.ok(body.indexOf("SET im_app_id='', im_app_secret='', im_provider=''") >= 0,
    '解绑必须显式写空串（upsert 传 None = 保留既有值，置空不生效）');
  assert.ok(/SELECT COUNT\(\*\) FROM devops_agent_session WHERE im_app_id=\? AND repo_key!=\?/.test(body), '应按其他仓库引用数判断');
  assert.ok(body.indexOf('DELETE FROM global_im_bot_config WHERE bot_app_id=?') >= 0, '无其他引用时才删注册表行');
  assert.ok(body.indexOf('DELETE FROM vod_conversations WHERE agent_session_id=?') >= 0, '应清理该仓库群会话');
  assert.ok(/if _vod_im_unbind and body\.backends is None and not body\.skill and not body\.skill_id/.test(agentsPy),
    '解绑-only 请求应只清 IM，保留引擎/技能/订阅配置');
});

// ─── 测试 7：双语 i18n 文案 ───
test('locales：zh/en 解绑文案键齐全（按钮/确认/成功/失败）', () => {
  const keys = ['unbind', 'unbind_confirm', 'unbind_success', 'unbind_failed'];
  const zh = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/chat.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/chat.json'), 'utf8'));
  keys.forEach(k => {
    assert.ok(zh.im_channel && zh.im_channel[k], 'zh chat.im_channel.' + k + ' 缺失');
    assert.ok(en.im_channel && en.im_channel[k], 'en chat.im_channel.' + k + ' 缺失');
  });
  const zhA = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/agents.json'), 'utf8'));
  const enA = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/agents.json'), 'utf8'));
  keys.forEach(k => {
    assert.ok(zhA.config && zhA.config[k], 'zh agents.config.' + k + ' 缺失');
    assert.ok(enA.config && enA.config[k], 'en agents.config.' + k + ' 缺失');
  });
  assert.strictEqual(zh.im_channel.unbind, '解绑', 'zh 解绑按钮文案');
  assert.strictEqual(en.im_channel.unbind, 'Unbind', 'en 解绑按钮文案');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
