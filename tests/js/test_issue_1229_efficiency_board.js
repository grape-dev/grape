/*
 * Issue #1229 能效看板（flow efficiency）前端源断言测试
 *
 * 历史：本文件原先断言 src/api/static/js/pages/observability.js 的旧实现（协作者表格版）。
 * Phase 1 结构迁移 + vue-integration 全量 Vue 化后，该老页面已删除、路径整体搬到
 * src/api/frontend/legacy；2026-11-11 移植 2.0 看板（10 段 + 下钻）到 Vue 视图时，
 * 按 grape-vue-view-port §2「陈旧断言改成读 Vue 源码」重写为源断言。
 *
 * 运行：node tests/js/test_issue_1229_efficiency_board.js
 *
 * 验证点：
 *   T10a  inspect 分区子 Tab 注册 efficiency（能效看板入口存在）
 *   T10b  zh/en eff2 键集合一致；视图用到的每个 eff2 键两边都有
 *   T11a  10 段卡片 + 两个下钻入口（openEffDaily / openEffIssues）都在模板里
 *   T11b  三条端点都在位（board / daily / issues）且都带 repo 参数
 *   T11c  能效块不用 v-html（issue 标题走模板绑定，无拼接注入面）
 *   T11d  分页：页缓存 + 请求序号防过期响应；周切换重取、分组切换不重取
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = path.join(ROOT, 'src/api/frontend/app/src/views/ObservabilityView.vue');
const zh = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales/zh/observability.json'), 'utf8')
);
const en = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales/en/observability.json'), 'utf8')
);

const src = fs.readFileSync(VIEW, 'utf8');
const tplEnd = src.indexOf('<script setup>');
const tpl = src.slice(0, tplEnd);
const script = src.slice(tplEnd);
const effTplStart = tpl.indexOf("tab === 'efficiency'")
// 内容区（10 段卡片）在下钻弹窗之前结束，弹窗单独在模板尾部
const effTpl = tpl.slice(effTplStart, tpl.indexOf('<!-- events 子 Tab'))
const effModalStart = tpl.indexOf('能效看板下钻弹窗')
const effTplAll = effTpl + tpl.slice(effModalStart)
const effScriptStart = script.indexOf('能效看板（Issue #1229');
const effScript = script.slice(effScriptStart);

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  \u2713 ' + name);
  } catch (e) {
    failed++;
    console.log('  \u2717 ' + name + '\n    ' + e.message);
  }
}

// ---------- T10a: 子 Tab 注册 ----------
test('T10a inspect 分区子 Tab 注册 efficiency', () => {
  const m = script.match(/const subTabs = computed\(\(\) =>([\s\S]*?)\n\)/)
  assert.ok(m, '未找到 subTabs computed')
  assert.ok(m[1].includes("'efficiency'"), 'subTabs 缺 efficiency: ' + m[1])
  assert.ok(/tb === 'efficiency'\) loadEff\(\)/.test(script), 'switchTab 未接 loadEff()')
  assert.ok(
    /tab\.value === 'efficiency'\) loadEff\(true\)/.test(script),
    'refreshObs 自动刷新未接 loadEff(true)'
  )
})

// ---------- T10b: i18n 键 ----------
test('T10b zh/en eff2 键集合一致 + 视图用到的键都存在', () => {
  const zk = Object.keys(zh.eff2 || {}).sort()
  const ek = Object.keys(en.eff2 || {}).sort()
  assert.deepStrictEqual(zk, ek, 'zh/en eff2 键集合不一致')
  assert.ok(zk.length >= 77, 'eff2 键数量不足: ' + zk.length)
  assert.ok(zh.tab && zh.tab.efficiency, 'zh 缺 tab.efficiency')
  assert.ok(en.tab && en.tab.efficiency, 'en 缺 tab.efficiency')
  const used = new Set()
  const re = /t\('eff2\.([a-z0-9_]+)'/g
  let mm
  while ((mm = re.exec(effTpl + effScript))) used.add(mm[1])
  const missingZh = [...used].filter((k) => zh.eff2[k] === undefined)
  const missingEn = [...used].filter((k) => en.eff2[k] === undefined)
  assert.strictEqual(missingZh.length, 0, 'zh 缺键: ' + missingZh.join(','))
  assert.strictEqual(missingEn.length, 0, 'en 缺键: ' + missingEn.join(','))
  assert.ok(used.size >= 50, '视图只用到 ' + used.size + ' 个 eff2 键，疑似漏段')
})

// ---------- T11a: 10 段 + 下钻入口 ----------
test('T11a 10 段卡片 + 两个下钻入口', () => {
  const segs = [
    'kpi_net',
    'kpi_total',
    'kpi_wip',
    'kpi_done_week',
    'kpi_overdue',
    'kpi_throughput',
    'kpi_cycle',
    'kpi_aging',
    'kpi_wip_dist',
    'kpi_delivery',
  ]
  const missing = segs.filter((k) => !effTpl.includes("t('eff2." + k + "')"))
  assert.strictEqual(missing.length, 0, '缺段: ' + missing.join(','))
  assert.ok(/openEffDaily\(b\.week\)/.test(effTpl), '火花柱未接下钻')
  assert.ok(/openEffIssues\('aging'/.test(effTpl), '龄期桶未接 issue 下钻')
  assert.ok(/openEffIssues\('cycle'/.test(effTpl), '周期桶未接 issue 下钻')
  assert.ok(/setChartEl\('effTp'/.test(effTpl), '周吞吐图容器缺失')
})

// ---------- T11b: 端点 ----------
test('T11b 三条端点 + repo 参数', () => {
  for (const p of ['/dashboard/efficiency/board', '/dashboard/efficiency/daily', '/dashboard/efficiency/issues']) {
    assert.ok(effScript.includes(p), '缺端点 ' + p)
  }
  assert.ok(
    /function effRepoParam\(\)[\s\S]*?repo=' \+ encodeURIComponent\(r\.fullName\)/.test(effScript),
    'effRepoParam 未按 fullName 组 repo 参数'
  )
  assert.ok(/&weeks=' \+[\s\S]{0,80}effState\.weeks/.test(effScript), 'board 未带 weeks')
  assert.ok(/threshold_days=/.test(effScript), 'issues 未带 threshold_days')
  assert.ok(/effState\.thresholdDays = \(d && d\.aging/.test(effScript), '未从 board 回写 thresholdDays')
})

// ---------- T11c: 无 v-html ----------
test('T11c 能效块不使用 v-html（无拼接注入面）', () => {
  assert.ok(effTplAll.indexOf('v-html') < 0, '能效块出现 v-html')
  assert.ok(/class="eff-tdtl" :title="x\.title"/.test(effTplAll), 'issue 标题应走属性/插值绑定')
})

// ---------- T11d: 分页与刷新语义 ----------
test('T11d 分页缓存 + 过期响应丢弃 + 分组不重取', () => {
  assert.ok(/const effIssCache = new Map\(\)/.test(effScript), '缺页缓存')
  assert.ok(/effIssCache\.get\(key\)/.test(effScript), '未先查缓存')
  assert.ok(/reqId = \+\+effIss\.reqId/.test(effScript), '缺请求序号')
  assert.ok(/if \(reqId !== effIss\.reqId\) return/.test(effScript), '未丢弃过期响应')
  assert.ok(/@change="loadEff\(\)"/.test(effTpl), '周数切换未重取')
  assert.ok(/@click="effState\.group = g\[0\]"/.test(effTpl), '分组切换应走客户端')
  assert.ok(!/group_by=' \+[\s\S]{0,40}group = g\[/.test(effScript), '分组切换不应发请求')
})

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);