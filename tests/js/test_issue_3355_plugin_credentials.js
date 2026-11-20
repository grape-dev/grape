/*
 * Issue #3355 — 切片2：详情抽屉凭据段
 *
 * Seam（用户可见行为）：
 *   打开详情抽屉即可就地配置插件凭据——已配置字段绿✓+掩码占位回显；
 *   manifest 声明凭据保存走 POST /api/plugins/{name}/credentials（仅提交
 *   被改动且非空的键，掩码占位不回写）；➕ 自由增行保存走 PUT
 *   /api/settings/credentials（provider=plugin:{name} 冒号格式）；👁 仅切换
 *   输入框 type（明文回显不走任何端点）。
 *
 * 复审返工（QA 202045541 P1-2/P1-3/P2-5）：
 *   自由增行保存后仍同屏可见（「已配置但未声明」行，可改可删），并明示
 *   「本次会话生效/待后端迁移」；切换插件时迟到响应不得写入新插件表单。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const MARKET = read('app/src/views/PluginMarketView.vue');
const MARKET_TPL = MARKET.slice(0, MARKET.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

// 精确切片：从函数起点到下一个顶层 `function ` 之前，避免定长窗口跨进下一函数
function fnSeg(name, len) {
  const i = MARKET.indexOf('function ' + name + '(');
  assert.ok(i >= 0, '应存在 ' + name);
  const rest = MARKET.slice(i);
  const end = rest.indexOf('\nfunction ');
  const seg = end > 0 ? rest.slice(0, end) : rest;
  return len ? seg.slice(0, len) : seg;
}

check('抽屉打开即回显：GET /api/plugins/{name}/credentials', function () {
  const seg = fnSeg('loadCreds');
  assert.ok(
    /G\.API\.get\('\/plugins\/' \+ encodeURIComponent\(name\) \+ '\/credentials'\)/.test(seg),
    '应 GET /plugins/{name}/credentials'
  );
  assert.ok(/configured_keys/.test(seg), '应消费 configured_keys 存在性');
});

check('已配置字段绿✓ + 掩码占位（不回显明文）', function () {
  assert.ok(/pm__credok/.test(MARKET_TPL), '应渲染已配置 ✓ 标记');
  assert.ok(/isCredConfigured/.test(MARKET_TPL), '已配置标记应由存在性驱动');
  assert.ok(/cred_placeholder_configured/.test(MARKET_TPL), '已配置项应显示掩码占位');
  assert.ok(/isCredConfigured\(f\.key\)/.test(MARKET), '声明行按 key 判定已配置');
});

check('manifest 凭据声明双格式解析（数组 {key,name} / 映射表 {KEY:{required,inject}}）', function () {
  const seg = fnSeg('parseManifestCreds');
  assert.ok(/Array\.isArray\(raw\)/.test(seg), '应兼容数组形态');
  assert.ok(/f\.key \|\| f\.name/.test(seg), '数组条目取 key 或 name');
  assert.ok(/Object\.keys\(raw\)/.test(seg), '应兼容映射表形态（市场 manifest.json）');
  assert.ok(/required/.test(seg), '应透传 required 标注');
});

check('声明键保存走 POST /api/plugins/{name}/credentials，仅提交 dirty 非空值', function () {
  const seg = fnSeg('saveCreds');
  assert.ok(
    /G\.API\.post\('\/plugins\/' \+ encodeURIComponent\([a-zA-Z]+\.name\) \+ '\/credentials'/.test(seg),
    'manifest 声明凭据应 POST 插件端点'
  );
  assert.ok(/config: declared/.test(seg), 'POST body 应为 {config: 改动键值}');
  const dirtyGuard = /v !== '' && [a-zA-Z]+\.dirty/.test(seg);
  assert.ok(dirtyGuard, '仅提交被改动(dirty)且非空的值，掩码占位不回写');
});

check('➕ 自由增行保存走 PUT /api/settings/credentials（provider=plugin:{name}）', function () {
  const seg = fnSeg('saveCreds');
  assert.ok(/G\.API\.put\('\/settings\/credentials'/.test(seg), '自由增行应 PUT settings 泛化端点');
  assert.ok(/provider: 'plugin:' \+/.test(seg), 'provider 应为 plugin:{name} 冒号格式');
  assert.ok(/function addCredRow\(/.test(MARKET), '应有自由添加行入口');
  assert.ok(/function removeCredRow\(/.test(MARKET), '自由行应可移除');
});

check('👁 仅切换输入框 type（明文切换不触发任何 API）', function () {
  const seg = fnSeg('toggleCredEye');
  assert.ok(!/G\.API\./.test(seg), '👁 切换不得调用任何端点');
  assert.ok(/\.show = !.*\.show/.test(seg), '仅翻转明文态标记');
  assert.ok(/:type="f\.show \? 'text' : 'password'"/.test(MARKET_TPL), '输入框 type 由明文态驱动');
});

check('保存成功后重拉回显 + 清空明文输入（新值覆盖、留空保持）', function () {
  const seg = fnSeg('saveCreds');
  assert.ok(/loadCreds\(/.test(seg), '保存成功应重拉凭据存在性');
  assert.ok(/\.dirty = false/.test(seg), '保存后应复位 dirty 标记');
});

check('关闭/切换抽屉复位凭据表单（脏态不泄漏到下一个插件）', function () {
  assert.ok(/function resetCredState\(/.test(MARKET), '应有凭据表单复位助手');
  const open = fnSeg('openDetail');
  const close = fnSeg('closeDetail');
  assert.ok(/resetCredState\(\)/.test(open) || /resetCredState\(\)/.test(fnSeg('loadDetail')), '打开时应按插件重建凭据表单');
  assert.ok(/resetCredState\(\)/.test(close), '关闭时应复位凭据表单');
});

// ── 复审返工：自由增行保存后同屏可见（P1-2） ──
check('自由增行保存后同屏可见：未声明的已配置行可改可删', function () {
  assert.ok(
    /credState\.savedFreeRows/.test(MARKET_TPL),
    '模板应渲染「已配置但未声明」自由行（保存后不消失）'
  );
  assert.ok(
    /function syncSavedFreeRows\(/.test(MARKET),
    '应有 configured_keys ∪ freeConfiguredKeys 的未声明行同步助手'
  );
  const sync = fnSeg('syncSavedFreeRows');
  assert.ok(/configuredKeys/.test(sync) && /freeConfiguredKeys/.test(sync), '应合并服务端存在性与客户端会话标记');
  assert.ok(/declared/.test(sync), '声明键不得重复渲染为自由行');
  assert.ok(/syncSavedFreeRows\(\)/.test(fnSeg('saveCreds')), '保存成功后应同步出可继续编辑的自由行');
  assert.ok(/function removeSavedCredRow\(/.test(MARKET), '已配置自由行应可删除');
  assert.ok(/freeConfiguredKeys/.test(fnSeg('removeSavedCredRow')), '删除应移除会话标记');
});

check('自由行真值明示：本次会话生效/待后端迁移（不再假称已持久化）', function () {
  assert.ok(/cred_free_pending/.test(MARKET_TPL), '自由行入口应明示待后端迁移');
  assert.ok(/cred_saved_session/.test(fnSeg('saveCreds')), '自由行保存提示应明示会话级生效');
});

// ── 复审返工：切插件竞态（P2-5） ──
check('切换插件竞态守卫：迟到响应不写入新插件表单（loadCreds/loadParams）', function () {
  assert.ok(/sameDetail\(name\)|detail\.value\.name !== name|_currentView/.test(fnSeg('loadCreds')), 'loadCreds 应按插件名或视图哨兵丢弃迟到响应');
  assert.ok(/sameDetail\(name\)|detail\.value\.name !== name|_currentView/.test(fnSeg('loadParams')), 'loadParams 应按插件名或视图哨兵丢弃迟到响应');
});

console.log('\n#3355 凭据段契约通过 (' + passed + '/11)');
