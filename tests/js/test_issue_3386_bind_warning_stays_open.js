/**
 * Issue #3386 复审回归：BindDialog 缺 workflow 时的交互约定（源码断言）。
 *
 * Spec：绑定响应含 missing_scopes.workflow 时，弹窗必须保留展示预警与
 * 重新创建令牌入口，用户确认（ackPatWarning）后才 emit('bound')/关闭——
 * 不得在设置 patWarning 后立即 emit('bound')（父组件会立刻关弹窗/跳转，
 * 预警不可达）。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const BIND = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/components/BindDialog.vue'), 'utf8');

test('缺 workflow：设置预警后不立即 emit bound，保留弹窗', () => {
  // 缺 workflow 分支存在且以设置 patWarning 开头
  assert.match(BIND, /missing\.indexOf\('workflow'\) >= 0[\s\S]*?patWarning\.value = t\('github_workflow_scope_missing'\)/,
    '缺 workflow 分支未设置 patWarning');
  // 分支内不得再 emit('bound')（立即上抛会让预警不可达）；剔除注释行后判定
  const branch = BIND.split(/missing\.indexOf\('workflow'\) >= 0/)[1] || '';
  const body = branch.split(/function /)[0]
    .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');
  assert.ok(!body.includes("emit('bound'"), '缺 workflow 分支不应立即 emit bound');
  assert.match(BIND, /return refreshRelations\(\)\s*\n\s*\}/,
    '缺 workflow 分支应仅刷新关系并保留弹窗');
});

test('用户确认后才关闭并 emit：ackPatWarning -> refreshRelations -> bindOk', () => {
  assert.match(BIND, /function ackPatWarning\(\)\s*\{\s*patWarning\.value = ''\s*\n\s*refreshRelations\(\)\.then\(function \(\)\s*\{\s*bindOk\(\)\s*\}\)/,
    'ackPatWarning 应清预警、刷新关系后走 bindOk 关闭/emit');
  // 预警态下保存按钮切换为确认按钮（预警与令牌链接保持可见）
  assert.match(BIND, /v-if="!patWarning"[\s\S]*?savePat\(\)/,
    '保存按钮未按 patWarning 条件渲染');
  assert.match(BIND, /v-else class="bt bt-o bd__wide" @click="ackPatWarning\(\)"/,
    '预警态缺确认按钮');
  // 预警块内保留重新创建令牌链接
  assert.match(BIND, /v-if="patWarning" class="bd__warn"[\s\S]*?patUrl[\s\S]*?open_token_page/,
    '预警块未携带重新创建令牌链接');
});
