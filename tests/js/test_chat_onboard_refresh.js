/*
 * 飞书扫码成功后按当前视图刷新（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后在 closeOnboardModal(refresh)。
 *   1. 关闭即清理：taskId 置空、定时器全部 clear、弹窗状态复位
 *   2. refresh=true 时按当前视图刷新对应数据源：
 *        im-channel → loadImChannelConfig / VOD bot → refreshVodOnboard / 其它 → loadConfigData
 *   3. 不传 refresh（用户手动关闭/失败取消）不触发刷新
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ChatView.vue'), 'utf8');

function fnBody(name) {
  const i = VIEW.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  let d = 0,
    started = false;
  for (let j = i; j < VIEW.length; j++) {
    if (VIEW[j] === '{') {
      d++;
      started = true;
    } else if (VIEW[j] === '}') {
      d--;
      if (started && d === 0) return VIEW.slice(i, j + 1);
    }
  }
  throw new Error(name + ' 括号不闭合');
}

const B = fnBody('closeOnboardModal');
let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('关闭即复位状态并清定时器', function () {
  assert.ok(/onboardTaskId = null/.test(B), 'taskId 应置空');
  assert.ok(/clearInterval\(onboardTimer\)/.test(B), '应清轮询定时器');
  assert.ok(/clearInterval\(countdownTimer\)/.test(B), '应清倒计时');
  assert.ok(/onboard\.open = false/.test(B), '应关闭弹窗');
});

check('refresh=true 按视图分派刷新', function () {
  assert.ok(/if \(refresh\) \{/.test(B), '刷新需显式开关');
  assert.ok(/_currentView === 'im-channel'/.test(B), 'im-channel 视图走 loadImChannelConfig');
  assert.ok(/loadImChannelConfig/.test(B), '应调用 loadImChannelConfig');
  assert.ok(/refreshVodOnboard/.test(B), 'VOD bot 应走 refreshVodOnboard');
  assert.ok(/loadConfigData\(\)/.test(B), '其余走 loadConfigData');
});

check('扫码成功入口传 refresh=true', function () {
  assert.ok(/closeOnboardModal\(true\)/.test(VIEW), '成功路径应传 true');
  const manual = (VIEW.match(/closeOnboardModal\(\)/g) || []).length;
  assert.ok(manual >= 1, '手动关闭不应触发刷新，实际 ' + manual);
});

console.log('\n#onboard-refresh 全部通过 (' + passed + '/3)');