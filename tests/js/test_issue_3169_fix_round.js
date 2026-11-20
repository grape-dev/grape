/*
 * Issue #3169 — 复审修复轮契约（QA 第二轮报告中可静态验证项）
 *
 * Seam（用户可见行为）：
 *   1. i18n 契约：不存在对象被当文案渲染、{{msg}}/{count} 占位符全部插值、
 *      英文语言包不缺扁平键、Chat 插件键进入语言包。
 *   2. Runtime 插件面板：agent/installed 带 repo_key；desired 取详情接口下发字段；
 *      刷新加载态不再早退卡 ⟳。
 *   3. 状态：分页单一 total、上传表单统一重置。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const MARKET = read('app/src/views/PluginMarketView.vue');
const MARKET_TPL = MARKET.slice(0, MARKET.indexOf('<script'));
const CHAT = read('app/src/views/ChatView.vue');
const RUNTIMES = read('app/src/views/RuntimesView.vue');
const zhPlugins = JSON.parse(read('shell/locales/zh/plugins.json'));
const enPlugins = JSON.parse(read('shell/locales/en/plugins.json'));
const zhChat = JSON.parse(read('shell/locales/zh/chat.json'));
const enChat = JSON.parse(read('shell/locales/en/chat.json'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}
function flatten(obj, prefix, out) {
  out = out || {};
  Object.keys(obj || {}).forEach(function (k) {
    const key = prefix ? prefix + '.' + k : k;
    if (obj[k] && typeof obj[k] === 'object' && !Array.isArray(obj[k])) flatten(obj[k], key, out);
    else out[key] = obj[k];
  });
  return out;
}

check('i18n：上传提交键避开 upload 命名空间撞名（不渲染对象）', function () {
  assert.strictEqual(typeof zhPlugins.upload, 'object', 'upload 应为命名空间对象');
  assert.ok(zhPlugins.upload.submit, '应提供 upload.submit');
  assert.ok(enPlugins.upload && enPlugins.upload.submit, '英文应提供 upload.submit');
  assert.ok(/tp\('upload\.submit'\)/.test(MARKET_TPL), '按钮应使用 tp(upload.submit)');
  assert.ok(!/tp\('upload'\)/.test(MARKET), '不得再使用 tp(upload) 命中命名空间对象');
});

check('i18n：{{msg}} 占位符全部走插值（不再手工拼 e.message）', function () {
  ['install_failed', 'uninstall_failed', 'rollback_failed'].forEach(function (k) {
    assert.ok(/\{\{msg\}\}/.test(zhPlugins[k]), k + ' 中文包应含 {{msg}}');
    assert.ok(/\{\{msg\}\}/.test(enPlugins[k]), k + ' 英文包应含 {{msg}}');
  });
  assert.ok(/\{\{msg\}\}/.test(zhPlugins.detail.load_failed), 'detail.load_failed 应含 {{msg}}');
  assert.ok(/tp\('install_failed', \{ msg:/.test(MARKET), '安装失败应传 msg 插值');
  assert.ok(/tp\('uninstall_failed', \{ msg:/.test(MARKET), '卸载失败应传 msg 插值');
  assert.ok(/tp\('rollback_failed', \{ msg:/.test(MARKET), '回滚失败应传 msg 插值');
  assert.ok(/td\('load_failed', \{ msg:/.test(MARKET), '详情加载失败应传 msg 插值');
});

check('i18n：l0_errors_title 的 {count} 走插值（不残留花括号）', function () {
  assert.ok(/\{\{count\}\}/.test(zhPlugins.upload.l0_errors_title), '中文包应含 {{count}}');
  assert.ok(/\{\{count\}\}/.test(enPlugins.upload.l0_errors_title), '英文包应含 {{count}}');
  assert.ok(
    /tp\('upload\.l0_errors_title', \{ count: uploadErrors\.length \}\)/.test(MARKET),
    '应以 count 参数插值'
  );
  assert.ok(!/\.replace\('\{count\}'/.test(MARKET), '不得再用 .replace({count})');
});

check('i18n：两包补齐 5 个扁平键 + 详情表头键', function () {
  const zh = flatten(zhPlugins);
  const en = flatten(enPlugins);
  ['cancel', 'certified_short', 'installing_short', 'detail.action', 'detail.status'].forEach(
    function (k) {
      assert.ok(zh[k], '中文包缺 ' + k);
      assert.ok(en[k], '英文包缺 ' + k);
    }
  );
});

check('i18n：卸载确认不再遮蔽「捆绑技能一并移除」警示', function () {
  assert.ok(/捆绑技能/.test(zhPlugins.uninstall_confirm), '中文卸载确认应含捆绑技能警示');
  assert.ok(/Bundled/i.test(enPlugins.uninstall_confirm), '英文卸载确认应含捆绑技能警示');
});

check('i18n：Chat 插件键进语言包（英文不再回退中文）', function () {
  ['plugin_label', 'plugin_config_tip', 'plugin_multi_hint'].forEach(function (k) {
    assert.ok(zhChat[k], 'zh chat.json 缺 ' + k);
    assert.ok(enChat[k], 'en chat.json 缺 ' + k);
  });
  assert.ok(zhChat.plugin && zhChat.plugin.no_installed, 'zh chat.json 缺 plugin.no_installed');
  assert.ok(enChat.plugin && enChat.plugin.no_installed, 'en chat.json 缺 plugin.no_installed');
  assert.ok(zhChat.plugin && zhChat.plugin.clear, 'zh chat.json 缺 plugin.clear');
  assert.ok(enChat.plugin && enChat.plugin.clear, 'en chat.json 缺 plugin.clear');
});

check('异味：上传失败兜底不再误用「上传中」文案', function () {
  assert.ok(zhPlugins.upload.toast_upload_failed, '应提供 upload.toast_upload_failed');
  assert.ok(enPlugins.upload.toast_upload_failed, '英文应提供 upload.toast_upload_failed');
  assert.ok(
    !/\(e && e\.message\) \|\| tp\('upload\.uploading'\)/.test(MARKET),
    '上传失败兜底不得使用「上传中」'
  );
  assert.ok(/tp\('upload\.toast_upload_failed'\)/.test(MARKET), '应使用上传失败文案');
});

check('Runtime P1-2：agent/installed 带 repo_key（与壳层 currentRepo 同源）', function () {
  const i = RUNTIMES.indexOf('function loadRtPlugins(');
  assert.ok(i >= 0, '应存在 loadRtPlugins');
  const seg = RUNTIMES.slice(i, i + 2600);
  assert.ok(/\/plugins\/agent\/installed/.test(seg), '应调用 /plugins/agent/installed');
  assert.ok(/repo_key=/.test(seg), '请求应携带 repo_key');
  assert.ok(/repoKeyValue\(\)|repoKeyOf\(\)/.test(RUNTIMES), '应复用当前仓库键助手');
});

check('Runtime P2：desired 取详情接口下发字段（不再前端自算 agent-defaults）', function () {
  assert.ok(/desired_plugins/.test(RUNTIMES), '应消费 desired_plugins 字段');
  assert.ok(!/'\/plugins\/agent-defaults'/.test(RUNTIMES), '不应再前端自算 agent-defaults');
});

check('Runtime 状态：刷新加载态不再早退卡 ⟳', function () {
  const i = RUNTIMES.indexOf('function loadRtPlugins(');
  const seg = RUNTIMES.slice(i, i + 2200);
  const loadingOff = seg.indexOf('rtvPluginLoading.value = false');
  const guard = seg.indexOf("if (!engineDetail.value || engineDetail.value.id !== rid) return");
  assert.ok(loadingOff >= 0, '应复位 loading');
  assert.ok(guard >= 0, '保留切换详情的竞态守卫');
  assert.ok(loadingOff < guard, 'loading 复位应在早退之前');
});

check('状态：分页使用单一 total（total/pageTotal 不再双写）', function () {
  assert.ok(!/const pageTotal = ref\(/.test(MARKET), '不得保留 pageTotal');
  assert.ok(!/pageTotal\.value/.test(MARKET), '不得再写 pageTotal');
  assert.ok(/totalPages = computed\(\(\) => Math\.max\(1, Math\.ceil\(total\.value \/ pageSize\.value\)\)\)/.test(MARKET), 'totalPages 应基于 total');
});

check('状态：上传表单统一重置（非 6 字段手工逐项）', function () {
  assert.ok(/function resetUploadForm\(|function defaultUploadForm\(/.test(MARKET), '应抽出表单重置助手');
  const i = MARKET.indexOf('function openUpload(');
  const seg = MARKET.slice(i, i + 700);
  assert.ok(/uploadForm\.value = /.test(seg), 'openUpload 应整体赋值表单');
  assert.ok(!/uploadForm\.value\.sourceRepo = ''/.test(seg), '不得逐项手工重置');
});

console.log('\n#3169 复审修复轮契约全部通过 (' + passed + '/12)');