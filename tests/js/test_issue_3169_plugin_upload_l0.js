/*
 * Issue #3169 — 插件市场 · Slice 3：上传入口 + L0 门禁结构化错误展示（源码级断言）
 *
 * Seam（用户可见行为 → !1666 契约实查口径）：
 *   1. 市场页「上传插件」入口（非 official scope）→ 弹窗表单：ZIP 文件 + scope（user/repo）
 *      + repo（scope=repo 时当前仓库）+ 分类 + 可选来源三件套（source_repo/source_ref/source_branch）
 *   2. 上传前 L0 门禁先行：POST /api/plugin-market/validate-package（multipart file）
 *      → valid=false 时不上传，错误清单逐条结构化展示（check/code/path/reason/hint 五项齐）
 *   3. 门禁通过 → POST /api/plugins/upload（file + scope + repo + category_id + 来源字段）
 *   4. upload 自身 400（PLUGIN_L0_INVALID，detail.errors 同结构）也要结构化渲染，不合并成一句报错
 *   5. 上传成功后刷新列表
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const VUE = read('app/src/views/PluginMarketView.vue');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}
function bodyAfter(marker) {
  const i = VUE.indexOf(marker);
  assert.ok(i >= 0, '未找到 ' + marker);
  return VUE.slice(i, i + 4000);
}
const tpl = VUE.slice(0, VUE.indexOf('<script'));

check('工具栏有上传插件按钮（非 official scope 显示）', function () {
  assert.ok(/upload_plugin|openUpload/.test(tpl), '模板应有上传入口按钮');
});

check('上传弹窗表单：ZIP 文件 + scope + repo + 分类 + 来源三件套', function () {
  assert.ok(/type="file"/.test(tpl) && /\.zip/.test(tpl), '应有 ZIP 文件选择');
  assert.ok(/uploadForm\.scope/.test(tpl) || /uploadForm\.\w*scope/.test(tpl), '表单应有 scope 字段');
  assert.ok(/category/.test(tpl), '表单应有分类字段');
  assert.ok(/source_repo/.test(tpl), '表单应有来源仓库字段（来源一致性门控声明）');
  assert.ok(/source_ref/.test(tpl), '表单应有来源 commit 字段');
  assert.ok(/source_branch/.test(tpl), '表单应有来源分支字段');
});

check('L0 门禁先行：POST /api/plugin-market/validate-package（multipart file）', function () {
  const b = bodyAfter("function doUpload(");
  assert.ok(/plugin-market\/validate-package/.test(b), '应先调 /api/plugin-market/validate-package');
  const vi = b.indexOf('plugin-market/validate-package');
  const ui = b.indexOf("API.upload('/plugins/upload'");
  assert.ok(vi >= 0 && ui > vi, 'upload 必须在 validate-package 之后（门禁拒绝不上传）');
});

check('门禁拒绝短路：valid=false 不触发 /api/plugins/upload', function () {
  const b = bodyAfter("function doUpload(");
  const vi = b.indexOf('plugin-market/validate-package');
  const ui = b.indexOf("API.upload('/plugins/upload'");
  assert.ok(/\.valid\b/.test(b), '应校验 valid 位');
  // 拒绝分支展示结构化清单后 return，不落到 upload
  const rejectSeg = b.slice(vi, ui);
  assert.ok(/return/.test(rejectSeg), '拒绝分支应短路 return');
});

check('上传契约：POST /api/plugins/upload 携带 file+scope+repo+category_id+来源字段', function () {
  const b = bodyAfter("function doUploadAfterGate(");
  const ui = b.indexOf("API.upload('/plugins/upload'");
  assert.ok(ui >= 0, '应调 POST /plugins/upload');
  const seg = b.slice(0, ui + 800);
  for (const f of ["'file'", "'scope'", "'category_id'"]) {
    assert.ok(seg.includes(f + ','), 'FormData 应带 ' + f);
  }
  assert.ok(/source_repo|'repo'/.test(seg), 'FormData 应带仓库/来源字段');
});

check('结构化错误渲染：check/code/path/reason/hint 五项逐条展示（不合并成一句）', function () {
  // 模板里有逐字段消费（上传门禁错误表）
  for (const k of ['err.check', 'err.code', 'err.path', 'err.reason', 'err.hint']) {
    assert.ok(tpl.includes(k), '模板应逐条展示 ' + k);
  }
  assert.ok(/uploadErrors/.test(tpl), '模板应渲染错误清单容器');
});

check('upload 400 的 errors 数组同样结构化渲染（PLUGIN_L0_INVALID 同构）', function () {
  const b = bodyAfter("function doUploadAfterGate(");
  const ui = b.indexOf("API.upload('/plugins/upload'");
  const seg = b.slice(ui);
  assert.ok(/pushUploadError\(e\)|\.errors/.test(seg), '失败分支应读取 e.errors 数组（#3355 复审后经统一收敛助手）');
  assert.ok(/pushUploadError\(e\)|uploadErrors\.value =/.test(seg), '错误数组应写入 uploadErrors 状态');
});

check('上传成功：toast + 关闭弹窗 + 刷新列表', function () {
  const b = bodyAfter("function doUploadAfterGate(");
  assert.ok(/showUpload\.value = false/.test(b), '成功应关闭弹窗');
  assert.ok(/loadAll\(\)|loadPlugins\(\)/.test(b), '成功应刷新列表');
});

check('repo scope 缺仓库前置拦截', function () {
  const b = bodyAfter("function doUpload(");
  assert.ok(/currentRepo/.test(b), 'scope=repo 应校验当前仓库');
});

console.log('\n#3169 Slice 3 全部通过 (' + passed + '/9)');
