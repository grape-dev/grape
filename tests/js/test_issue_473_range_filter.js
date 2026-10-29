/*
 * Issue #473: 设备与引擎页 runtime 列表范围筛选 — 前端源断言测试
 *
 * 运行：node tests/js/test_issue_473_range_filter.js
 *
 * 验证点：
 *   1. runtimes.js 含范围状态 _rtRange（默认 'all'）与 _RT_RANGE_OPTIONS（全部/仓库/个人）
 *   2. _loadRuntimesData / _loadRuntimeDevices 透传 range 查询参数（非 all 时）
 *   3. 筛选栏渲染范围 chips（_rtSetRange）并展示当前选中范围提示（范围：X）
 *   4. _rtClearFilters 一并重置 _rtRange
 *   5. 后端 settings.py 两端点新增 range 参数与 _rt_in_range 范围过滤
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const RUNTIMES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf8');
const SETTINGS_PY = fs.readFileSync(path.join(ROOT, 'src/api/routes/settings.py'), 'utf8');

// 1. 范围状态与选项
assert.ok(/let _rtRange = 'all';/.test(RUNTIMES_JS), '应有范围状态 _rtRange 默认 all');
assert.ok(/_RT_RANGE_OPTIONS\s*=\s*\[/.test(RUNTIMES_JS), '应有 _RT_RANGE_OPTIONS 选项表');
for (const label of ['全部', '仓库', '个人']) {
  assert.ok(RUNTIMES_JS.includes(`label: '${label}'`), `范围选项应含「${label}」`);
}

// 2. 两个加载函数透传 range 参数
const loadEngines = RUNTIMES_JS.match(/function _loadRuntimesData\(\) \{[\s\S]*?\n\}/)[0];
assert.ok(/&range=' \+ encodeURIComponent\(_rtRange\)/.test(loadEngines), 'Engines 加载应透传 range 参数');
const loadDevices = RUNTIMES_JS.match(/function _loadRuntimeDevices\(\) \{[\s\S]*?\n\}/)[0];
assert.ok(/&range=' \+ encodeURIComponent\(_rtRange\)/.test(loadDevices), 'Workers 加载应透传 range 参数');
assert.ok(/_rtRange !== 'all'/.test(loadEngines) && /_rtRange !== 'all'/.test(loadDevices),
  'range=all（默认全量口径）不应发送 range 参数');

// 3. 筛选栏范围 chips + 选中范围提示
assert.ok(/_rtSetRange\('\$\{r\.key\}'\)/.test(RUNTIMES_JS), '范围 chips 应调用 _rtSetRange');
assert.ok(RUNTIMES_JS.includes('范围：${'), '筛选栏应展示当前选中范围提示');
assert.ok(/window\._rtSetRange = function\(r\) \{/.test(RUNTIMES_JS), '应导出 _rtSetRange');

// 4. 清除联动
const clearFn = RUNTIMES_JS.match(/window\._rtClearFilters = function\(\) \{[\s\S]*?\n\};/)[0];
assert.ok(/_rtRange = 'all';/.test(clearFn), '_rtClearFilters 应重置范围为 all');

// 5. 后端 range 参数 + 范围过滤
// PR #668 审查修复：range 参数升级为 Literal["", "all", "repo", "user"]（非法值 422），
// 本机引擎归属改按 DB 行 owner_id（local_owner）判定
assert.ok(/def get_agent_runtimes\([\s\S]*?range: RTRange = ""\)/.test(SETTINGS_PY),
  'get_agent_runtimes 应有 range 参数（Literal 校验）');
assert.ok(/def get_runtime_devices\([\s\S]*?range: RTRange = ""\)/.test(SETTINGS_PY),
  'get_runtime_devices 应有 range 参数（Literal 校验）');
assert.ok(/RTRange = Literal\["", "all", "repo", "user"\]/.test(SETTINGS_PY),
  '应定义 RTRange Literal 合法值');
assert.ok(/def _rt_in_range\(/.test(SETTINGS_PY), '应有 _rt_in_range 范围判定 helper');
assert.ok(SETTINGS_PY.includes('local_owner'), '_rt_in_range 应支持本机引擎 DB 行 owner_id 归属');
assert.ok(SETTINGS_PY.includes('_rt_in_range(d.get("scope")'), '设备列表应按范围过滤');
assert.ok(SETTINGS_PY.includes('_rt_in_range(rt.get("scope")'), '引擎列表应按范围过滤');

console.log('✅ Issue #473 前端范围筛选源断言全部通过（含 PR #668 审查修复）');
