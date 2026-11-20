/**
 * 组织页面路由契约（组织流水线独立页下线后的回归锚点）。
 *
 * 运行：node tests/js/test_org_pipeline_page.js
 *
 * 背景：组织流水线独立页（#org-pipeline/<id> + pages/org_pipeline.js）已下线，
 * 旧深链统一回落到 Vue 组织工作台详情（Organizations.vue）。本用例锁死「下线」
 * 口径，防止页面拆分（独立页）与下线（回落工作台）两种形态再次同时存在：
 * - nav() 必须把 org-pipeline 回落到 organizations；
 * - 不得再装载/渲染已下线的独立页；
 * - 组织详情页保持工作台形态（不恢复旧 Tab / 内嵌下发看板）。
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

function main() {
  const app = read('src/api/frontend/shell/js/app.js');
  const index = read('src/api/frontend/shell/index.html');
  const detail = read('src/api/frontend/app/src/views/Organizations.vue');

  // 1) 独立页已下线：旧深链（#org-pipeline/<id>）回落到组织工作台详情
  assert.ok(
    app.includes("if (view === 'org-pipeline') view = 'organizations'"),
    'nav() 必须把 org-pipeline 深链回落到 organizations',
  );
  assert.ok(
    !app.includes('renderOrgPipeline'),
    '已下线的独立页渲染入口（renderOrgPipeline）必须从 app.js 移除',
  );
  assert.ok(
    !index.includes('pages/org_pipeline.js'),
    'index.html 不得再装载已下线的组织流水线独立页脚本',
  );

  // 2) 组织详情页（Vue 工作台）不得回退到旧的内嵌 Tab 形态
  assert.ok(!detail.includes('_orgSetTab'), '组织详情页不得内嵌旧工作台 Tab');
  assert.ok(!detail.includes('_loadOrgDispatches'), '组织详情页不得内嵌下发看板');
  assert.ok(
    detail.includes('repo_map_title'),
    '组织详情页必须保留仓库地图工作台入口',
  );
  assert.ok(
    detail.includes("ct('organizations.workbench.repos.picker_title')"),
    '组织详情页必须保留关联仓库多选入口',
  );

  console.log('  ✅ org-pipeline 深链回落 organizations（独立页已下线）');
  console.log('  ✅ 独立页脚本不再装载、渲染入口已移除');
  console.log('  ✅ 组织详情页保持工作台形态（未回退内嵌 Tab）');
  console.log('\n✅ All org page routing contract tests passed!');
}

try {
  main();
} catch (err) {
  console.error('\n❌ Test failed:', err.message);
  console.error(err.stack);
  process.exit(1);
}