/**
 * QA BLOCKED-3（D1 国际化）：生产页面不得硬编码中文。
 *
 * 1) 内测申请页（/static-vue/beta-apply）：文案全部走 beta_apply 语言包，
 *    提交值仍是 zh 规范文本（后端契约不变）。
 * 2) 模板市场：流水线阶段 / 标签亮点 / 弹窗小节标题全部走 templates 语言包。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const BetaApplied = read('src/api/frontend/app/src/pages/BetaApply.vue');
const Templates = read('src/api/frontend/app/src/views/TemplatesView.vue');

const CQ = /[\u4e00-\u9fff]/;

test('T1 BetaApply 模板零中文，文案全走 t()', () => {
  const tpl = BetaApplied.slice(0, BetaApplied.indexOf('<script setup>'));
  assert.ok(!CQ.test(tpl), '模板仍有中文字面量');
  for (const k of ['title', 'subtitle', 'step_basic', 'step_identity', 'step_reason', 'submit',
                   'success_title', 'countdown_suffix']) {
    assert.ok(tpl.includes(`t('${k}'`), `模板未使用 t('${k}')`);
  }
  // 选项以 key 驱动，提交值取 zh 规范文本
  assert.match(BetaApplied, /const educationOpts = \['edu_bachelor'/, '学历选项未 key 化');
  assert.match(BetaApplied, /function v\(key\) \{[\s\S]{0,80}zhPack\.value\[key\]/, '缺 wire value 解析');
  assert.match(BetaApplied, /fetch\('\/static\/locales\/' \+ l \+ '\/beta_apply\.json'\)/, '未加载语言包');
});

test('T2 BetaApply 中/英语言包键集一致且无中文残留', () => {
  const zh = JSON.parse(read('src/api/frontend/shell/locales/zh/beta_apply.json'));
  const en = JSON.parse(read('src/api/frontend/shell/locales/en/beta_apply.json'));
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort(), 'zh/en 键集不一致');
  const cjkEn = Object.entries(en).filter(([, v]) => CQ.test(v)).map(([k]) => k);
  assert.deepEqual(cjkEn, [], 'en 包里有中文');
  assert.equal(zh.opt_other, '其他', 'opt_other 必须是后端规范值“其他”');
});

test('T3 模板市场：阶段/标签/小节标题走 templates 语言包', () => {
  assert.match(Templates, /const PIPELINE_STAGES = \{[\s\S]{0,300}'require'/, '阶段表未 key 化');
  assert.match(Templates, /G\.I18N\.t\('templates\.stage\.' \+ k\)/, '阶段渲染未走 templates.stage');
  assert.match(Templates, /const TAG_HIGHLIGHTS = \{\n  python: 'tag\.python'/, '标签亮点未 key 化');
  assert.match(Templates, /G\.I18N\.t\(key\)/, '标签渲染未翻译');
  for (const k of ['section_desc', 'section_highlights', 'section_readme', 'view_source', 'fork_one_click']) {
    assert.ok(Templates.includes(`t('templates.${k}')`), `未使用 templates.${k}`);
  }
});

test('T4 templates 语言包 stage/tag/文案键 zh/en 齐备', () => {
  const zh = JSON.parse(read('src/api/frontend/shell/locales/zh/templates.json'));
  const en = JSON.parse(read('src/api/frontend/shell/locales/en/templates.json'));
  assert.deepEqual(Object.keys(zh.stage).sort(), Object.keys(en.stage).sort(), 'stage 键集不一致');
  assert.deepEqual(Object.keys(zh.tag).sort(), Object.keys(en.tag).sort(), 'tag 键集不一致');
  assert.equal(Object.keys(zh.stage).length, 20);
  assert.ok(Object.keys(zh.tag).length >= 34);
  // 模板阶段实际用到的短 key 必须都在包内（渲染时统一加 templates.stage. 前缀）
  const block = Templates.slice(Templates.indexOf('const PIPELINE_STAGES = {'),
                               Templates.indexOf('function pipelineStages(x)'));
  const used = new Set((block.match(/'([a-z_]{3,})'/g) || []).map((s) => s.slice(1, -1)));
  assert.ok(used.size >= 15, '阶段 key 用量异常: ' + used.size);
  for (const key of used) {
    assert.ok(zh.stage[key], `templates.stage.${key} 缺失`);
  }
});