/**
 * Issue #2133 前端兜底 + D102-K 引导数据源（2026-11-13，随 arch-weekly-11 同步后补齐）
 *
 * 背景：weekly-11 把这两块前端写进了已退役的 src/api/static/js/pages/*，Vue 线没有
 * 对应实现。本测试锁住后端已就绪的两条链路在前端被正确接上：
 *   T1. 引擎卡「spawn_failed 降级」徽标 + 「一键加回」按钮的显示条件与后端字段对齐
 *   T2. 一键加回调用 POST /settings/agent-runtimes/{id}/readd，并把 bindings / terminalized+released 计数喂给文案
 *   T3. 引导页候选来自平台卡目录（/settings/vcs/platforms），/auth/providers 只取 OAuth 配置态
 *   T4. 目录接口失败 → 空壳渲染（不再硬编码 gitcode/github 双平台）
 *   T5. 引导壳全屏（隐藏 sidebar/tb，mainView fixed inset 0）
 *   T6. 所需 i18n 键 zh/en 齐备
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

const RT = read('src/api/frontend/app/src/views/RuntimesView.vue');
const APP = read('src/api/frontend/shell/js/app.js');

test('T1 降级徽标 / 一键加回的显示条件', () => {
  assert.match(RT, /v-if="rt\.spawn_fail_degraded"[\s\S]{0,200}class="rtv-degraded"/, '缺降级徽标');
  assert.match(RT, /v-if="rt\.detached_from_scenes \|\| rt\.spawn_fail_degraded"/, '按钮显示条件不对');
  assert.match(RT, /@click\.stop="readdEngine\(rt\)"/, '按钮未阻止冒泡（会误开详情）');
  // 符与后端 GET /settings/agent-runtimes 的输出字段一致
  assert.match(read('src/domains/mgmt/routes/settings.py'),
    /rt\["spawn_fail_degraded"\] = bool\(rt\.get\("spawn_fail_degraded_at"\)\)/);
});

test('T2 一键加回：接口、载荷、计数映射', () => {
  assert.match(RT, /G\.API\.post\('\/settings\/agent-runtimes\/' \+ rt\.id \+ '\/readd' \+ repoQs\(\), \{\}\)/,
    '调用路径/载荷不对');
  assert.match(RT, /label\.readd_done/, '未用 readd_done 文案');
  assert.match(RT, /b: \(d\.bindings \|\| \[\]\)\.length/, '场景绑定计数不对');
  assert.match(RT, /\(d\.terminalized \|\| \[\]\)\.length \+ \(d\.released \|\| \[\]\)\.length/, '任务对账计数不对');
  assert.match(RT, /showConfirm\(t\('label\.readd_confirm'\)/, '缺确认弹层');
  assert.match(RT, /loadRuntimesData\(\)/, '成功后未刷新列表');
});

test('T3 引导候选来自平台卡目录', () => {
  assert.match(APP, /API\.get\('\/settings\/vcs\/platforms'\)/, '未拉平台卡目录');
  assert.match(APP, /results\[0\]\.platforms|results\[0\] && results\[0\]\.platforms/, '未消费 platforms');
  assert.match(APP, /_oauthCfg\[p\.provider\] = !!p\.configured/, '未按 provider 映射 OAuth 配置态');
  // 目录字段直通（pat_create_url 供后续绑定弹层用）
  assert.match(APP, /pat_create_url: p\.pat_create_url/, '未透传 pat_create_url');
});

test('T4 目录失败不硬编码双平台', () => {
  assert.ok(!/provider: 'gitcode', name: 'GitCode'/.test(APP), '仍硬编码 gitcode 兜底');
  assert.match(APP, /_renderOnboardingShell\(\[\], isZh, name, avatar\)/, '兜底应为空壳');
});

test('T5 引导壳全屏', () => {
  assert.match(APP, /getElementById\('sidebar'\)[\s\S]{0,40}style\.display = 'none'/, '未隐藏侧栏');
  assert.match(APP, /getElementById\('tb'\)[\s\S]{0,40}style\.display = 'none'/, '未隐藏顶栏');
  assert.match(APP, /_mv\.style\.inset = '0'/, '未铺满 mainView');
});

test('T6 i18n 键 zh/en 齐备', () => {
  const keys = ['readd', 'readd_hint', 'readd_confirm', 'readd_done', 'readd_failed',
    'spawn_fail_degraded', 'spawn_fail_degraded_hint'];
  for (const lang of ['zh', 'en']) {
    const d = JSON.parse(read(`src/api/frontend/shell/locales/${lang}/runtimes.json`));
    for (const k of keys) {
      assert.ok(d.label && d.label[k], `${lang}/runtimes.json 缺 label.${k}`);
    }
  }
  // zh 文案必须与 en 同键集（只查这 7 个，避免整体对齐噪音）
  const zh = JSON.parse(read('src/api/frontend/shell/locales/zh/runtimes.json')).label;
  const en = JSON.parse(read('src/api/frontend/shell/locales/en/runtimes.json')).label;
  for (const k of keys) assert.ok(en[k] && en[k] !== zh[k], `en 的 ${k} 未翻译`);
});