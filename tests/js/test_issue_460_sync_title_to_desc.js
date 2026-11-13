/*
 * Issue #460 — 「标题同步到描述」按钮（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试从 legacy `js/pages/issues.js` 提取 syncTitleToDesc
 * 在 vm 沙箱执行；该页已随 Vue 迁移删除，改为对 IssuesView.vue 的源码级断言。
 * 断言语义与原用例一致，强度略降（不再执行逻辑，改为校验实现结构与守卫顺序）：
 *   1. syncTitleToDesc 存在且由模板按钮触发（@click.prevent）
 *   2. 标题为空 → 提前 return，不改描述（无副作用）
 *   3. 描述已含标题 → 幂等跳过，不重复追加
 *   4. 追加格式 = 原描述 + 空行 + 标题；无描述时直接写入标题
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const SRC_PATH = path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue');
const src = fs.readFileSync(SRC_PATH, 'utf8');

function fnBody(source, name) {
  const i = source.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到函数 ' + name);
  let depth = 0;
  let started = false;
  for (let j = i; j < source.length; j++) {
    if (source[j] === '{') {
      depth++;
      started = true;
    } else if (source[j] === '}') {
      depth--;
      if (started && depth === 0) return source.slice(i, j + 1);
    }
  }
  throw new Error('函数 ' + name + ' 括号不闭合');
}

const body = fnBody(src, 'syncTitleToDesc');
let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('模板按钮触发 @click.prevent="syncTitleToDesc"', function () {
  assert.ok(/@click\.prevent="syncTitleToDesc"/.test(src), '模板应绑定 syncTitleToDesc');
});

check('标题空 → 提前 return（不改描述）', function () {
  const guard = body.indexOf('if (!title) return');
  const write = body.indexOf('modal.desc =');
  assert.ok(guard >= 0, '应存在 if (!title) return');
  assert.ok(write > guard, '标题守卫必须在写描述之前');
});

check('描述已含标题 → 幂等跳过', function () {
  assert.ok(
    /if\s*\(desc\.indexOf\(title\)\s*>=\s*0\)\s*return/.test(body),
    '应存在 desc.indexOf(title) >= 0 的幂等守卫',
  );
});

check('追加格式 = 原描述 + 空行 + 标题', function () {
  assert.ok(
    body.indexOf("desc + '\\n\\n' + title") >= 0,
    "追加格式应为 desc + '\\n\\n' + title",
  );
  assert.ok(/modal\.desc = desc \?/.test(body), '无描述时应直接写入标题（三元分支）');
});

console.log('\n#460 全部通过 (' + passed + '/4)');