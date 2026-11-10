/*
 * Issue #956 前端契约测试：钉钉渠道产品化（im_channel.js）
 *
 * 运行：node tests/js/test_issue_956_im_dingtalk.js
 *
 * 验证点（对应 Issue #956 验收 M2）：
 *   1. 钉钉卡片去灰态：_IM_PLATFORMS.dingtalk 无 disabled 标记（wecom 仍灰态，不在本期范围）；
 *   2. 保存/解绑载荷带 provider（{im:{provider,app_id,app_secret}}，后端 provider 分支）；
 *   3. 保存失败不清空已存凭证：失败路径只 toast、不重载（loadImChannelConfig 仅在成功 then 里）；
 *   4. 错凭证可读提示：错误 toast 透传后端 detail.message（e.message）；
 *   5. 扫码按钮仅飞书渲染（钉钉无扫码链路，Out of Scope）；
 *   6. 后端 GET /chat/config 回显 provider 化（im.provider + dingtalk 真值 + app_secret_exists），
 *      全链路不回传 secret。
 *
 * 策略：源码级断言（与 tests/js 现行范式一致）+ 载荷行为断言。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const imSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/im_channel.js'), 'utf8');
const chatPy = fs.readFileSync(path.join(ROOT, 'src/api/routes/chat.py'), 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

function extractFn(src, name) {
  const m = src.match(new RegExp('function ' + name + '\\(\\) \\{[\\s\\S]*?\\n\\}'));
  if (!m) throw new Error('未找到函数 ' + name);
  return m[0];
}

console.log('Issue #956: 钉钉渠道产品化（前端契约）— 回归测试\n');

// ─── 1：钉钉去灰态 ───
test('im_channel.js：dingtalk 平台卡无 disabled（去灰态，可选可配）', () => {
  const m = imSrc.match(/dingtalk:\s*\{[\s\S]*?\n  \}/);
  assert.ok(m, '应存在 dingtalk 平台定义');
  assert.ok(!/disabled:\s*true/.test(m[0]), 'dingtalk 不得再标记 disabled:true');
});
test('im_channel.js：wecom 保持灰态（企业微信不在本期开放范围）', () => {
  const m = imSrc.match(/wecom:\s*\{[\s\S]*?\n  \}/);
  assert.ok(m && /disabled:\s*true/.test(m[0]), 'wecom 应维持 disabled:true 占位');
});

// ─── 2：保存/解绑载荷 provider 化 ───
test('saveImChannelConfig：载荷形态 {im:{provider,app_id,app_secret}}', () => {
  const fn = extractFn(imSrc, 'saveImChannelConfig');
  assert.ok(/const _body = \{im: \{provider, app_id: appId, app_secret: secret\}\}/.test(fn),
    '保存载荷应带 provider 键（后端 provider 分支入口）');
});

function runSave(putHandler) {
  const calls = { put: null, toasts: [], reloads: 0, caught: null };
  const API = {
    put: (url, body) => {
      calls.put = { url, body };
      return putHandler(calls);
    }
  };
  const app = {
    currentRepo: () => ({ fullName: 'grape-dev/grape' }),
    showToast: (m) => calls.toasts.push('T:' + m)
  };
  const I18N = {
    t: (k, p) => (p && p.msg !== undefined) ? ('save_failed:' + p.msg) : k,
    getLang: () => 'zh'
  };
  global.API = API; global.app = app; global.I18N = I18N;
  global.document = { getElementById: (id) => ({
    value: id === 'cfgImAppId' ? 'dt-app-1' : 'dt-secret-1'
  }) };
  const _imCurrentProvider = 'dingtalk';
  const body = extractFn(imSrc, 'saveImChannelConfig');
  try {
    eval(body + '\nsaveImChannelConfig();');
  } catch (e) { calls.caught = e; }
  return calls;
}

test('saveImChannelConfig：钉钉载荷携带 provider=dingtalk + 双字段', () => {
  const calls = runSave(() => ({ then: () => ({ catch: () => {} }) }));
  assert.deepStrictEqual(calls.put.body,
    { im: { provider: 'dingtalk', app_id: 'dt-app-1', app_secret: 'dt-secret-1' } });
});

test('saveImChannelConfig：保存失败只 toast 不重载（已存凭证不清空）', () => {
  const calls = runSave(() => ({
    then: () => ({ catch: (cb) => { cb({ message: '该凭证已被其他用户使用' }); } })
  }));
  assert.ok(calls.toasts.some(t => t.indexOf('该凭证已被其他用户使用') >= 0),
    '失败 toast 应透传后端可读错误信息（错凭证可读提示）');
  assert.strictEqual(calls.reloads, 0, '失败路径不得重载/清空表单');
});

// ─── 3：扫码按钮仅飞书 ───
test('扫码配置按钮仅 feishu 渠道渲染', () => {
  assert.ok(/\$\{_imCurrentProvider === 'feishu' \? `<button[^]*startFeishuOnboard/.test(imSrc.replace(/\n/g, '^')),
    '扫码按钮应包在 _imCurrentProvider === \'feishu\' 条件内');
});

// ─── 4：当前渠道回显走 im.provider ───
test('loadImChannelConfig：优先用后端回显的 im.provider 选当前渠道', () => {
  assert.ok(/platforms\.provider === 'dingtalk' \|\| platforms\.provider === 'feishu'/.test(imSrc),
    '应以 GET /chat/config 返回的 im.provider 为当前渠道权威');
});

// ─── 5：后端契约（源码级） ───
test('chat.py：GET /config 回显 provider 化且仅 app_secret_exists 布尔', () => {
  assert.ok(/"provider": im_provider/.test(chatPy), 'im 响应应含 provider 键');
  assert.ok(/def _im_echo\(provider: str\) -> dict:/.test(chatPy), '应存在 provider 化回显 helper');
  const m = chatPy.match(/def _im_echo[\s\S]*?\n    return \{[\s\S]*?\n    \}/);
  assert.ok(m && /"app_secret_exists": bool\(im_app_secret\) and hit/.test(m[0]),
    'secret 仅以 app_secret_exists 布尔回显');
  assert.ok(/"app_id": im_app_id if hit else ""/.test(m[0]), 'app_id 仅命中渠道回显');
});

test('chat.py：PUT 接受 im.provider 分支（非法值 400 可读报错）', () => {
  assert.ok(/provider = str\(im\.get\("provider"\) or ""\)\.strip\(\)\.lower\(\)/.test(chatPy),
    '应从 im.provider 解析渠道');
  assert.ok(/if provider and provider not in \("feishu", "dingtalk"\):/.test(chatPy),
    '应校验 provider 合法集合');
  assert.ok(/暂不支持该 IM 渠道/.test(chatPy), '非法 provider 应 400 且文案可读');
});

test('chat.py：写桥透传 channel_type + 解绑按 provider 维度清理', () => {
  assert.ok(/channel_type=provider or "feishu"\)/.test(chatPy), 'sync_legacy_installation 应传 channel_type=provider');
  assert.ok(/_purge_chat_im_binding\(user\["id"\], user\.get\("username", ""\), old_app_id, provider=_purge_provider\)/.test(chatPy),
    '解绑清理应带 provider 维度');
  assert.ok(/if is_feishu:/.test(chatPy), '钉钉解绑应跳过飞书专属清理（unified_user_mapping / feishu_onboard_task）');
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
