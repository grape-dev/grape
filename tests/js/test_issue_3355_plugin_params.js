/*
 * Issue #3355 — 切片3：详情抽屉参数段（A4 personal overlay）
 *
 * Seam（用户可见行为）：
 *   抽屉内可编辑插件运行参数的个人默认值——manifest.commands 双格式解析出
 *   参数表（string/number/boolean/enum 类型化控件）；GET
 *   /api/plugins/{name}/config 回填个人覆盖值并标注来源；PUT
 *   /api/plugins/{name}/config 仅提交被改动且非空的键（保存前 diff 原值，
 *   不全量写回覆盖默认值）；保存后重开抽屉值持久。
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

function fnSeg(name, len) {
  const i = MARKET.indexOf('function ' + name + '(');
  assert.ok(i >= 0, '应存在 ' + name);
  return MARKET.slice(i, i + (len || 1500));
}

check('参数表来自 manifest.commands 双格式解析（数组/映射表，params 简写字符串形态兼容）', function () {
  const nc = fnSeg('normalizeCommands', 900);
  assert.ok(/Array\.isArray\(arr\)/.test(nc), 'commands 应兼容数组形态');
  assert.ok(/Object\.keys\(arr\)/.test(nc), 'commands 应兼容映射表形态（早期 demo 包）');
  const np = fnSeg('normalizeParams', 1100);
  assert.ok(/typeof v === 'string'/.test(np), 'params 应兼容简写字符串形态（值即描述）');
  const pp = fnSeg('parseManifestParams', 1200);
  assert.ok(/\.commands/.test(pp), '应从 manifest.commands 解析');
  assert.ok(/cmd\.name|cn \+ '\.'/.test(pp), '参数键应为 命令.参数 形态');
  assert.ok(/type/.test(pp), '应透传参数类型');
});

check('类型化控件：boolean 复选 / enum 下拉 / number 数字框 / string 文本框', function () {
  assert.ok(/r\.type === 'boolean'/.test(MARKET_TPL), 'boolean 应渲染复选控件');
  assert.ok(/enumOpts/.test(MARKET_TPL), 'enum 应渲染下拉选项');
  assert.ok(/r\.type === 'number' \? 'number' : 'text'/.test(MARKET_TPL), 'number/string 应切换输入框 type');
});

check('回填：GET /api/plugins/{name}/config 个人覆盖值 + 来源标注', function () {
  const seg = fnSeg('loadParams', 1200);
  assert.ok(
    /G\.API\.get\('\/plugins\/' \+ encodeURIComponent\(name\) \+ '\/config'\)/.test(seg),
    '应 GET /plugins/{name}/config'
  );
  assert.ok(/hasOwnProperty/.test(seg) || /in cfg/.test(seg), '应按参数键回填个人覆盖值');
  assert.ok(/personal = true/.test(seg), '覆盖键应标注「个人覆盖」来源');
  assert.ok(/param_src_personal/.test(MARKET_TPL), '模板应展示来源标注');
});

check('保存：PUT /api/plugins/{name}/config，仅提交被改动且非空的键（diff 原值）', function () {
  const seg = fnSeg('saveParams');
  assert.ok(
    /G\.API\.put\('\/plugins\/' \+ encodeURIComponent\([a-zA-Z]+\.name\) \+ '\/config'/.test(seg),
    '应 PUT /plugins/{name}/config'
  );
  assert.ok(/config: config/.test(seg), 'body 应为 {config: 改动键值}');
  assert.ok(/r\.dirty/.test(seg), '仅收集被改动（dirty）的键');
  assert.ok(/v !== ''/.test(seg), '空值不提交（不覆盖写空）');
  assert.ok(/String\(r\.original\)/.test(seg), '保存前应 diff 原值（未改动键不写回）');
});

check('保存成功后本地固化（original 同步 + 来源升级个人覆盖 + dirty 复位）', function () {
  const seg = fnSeg('saveParams');
  assert.ok(/r\.original = config\[r\.pk\]/.test(seg), '保存后应固化原值（按提交键回写）');
  assert.ok(/dirty = false/.test(seg), '保存后应复位 dirty');
  assert.ok(/param_saved/.test(seg), '应提示保存成功');
});

check('关闭/切换抽屉复位参数表单（掩码/脏态不泄漏）', function () {
  assert.ok(/function resetParamState\(/.test(MARKET), '应有参数表单复位助手');
  const open = fnSeg('openDetail');
  const close = fnSeg('closeDetail');
  assert.ok(/resetParamState\(\)/.test(open), '打开抽屉应复位参数表单');
  assert.ok(/resetParamState\(\)/.test(close), '关闭抽屉应复位参数表单');
});

console.log('\n#3355 参数段契约通过 (' + passed + '/6)');
