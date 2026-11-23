/*
 * Issue #3395 — 前端功能开关：统一读取 helper 的双态行为
 *
 * Seam：src/api/frontend/app/src/lib/features.js 是前端唯一读取入口，
 * 数据来自服务端 shell HTML 注入的 window.__FEATURES__。
 *   - 缺省/非 false → 开启（默认零变化）
 *   - plugins === false → 关闭
 */
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const MOD = path.resolve(
  __dirname,
  '../../src/api/frontend/app/src/lib/features.js'
);

function withFlags(flags, fn) {
  const prev = globalThis.__FEATURES__;
  globalThis.__FEATURES__ = flags;
  try {
    fn();
  } finally {
    if (prev === undefined) delete globalThis.__FEATURES__;
    else globalThis.__FEATURES__ = prev;
  }
}

(async function run() {
  const mod = await import(pathToFileURL(MOD).href);
  let passed = 0;
  function check(name, fn) {
    fn();
    passed++;
    console.log('  ✓ ' + name);
  }

  check('未注入 window.__FEATURES__ 时默认开启（零变化）', function () {
    withFlags(undefined, function () {
      assert.strictEqual(mod.pluginsEnabled(), true);
    });
  });

  check('服务端下发 plugins=true → 开启', function () {
    withFlags({ plugins: true }, function () {
      assert.strictEqual(mod.pluginsEnabled(), true);
    });
  });

  check('服务端下发 plugins=false → 关闭', function () {
    withFlags({ plugins: false }, function () {
      assert.strictEqual(mod.pluginsEnabled(), false);
    });
  });

  check('未知 flag 名回退默认开启，不误伤其它入口', function () {
    withFlags({ plugins: false }, function () {
      assert.strictEqual(mod.isFeatureEnabled('unknown', true), true);
      assert.strictEqual(mod.isFeatureEnabled('unknown', false), false);
    });
  });

  console.log('\n#3395 feature helper 全部通过 (' + passed + '/4)');
})().catch(function (e) {
  console.error(e && e.stack ? e.stack : e);
  process.exit(1);
});