/**
 * Issue #2346 任务接管：前端状态面（2026-11-13 补齐）
 *
 * 后端 takeover 是一等资源（8 个 /api/takeover/* 接口），但 Vue 线此前零入口。
 * 按「正式版不建运维面板、状态用徽标」的口径，前端只做两件事：
 *   T1. 看板卡 / 任务详情头打「接管中」药丸（谁 · 哪台机器 · 什么 agent）
 *   T2. 点药丸 = 结束接管（abort，只停本节点不重派），带确认弹层
 *   T3. 会话来自服务端 /takeover/sessions?repo_key=&status=active（刷新后仍在）
 *   T4. 文案 zh/en 齐备；药丸样式符合全站药丸语言（11px/600/999px）
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const ISS = read('src/api/frontend/app/src/views/IssuesView.vue');

test('T1 看板卡与详情头都有接管中药丸', () => {
  const pills = ISS.match(/class="isk-tko-pill"/g) || [];
  assert.ok(pills.length >= 2, `药丸出现 ${pills.length} 次（看板卡 + 详情头应各一处）`);
  assert.match(ISS, /v-if="takeoverOf\(tk\)"/, '看板卡未按会话显示');
  assert.match(ISS, /v-if="takeoverOf\(detailView\)"/, '详情头未按会话显示');
  assert.match(ISS, /:title="takeoverTitle\(tk\)"/, '缺 hover 说明');
});

test('T2 点药丸结束接管（确认 + abort + 刷新）', () => {
  assert.match(ISS, /@click\.stop="abortTakeover\(tk\)"/, '看板卡点击未阻止冒泡');
  assert.match(ISS, /@click="abortTakeover\(detailView\)"/, '详情头点击未接 abort');
  assert.match(ISS, /G\.API\.post\('\/takeover\/sessions\/' \+ s\.id \+ '\/abort'/, 'abort 路径不对');
  assert.match(ISS, /t\('takeover\.abort_confirm'\)/, '缺确认文案');
  assert.match(ISS, /loadTakeovers\(\)/, 'abort 后未刷新会话');
});

test('T3 会话来自服务端 active 列表', () => {
  assert.match(ISS, /'\/takeover\/sessions\?repo_key=' \+ encodeURIComponent\(key\) \+ '&status=active&limit=\d+'/, '未按 repo_key+active 拉取');
  assert.match(ISS, /issue_id \|\| ''\)\.replace\('#', ''\)/, 'issue_id 未归一（#12 → 12）');
  assert.match(ISS, /loadTakeovers\(\)\n  startPoll\(\)/, 'onMounted 未触发首次加载');
});

test('T4 文案 zh/en 齐备 + 药丸样式', () => {
  const keys = ['active', 'abort_confirm', 'abort_done', 'abort_failed'];
  for (const lang of ['zh', 'en']) {
    const d = JSON.parse(read(`src/api/frontend/shell/locales/${lang}/tasks.json`));
    assert.ok(d.takeover, `${lang}/tasks.json 缺 takeover 段`);
    for (const k of keys) assert.ok(d.takeover[k], `${lang} 缺 takeover.${k}`);
  }
  assert.match(ISS, /\.isk-tko-pill \{[\s\S]{0,400}background: #6d43c9;/, '药丸底色不是 #6d43c9');
  assert.match(ISS, /\.isk-tko-pill \{[\s\S]{0,400}border-radius: 999px;/, '药丸不是 999px');
  assert.match(ISS, /\.isk-tko-pill \{[\s\S]{0,400}font-size: 11px;/, '药丸字号不是 11px');
});