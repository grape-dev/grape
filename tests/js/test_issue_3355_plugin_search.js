/*
 * Issue #3355 — 切片4：搜索接 /api/plugins/search（P3-2）+ 坏 zip 错误容错降级
 *
 * Seam（用户可见行为）：
 *   搜索输入防抖 300ms 走 GET /api/plugins/search?q=（Network 可验）；
 *   空 q 回落列表接口；搜索命中/异常回落列表 ?q=；结果行展示 match_reason；
 *   点击搜索结果可打开详情抽屉。坏 zip 仅返回 {code,message}（缺五项结构化
 *   字段）时前端不白屏，逐条降级展示 message。
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
  // 兼容 `function foo(` 与生命周期钩子 `foo(function (` 两种写法
  let i = MARKET.indexOf('function ' + name + '(');
  if (i < 0) i = MARKET.indexOf(name + '(function');
  assert.ok(i >= 0, '应存在 ' + name);
  const rest = MARKET.slice(i);
  const end = rest.indexOf('\nfunction ');
  const seg = end > 0 ? rest.slice(0, end) : rest;
  return len ? seg.slice(0, len) : seg;
}

check('搜索防抖 300ms 走 GET /api/plugins/search?q=', function () {
  const seg = fnSeg('onSearchInput');
  assert.ok(/setTimeout\(/.test(seg), '输入应防抖');
  assert.ok(/300\)/.test(seg), '防抖窗口应为 300ms');
  assert.ok(/clearTimeout\(/.test(seg), '连续输入应重置计时器');
  assert.ok(/@input="onSearchInput"/.test(MARKET_TPL), '搜索框应接防抖输入');
});

check('搜索契约：非空 q 命中 /plugins/search（encodeURIComponent 编码）', function () {
  const seg = fnSeg('doSearch');
  assert.ok(/\/plugins\/search\?q=' \+ encodeURIComponent\(/.test(seg), '应调 /plugins/search?q=');
  assert.ok(/match_reason/.test(seg), '应透传 match_reason');
  assert.ok(/plugin_id \|\| p\.id/.test(seg), '搜索行应以 plugin_id 归一（详情按 by-id 打开）');
});

check('空 q / 空结果 / 接口异常 → 回落列表接口', function () {
  const seg = fnSeg('doSearch');
  assert.ok(/if \(!q\.value\.trim\(\)\)/.test(seg) || /if \(!q\.value\)/.test(seg), '空 q 应回落列表态');
  assert.ok(/!rows\.length/.test(seg), '空结果应回落列表 ?q=');
  assert.ok(/\.catch\(/.test(seg), '接口异常应回落列表 ?q=');
  assert.ok(/searchMode\.value = false/.test(seg), '回落应退出搜索态');
});

check('搜索结果行展示 match_reason（命中原因可见）', function () {
  assert.ok(/match_reason/.test(MARKET_TPL), '模板应展示 match_reason');
  assert.ok(/searchMode/.test(MARKET_TPL), '应有搜索结果分支');
});

check('重置筛选/切换页签退出搜索态（不残留搜索结果）', function () {
  const rf = fnSeg('resetFilters');
  assert.ok(/searchMode\.value = false/.test(rf), '重置应退出搜索态');
  const st = fnSeg('switchTab');
  assert.ok(/searchMode\.value = false/.test(st), '切换页签应退出搜索态');
});

check('坏 zip 容错：错误项缺结构化字段时降级展示 message（不白屏）', function () {
  assert.ok(
    /err\.reason \|\| err\.message/.test(MARKET_TPL),
    'L0 错误行应降级展示 message（后端仅 {code,message} 时）'
  );
});

// ── 复审返工（QA 202045541 P2-4/P3-7/P3-8） ──
check('坏 zip 降级路径可达：{code,message} 收敛为单条 errors 进面板（不只 toast）', function () {
  assert.ok(/function pushUploadError\(/.test(MARKET), '应集中收敛上传错误（含无 errors 的 {code,message}）');
  const seg = fnSeg('pushUploadError');
  assert.ok(/errors/.test(seg), '已有结构化 errors 应原样透传');
  assert.ok(/detail/.test(seg) && /message/.test(seg), '应兼容 e.detail.{code,message} / e.message');
  assert.ok(/uploadErrors\.value/.test(seg), '应写入 pm__l0err 面板数据源');
  assert.ok(/pushUploadError\(/.test(fnSeg('doUpload')), 'L0 门禁 catch 应走统一收敛');
  assert.ok(/pushUploadError\(/.test(fnSeg('doUploadAfterGate')), '上架 catch 应走统一收敛');
});

check('搜索命中他人插件 403：给出明确无权限文案（不静默加载失败）', function () {
  const seg = fnSeg('loadDetail');
  assert.ok(/403/.test(seg), '详情加载应识别 403');
  assert.ok(/detail_forbidden/.test(seg), '403 应提示无权限（而非通用加载失败）');
});

check('搜索防抖计时器卸载清理（无泄漏）', function () {
  assert.ok(/onBeforeUnmount/.test(MARKET), '应引入组件卸载钩子');
  const seg = fnSeg('onBeforeUnmount');
  assert.ok(/searchTimer/.test(seg), '卸载时应清理 searchTimer');
  assert.ok(/clearTimeout\(/.test(seg), '卸载时应 clearTimeout');
});

console.log('\n#3355 搜索接线契约通过 (' + passed + '/9)');
