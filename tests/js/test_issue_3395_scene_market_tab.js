/*
 * Issue #3395 — 切片C：scene-market / skills 页内「插件」Tab 显隐
 *
 * Seam：SceneMarket.vue 一级 Tab（场景 / 技能 / 插件）第三个「🧩 插件」在
 * flag=0 时用 v-if 不渲染（不是 display:none），前两个 Tab 不受影响。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const VUE = fs.readFileSync(path.join(FE, 'app/src/views/SceneMarket.vue'), 'utf8');
const tpl = VUE.slice(0, VUE.indexOf('<script'));
const SKILLS = fs.readFileSync(path.join(FE, 'app/src/views/SkillsView.vue'), 'utf8');
const skillsTpl = SKILLS.slice(0, SKILLS.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('SceneMarket 从统一 helper 读取开关（不自行解析 env/cookie）', function () {
  assert.ok(
    /import\s*\{[^}]*pluginsEnabled[^}]*\}\s*from\s*['"]@\/lib\/features['"]/.test(VUE),
    '应 import { pluginsEnabled } from @/lib/features'
  );
  assert.ok(/const\s+showPlugins\s*=\s*pluginsEnabled\(\)/.test(VUE), '应有 showPlugins 开关常量');
});

check('插件 Tab 带 v-if="showPlugins"（flag=0 不渲染）', function () {
  const i = tpl.indexOf('🧩');
  assert.ok(i >= 0, '模板应存在插件 Tab');
  // 取包含该 Tab 的整个 <a ...> 开标签
  const start = tpl.lastIndexOf('<a', i);
  const openTagEnd = tpl.indexOf('>', i);
  const openTag = tpl.slice(start, openTagEnd + 1);
  assert.ok(/v-if="showPlugins"/.test(openTag), '插件 Tab 开标签应带 v-if="showPlugins"：' + openTag);
  assert.ok(/app\.nav\('plugins'\)/.test(openTag), 'flag=1 时仍指向 plugins 视图');
});

check('场景/技能两个 Tab 不受开关影响', function () {
  const scenes = tpl.indexOf("tt('scenes_tab')");
  const skills = tpl.indexOf("tt('skills_tab')");
  assert.ok(scenes >= 0 && skills >= 0, '应保留场景/技能 Tab');
  const skillsTag = tpl.slice(tpl.lastIndexOf('<a', skills), tpl.indexOf('>', skills) + 1);
  assert.ok(!/v-if/.test(skillsTag), '技能 Tab 不应被开关裁剪');
});

check('SkillsView 从统一 helper 读取开关', function () {
  assert.ok(
    /import\s*\{[^}]*pluginsEnabled[^}]*\}\s*from\s*['"]@\/lib\/features['"]/.test(SKILLS),
    'SkillsView 应 import { pluginsEnabled } from @/lib/features'
  );
  assert.ok(/const\s+showPlugins\s*=\s*pluginsEnabled\(\)/.test(SKILLS), '应有 showPlugins 开关常量');
});

check('SkillsView 插件 Tab 带 v-if="showPlugins"（flag=0 不渲染）', function () {
  const i = skillsTpl.indexOf('🧩');
  assert.ok(i >= 0, 'SkillsView 模板应存在插件 Tab');
  const start = skillsTpl.lastIndexOf('<a', i);
  const end = skillsTpl.indexOf('>', i);
  const openTag = skillsTpl.slice(start, end + 1);
  assert.ok(/v-if="showPlugins"/.test(openTag), '插件 Tab 开标签应带 v-if="showPlugins"：' + openTag);
});

console.log('\n#3395 scene-market/skills 插件 Tab 全部通过 (' + passed + '/5)');