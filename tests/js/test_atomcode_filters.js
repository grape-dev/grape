/* Execute the actual selector expressions with native and instance names. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = page => fs.readFileSync(path.join(__dirname, '../../src/api/static/js/pages', page + '.js'), 'utf8');
const names = ['atomcode', 'atomcode-leo-mac', 'codex-test', 'unknown'];
const agents = read('agents').match(/const _engineTypeOf = .*?; \};/)[0];
for (const name of names) {
  assert.equal(vm.runInNewContext(agents + '; _engineTypeOf(name)', {name}), name.startsWith('atomcode') ? 'atomcode' : name.startsWith('codex') ? 'codex' : '');
}
const sorted = names.map(name => ({name}));
const dashboard = read('dashboard').match(/const _availTypes = .*?;$/m)[0];
assert.deepEqual(Array.from(vm.runInNewContext(dashboard + '; _availTypes', {sorted})), ['atomcode', 'codex']);
const runtimes = read('runtimes').match(/const _availTypes = _runtimeTab[\s\S]*?: \(_rtDevAvailTypes \|\| \[\]\);/)[0];
assert.deepEqual(Array.from(vm.runInNewContext(runtimes + '; _availTypes', {_runtimeTab:'engines', data:{runtimes:sorted}})), ['atomcode', 'codex']);
const autopilot = read('autopilot').match(/var tn = n\.includes.*?;$/m)[0];
assert.equal(vm.runInNewContext(autopilot + '; tn', {n:'atomcode-leo-mac'}), 'atomcode');
console.log('AtomCode selection and filtering contracts passed');
