/*
 * Issue #3694 — 删除技能确认弹窗把 {{name}} 当字面量直出
 *
 * Seam（用户可见行为）：打开删除确认弹窗时，标题展示当前被删技能的真实名称，
 * 界面任何位置不出现 `{{name}}` / `{name}` 占位符；标题与正文不重复同句。
 *
 * 前端无组件单测框架，沿用仓库 tests/js 的源码级断言口径：
 *   1) 语言包契约（降级词条 + 原模板占位符成对保留）
 *   2) 从 SFC 抽取**真实正则字面量**做替换，验证任意名称下无占位符残留
 *   3) 模板结构（标题绑定 computed / 去重 / 长名样式 / 交互不变）
 *
 * 运行：node tests/js/test_issue_3694_skill_delete_confirm.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const FE = path.join(ROOT, 'src/api/frontend');
const VIEW = fs.readFileSync(path.join(FE, 'app/src/views/SkillsView.vue'), 'utf8');
const zh = JSON.parse(fs.readFileSync(path.join(FE, 'shell/locales/zh/skills.json'), 'utf8'));
const en = JSON.parse(fs.readFileSync(path.join(FE, 'shell/locales/en/skills.json'), 'utf8'));
const INDEX_HTML = fs.readFileSync(path.join(FE, 'shell/index.html'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

// 删除确认弹窗模板块（到转换弹窗注释为止）
const DEL_START = '<!-- ═══ 删除确认弹窗 ═══ -->';
const DEL_END = '<!-- ═══ 转换（复制）确认弹窗 ═══ -->';
function deleteBlock() {
  const i = VIEW.indexOf(DEL_START);
  const j = VIEW.indexOf(DEL_END);
  assert.ok(i >= 0 && j > i, 'SkillsView.vue 应存在「删除确认弹窗」模板块');
  return VIEW.slice(i, j);
}

// 从 SFC 中截取某个 computed 的定义体（到下一个顶层 `})` 或 `const ` 为止）
function computedBody(name) {
  const i = VIEW.indexOf('const ' + name + ' = computed(');
  assert.ok(i >= 0, 'SkillsView.vue 应定义 computed `' + name + '`');
  const j = VIEW.indexOf('\nconst ', i + 10);
  return VIEW.slice(i, j > i ? j : i + 800);
}

// 从源码里抽取 `.replace(/<pattern>/<flags>, ...)` 的真实正则（拒绝测试自造逻辑）
function replaceRegexOf(src) {
  const m = /\.replace\(\s*\/((?:\\\/|[^/\n])+)\/([a-z]*)\s*,/.exec(src);
  assert.ok(m, '标题 computed 应通过 .replace(/<pattern>/, name) 做占位符插值');
  return new RegExp(m[1], m[2]);
}

const PLACEHOLDER = /\{\{?\s*name\s*\}?\}/;

check('T1 语言包：zh/en 均新增空名降级词条 confirm.delete_skill_fallback', function () {
  ['zh', 'en'].forEach(function (lang) {
    const pack = lang === 'zh' ? zh : en;
    const v = pack.confirm && pack.confirm.delete_skill_fallback;
    assert.ok(typeof v === 'string' && v.trim(), lang + ' skills.json 缺 confirm.delete_skill_fallback');
  });
  assert.strictEqual(zh.confirm.delete_skill_fallback, '该技能');
  assert.strictEqual(en.confirm.delete_skill_fallback, 'this skill');
});

check('T2 语言包：原模板 confirm.delete_skill 仍成对保留 {{name}}（#3355 契约不变）', function () {
  assert.ok(/\{\{name\}\}/.test(zh.confirm.delete_skill), 'zh 原模板应保留 {{name}}');
  assert.ok(/\{\{name\}\}/.test(en.confirm.delete_skill), 'en 原模板应保留 {{name}}');
});

check('T3 被删名取自当前对象：titleOf(deleteCtx.skill) + 空名降级词条', function () {
  const body = computedBody('deleteSkillName');
  assert.ok(body.indexOf('deleteCtx.skill') >= 0, 'deleteSkillName 应读取 deleteCtx.skill');
  assert.ok(body.indexOf('titleOf(') >= 0, 'deleteSkillName 应复用 titleOf（display_name 优先）');
  assert.ok(/tt2\('confirm\.delete_skill_fallback'\)/.test(body),
    '空名时应降级到 tt2(\'confirm.delete_skill_fallback\')');
  // 展示名口径与 titleOf 一致：display_name 优先、回退 name
  const titleOfIdx = VIEW.indexOf('function titleOf(s)');
  const titleOfBody = VIEW.slice(titleOfIdx, titleOfIdx + 200);
  assert.ok(/display_name\s*\|\|\s*s\.name|s\.display_name\s*\|\|\s*s\.name/.test(titleOfBody),
    'titleOf 应保持 display_name 优先、回退 name');
});

check('T4 标题 computed：取 tc2 模板后替换 name 占位符，且对 {{name}}/{name} 均生效', function () {
  const body = computedBody('deleteTitle');
  assert.ok(/tc2\('skills\.confirm\.delete_skill'\)/.test(body), 'deleteTitle 应以 tc2 原模板为基准');
  const re = replaceRegexOf(body);

  const zhTpl = zh.confirm.delete_skill;
  const enTpl = en.confirm.delete_skill;
  const samples = ['代码审查', 'code-review', 'A B C', '删除技能这个超长名称用于验证换行与收束不会溢出对话框'];
  samples.forEach(function (name) {
    const zhOut = zhTpl.replace(re, name);
    const enOut = enTpl.replace(re, name);
    assert.ok(!PLACEHOLDER.test(zhOut), 'zh 标题仍残留占位符：' + zhOut);
    assert.ok(!PLACEHOLDER.test(enOut), 'en 标题仍残留占位符：' + enOut);
    assert.ok(zhOut.indexOf(name) >= 0, 'zh 标题应包含真实技能名：' + zhOut);
    assert.ok(enOut.indexOf(name) >= 0, 'en 标题应包含真实技能名：' + enOut);
  });

  // 双/单花括号两种写法都要替换（历史扁平键为 {name}）
  assert.ok(!PLACEHOLDER.test(zhTpl.replace(re, 'X')) && zhTpl.replace(re, 'X') !== zhTpl,
    '双花括号 {{name}} 应被替换');
  const single = 'Delete skill {name}?';
  assert.strictEqual(single.replace(re, 'X'), 'Delete skill X?', '单花括号 {name} 也应被替换');
  // 含空格写法
  assert.strictEqual('技能 {{ name }} 删除'.replace(re, 'X'), '技能 X 删除', '占位符内空格应容忍');

  // 空名降级值参与替换后同样无残留
  const fallbackZh = zhTpl.replace(re, zh.confirm.delete_skill_fallback);
  assert.ok(!PLACEHOLDER.test(fallbackZh) && fallbackZh.indexOf('该技能') >= 0,
    '空名降级后标题应含「该技能」且无占位符');
});

check('T5 模板：标题绑定 deleteTitle，正文移除重复句与孤立 🗑 文本行', function () {
  const block = deleteBlock();
  assert.ok(block.indexOf(':title="deleteTitle"') >= 0, ':title 应绑定 deleteTitle');
  assert.ok(!/:title="tc2\(/.test(block), ':title 不应再直出 tc2 原模板');
  assert.ok(block.indexOf('<h3>') < 0, '正文不应保留重复同句的 <h3>');
  assert.ok(block.indexOf('tc2(') < 0, '正文不应再调用 tc2（避免重复同句）');
  assert.ok(block.indexOf("tt('delete_skill_tip')") >= 0, '正文应保留 delete_skill_tip');
  assert.ok(block.indexOf('sk__conf-ic') >= 0, '正文应保留图标位');
  // 模板里全文只出现一次删除模板调用（即 deleteTitle computed 内部）
  const calls = (VIEW.match(/tc2\('skills\.confirm\.delete_skill'\)/g) || []).length;
  assert.strictEqual(calls, 1, '删除模板调用应只剩 deleteTitle computed 一处，实际 ' + calls);
});

check('T6 长名不溢出：:deep(.a-modal__title) 具备 overflow-wrap/word-break', function () {
  const m = /:deep\(\.a-modal__title\)\s*\{([^}]*)\}/.exec(VIEW);
  assert.ok(m, '缺少 :deep(.a-modal__title) 样式规则');
  assert.ok(/overflow-wrap:\s*anywhere/.test(m[1]), '标题应 overflow-wrap: anywhere');
  assert.ok(/word-break:\s*break-word/.test(m[1]), '标题应 word-break: break-word');
});

check('T7 交互不变：取消/确认删除、loading 文案、删除接口调用保持原样', function () {
  const block = deleteBlock();
  assert.ok(/@click="deleteCtx\.open = false"/.test(block), '取消仍应关闭弹窗');
  assert.ok(/@click="confirmDelete"/.test(block), '确认仍应调用 confirmDelete');
  assert.ok(/deleteCtx\.busy \? tt\('deleting'\) : tt\('confirm_delete'\)/.test(block),
    '按钮 loading 文案应保持不变');
  assert.ok(VIEW.indexOf("G.API.del('/skills/by-id/'") >= 0, '删除接口调用路径应保持不变');
  const askIdx = VIEW.indexOf('function askDelete(s)');
  const askBody = VIEW.slice(askIdx, askIdx + 200);
  assert.ok(/deleteCtx\.skill = s/.test(askBody), 'askDelete 应把当前技能写入 deleteCtx.skill');
});

check('T8 i18n 缓存版本：新增词条后 index 页 cacheVer 不低于 82', function () {
  const m = /__I18N_CACHE_VER__\s*=\s*'(\d+)'/.exec(INDEX_HTML);
  assert.ok(m, 'shell/index.html 应有 __I18N_CACHE_VER__');
  assert.ok(parseInt(m[1], 10) >= 82, '新增语言包词条应 bump cacheVer（当前 ' + m[1] + '）');
});

console.log('\n#3694 删除确认弹窗插值通过 (' + passed + '/8)');