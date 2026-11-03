/*
 * Issue #614: 会话超时自动登出后凭据弹窗明文秘钥未清理 — 前端测试
 *
 * 运行：node tests/js/test_issue_614_session_logout_cred_clear.js
 *
 * 验证点：
 *   1. app.showLogin()（所有登出/会话失效路径的最终汇聚点）触发统一清理钩子
 *      _clearSensitiveModals()：
 *      - 关闭统一弹层 grapeModalOverlay（showModal/showPrompt）与确认弹窗 grapeConfirmOverlay
 *      - 清空 runtime 设备凭据面板 addDeviceCredArea / addDeviceDevArea / aiDevspacePanel
 *        的明文 token DOM 并隐藏
 *      - 移除敏感弹窗 sreAddDeviceOverlay / addDeviceOverlay / credModal_*（devspace 环境凭据弹窗）
 *      - 兜底移除 position:fixed 且 z-index∈[1000,9000) 的未知遮罩层
 *   2. 非清理目标不受影响：toast(9999/10000)、warn(9999)、帮助抽屉(9990)、
 *      顶栏菜单(10001)、非 fixed 元素
 *   3. 清理幂等（重复调用不抛错、状态保持）
 *   4. app 导出对象包含 _clearSensitiveModals（供页面脚本/测试调用）
 *
 * 策略：从 app.js 源码提取 closeModal/closeConfirm/_clearSensitiveModals/showLogin
 *      真实函数体，在 mock DOM 沙箱执行（先例：test_issue_393_admin_confirm_entry.js）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const appJsPath = path.resolve(__dirname, '../../src/api/static/js/app.js');
const src = fs.readFileSync(appJsPath, 'utf8');

// ── 花括号配平提取函数完整源码 ──
function extractFn(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) return null;
  const braceOpen = src.indexOf('{', start);
  let depth = 0, i = braceOpen;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  if (depth !== 0) return null;
  return src.slice(start, i + 1);
}

const closeModalSrc = extractFn('closeModal');
const closeConfirmSrc = extractFn('closeConfirm');
const clearSrc = extractFn('_clearSensitiveModals');
const showLoginSrc = extractFn('showLogin');

// 前置存在性：钩子不存在 = 红（Issue #614 未实现）
assert.ok(clearSrc, 'app.js 应定义 _clearSensitiveModals()（Issue #614 统一清理钩子）');
assert.ok(closeModalSrc && closeConfirmSrc && showLoginSrc, 'app.js 应含 closeModal/closeConfirm/showLogin（既有函数）');

// showLogin 应在函数体内调用 _clearSensitiveModals()（所有登出路径汇聚点统一清理）
assert.ok(
  /\b_clearSensitiveModals\s*\(\s*\)/.test(showLoginSrc),
  'showLogin() 函数体应调用 _clearSensitiveModals()（Issue #614：会话失效路径统一清理）'
);

// app 导出对象应包含 _clearSensitiveModals
assert.ok(
  /return\s*\{[^}]*\b_clearSensitiveModals\b/.test(src) || /\b_clearSensitiveModals\s*,/.test(src),
  'app 导出对象应包含 _clearSensitiveModals'
);

// ── mock DOM ──
function mkEl(id, opts) {
  opts = opts || {};
  return {
    id: id,
    innerHTML: opts.innerHTML || '',
    style: {
      display: opts.display || (opts.fixed ? 'flex' : 'none'),
      position: opts.fixed ? 'fixed' : (opts.position || ''),
      zIndex: opts.z != null ? String(opts.z) : '',
    },
    removed: false,
    remove() { this.removed = true; },
  };
}

const PLAINTEXT_TOKEN = 'rt_secret_TOKEN_614_aaa.bbb.ccc';

function buildSandbox() {
  const els = {
    grapeModalOverlay: mkEl('grapeModalOverlay', { fixed: true, z: 1001, innerHTML: '<code>' + PLAINTEXT_TOKEN + '</code>' }),
    grapeConfirmOverlay: mkEl('grapeConfirmOverlay', { fixed: true, z: 1001, innerHTML: '<div>confirm</div>' }),
    credModal_env42: mkEl('credModal_env42', { fixed: true, z: 1001, innerHTML: '<code>curl ... --token ' + PLAINTEXT_TOKEN + '</code>' }),
    sreAddDeviceOverlay: mkEl('sreAddDeviceOverlay', { fixed: true, z: 1001, innerHTML: '<div>' + PLAINTEXT_TOKEN + '</div>' }),
    unknownCredOverlay: mkEl('unknownCredOverlay_614', { fixed: true, z: 1001, innerHTML: '<code>' + PLAINTEXT_TOKEN + '</code>' }),
    addDeviceCredArea: mkEl('addDeviceCredArea', { display: 'block', innerHTML: '<code>RUNTIME_TOKEN=' + PLAINTEXT_TOKEN + '</code>' }),
    addDeviceDevArea: mkEl('addDeviceDevArea', { display: 'block', innerHTML: '<code>' + PLAINTEXT_TOKEN + '</code>' }),
    aiDevspacePanel: mkEl('aiDevspacePanel', { display: 'block', innerHTML: '<code>' + PLAINTEXT_TOKEN + '</code>' }),
    // 非清理目标
    toastHost: mkEl('toastHost', { fixed: true, z: 10000 }),
    warnBanner: mkEl('warnBanner', { fixed: true, z: 9999 }),
    helpOverlay: mkEl('helpOverlay', { fixed: true, z: 9990 }),
    tbOrgMenu: mkEl('tbOrgMenu', { fixed: true, z: 10001 }),
    sceneDropdown: mkEl('sceneDropdown', { position: 'absolute', z: 100 }),
    mainView: mkEl('mainView', { display: 'block' }),
  };
  const list = Object.keys(els).map(k => els[k]);
  const documentElement = { removeAttribute() {} };
  const document = {
    documentElement: documentElement,
    body: { style: { overflow: 'hidden' } },
    getElementById(id) { const e = list.find(x => x.id === id); return (e && !e.removed) ? e : null; },
    querySelectorAll(sel) {
      if (sel === '[id^="credModal_"]') return list.filter(x => x.id.indexOf('credModal_') === 0 && !x.removed);
      if (sel === 'body > div') return list.filter(x => !x.removed);
      throw new Error('mock DOM 不支持选择器: ' + sel);
    },
  };
  const $ = (id) => document.getElementById(id) || { classList: { add() {}, remove() {} }, style: {} };
  const window = { _currentView: 'runtimes' };
  const noop = () => {};

  // closeModal 引用模块级 _modalEl（当前 showModal 打开的统一弹层）；
  // showLogin 引用模块级 _refreshTimer（UC16e 停轮询）与 clearInterval
  const fns = new Function(
    'document', '_modalEl', '$', 'renderOAuthButtons', 'window', 'setTimeout', 'clearInterval', '_refreshTimer',
    closeModalSrc + '\n' + closeConfirmSrc + '\n' + clearSrc + '\n' + showLoginSrc + '\n' +
    'return { closeModal: closeModal, closeConfirm: closeConfirm, _clearSensitiveModals: _clearSensitiveModals, showLogin: showLogin };'
  )(document, els.grapeModalOverlay, $, noop, window, noop, noop, null);

  return { els: els, fns: fns, document: document };
}

console.log('Issue #614: 会话超时登出后凭据弹窗明文秘钥清理测试\n');

// ── 1/4 模拟会话超时自动登出：refresh 失败 → showLogin ──
{
  const sb = buildSandbox();
  const { els, fns } = sb;

  // 前置：敏感面确在展示（复现 Issue 症状）
  assert.ok(els.addDeviceCredArea.innerHTML.indexOf(PLAINTEXT_TOKEN) >= 0, '前置：凭据面板展示明文 token');
  assert.ok(els.credModal_env42 && !els.credModal_env42.removed, '前置：devspace 凭据弹窗打开');

  // 触发登出路径汇聚点（不刷新页面）
  fns.showLogin();

  // 统一弹层/确认弹窗已关闭
  assert.ok(els.grapeModalOverlay.removed, 'grapeModalOverlay 应被关闭');
  assert.ok(els.grapeConfirmOverlay.removed, 'grapeConfirmOverlay 应被关闭');
  // 敏感弹窗已移除
  assert.ok(els.credModal_env42.removed, 'credModal_env42（devspace 环境凭据弹窗）应被移除');
  assert.ok(els.sreAddDeviceOverlay.removed, 'sreAddDeviceOverlay（SRE 添加设备弹窗）应被移除');
  assert.ok(els.unknownCredOverlay.removed, '未知 z=1001 fixed 遮罩应被兜底移除');
  // 明文秘钥已清空
  assert.strictEqual(els.addDeviceCredArea.innerHTML, '', 'addDeviceCredArea innerHTML 应被清空（明文 token 清除）');
  assert.strictEqual(els.addDeviceDevArea.innerHTML, '', 'addDeviceDevArea innerHTML 应被清空');
  assert.strictEqual(els.aiDevspacePanel.innerHTML, '', 'aiDevspacePanel innerHTML 应被清空');
  assert.strictEqual(els.addDeviceCredArea.style.display, 'none', 'addDeviceCredArea 应被隐藏');
  // 全页面明文 token 不再残留
  const residue = Object.keys(els).filter(k => !els[k].removed && String(els[k].innerHTML).indexOf(PLAINTEXT_TOKEN) >= 0);
  assert.strictEqual(residue.length, 0, '清理后页面不应残留明文 token（残留: ' + residue.join(',') + '）');

  // 非清理目标不受影响
  assert.ok(!els.toastHost.removed, 'toast(10000) 不应被清理');
  assert.ok(!els.warnBanner.removed, 'warn(9999) 不应被清理');
  assert.ok(!els.helpOverlay.removed, '帮助抽屉(9990) 不应被清理');
  assert.ok(!els.tbOrgMenu.removed, '顶栏菜单(10001) 不应被清理');
  assert.ok(!els.sceneDropdown.removed, '低 z-index absolute 元素不应被清理');
  assert.ok(!els.mainView.removed, 'mainView 不应被清理');

  console.log('  PASS: 会话超时登出（showLogin）后凭据弹窗关闭/明文秘钥清空');
}

// ── 2/4 手动登出路径：logout → showLogin（同一汇聚点，行为一致）──
{
  const sb = buildSandbox();
  sb.fns.showLogin();
  assert.ok(sb.els.grapeModalOverlay.removed && sb.els.credModal_env42.removed);
  assert.strictEqual(sb.els.addDeviceCredArea.innerHTML, '');
  console.log('  PASS: 手动登出路径（logout → showLogin）同样清理');
}

// ── 3/4 幂等：重复调用不抛错、状态保持 ──
{
  const sb = buildSandbox();
  sb.fns.showLogin();
  assert.doesNotThrow(() => { sb.fns._clearSensitiveModals(); sb.fns.showLogin(); }, '重复清理不应抛错');
  assert.strictEqual(sb.els.addDeviceCredArea.innerHTML, '');
  console.log('  PASS: 清理幂等（二次调用无异常）');
}

// ── 4/4 空页面（未打开任何弹窗）调用安全 ──
{
  const fns = new Function(
    'document', '_modalEl', '$', 'renderOAuthButtons', 'window', 'setTimeout', 'clearInterval', '_refreshTimer',
    closeModalSrc + '\n' + closeConfirmSrc + '\n' + clearSrc + '\n' + showLoginSrc + '\n' +
    'return { showLogin: showLogin };'
  )(
    { documentElement: { removeAttribute() {} }, body: { style: {} }, getElementById: () => null, querySelectorAll: () => [] },
    null, () => ({ classList: { add() {}, remove() {} }, style: {} }), () => {}, { _currentView: '' }, () => {}, () => {}, null
  );
  assert.doesNotThrow(() => fns.showLogin(), '无弹窗时 showLogin 应安全');
  console.log('  PASS: 空页面调用安全');
}

console.log('\n全部通过\n');
