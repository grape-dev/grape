/*
 * Issue #3734：任务看板会话失效（401）后轮询不停机的行为回归。
 *
 * 运行：node tests/js/test_issue_3734_poll_guard.js
 *
 * 验证点：
 *   1. pollGuard 纯函数：401/sessionExpired → stop；非 401 连续 3 次失败 → degrade；
 *      成功即清零（resetPollGuard）；降频间隔 60s、常规 30s。
 *   2. api.js handleResp 401 无 retryFn 分支：广播 grape:session-expired 且同一失效
 *      周期只广播一次（去重）。
 *   3. IssuesView.vue：监听会话失效事件 → stopPoll + 横幅 + 重新登录入口；
 *      boardFetch 开头短路（每轮 5 列 401 压到 1 条）；catch 先行 401 判定；
 *      连续失败降频；新增 _startScenePoll 桥与 _stopScenePoll 对称。
 *   4. app.js：showLogin 停 tasks 轮询；登录恢复后显式回启 tasks 轮询。
 *   5. zh/en 语言包含横幅文案键。
 */
const assert = require('assert')
const path = require('path')
const fs = require('fs')
const os = require('os')

const root = path.resolve(__dirname, '../..')
const LIB = path.join(root, 'src/api/frontend/app/src/lib/pollGuard.js')
const VIEW = path.join(root, 'src/api/frontend/app/src/views/IssuesView.vue')
const API = path.join(root, 'src/api/frontend/shell/js/api.js')
const APP = path.join(root, 'src/api/frontend/shell/js/app.js')

let pass = 0
let fail = 0
function ok(cond, name) {
  if (cond) {
    pass++
  } else {
    fail++
    console.error('✗ ' + name)
  }
}

;(async function main() {
  // ---- 1. pollGuard 纯函数 ----
  const source = fs.readFileSync(LIB, 'utf8')
  const tmpFile = path.join(os.tmpdir(), 'pollGuard-' + process.pid + '.mjs')
  fs.writeFileSync(tmpFile, source)
  const mod = await import('file://' + tmpFile)

  ok(mod.POLL_INTERVAL_MS === 30000, '常规轮询间隔 30s')
  ok(mod.POLL_DEGRADE_INTERVAL_MS === 60000, '降频间隔 60s')
  ok(mod.POLL_FAILURE_LIMIT === 3, '连续失败阈值 3')

  ok(
    mod.nextPollAction({ status: 401, failures: 0, sessionExpired: false }) === 'stop',
    '401 → 立即停机'
  )
  ok(
    mod.nextPollAction({ status: 200, failures: 0, sessionExpired: true }) === 'stop',
    '会话已失效 → 停机'
  )
  ok(
    mod.nextPollAction({ status: 200, failures: 0, sessionExpired: false }) === 'continue',
    '正常 → 继续'
  )
  ok(
    mod.nextPollAction({ status: 200, failures: 2, sessionExpired: false }) === 'continue',
    '失败 2 次 → 仍继续'
  )
  ok(
    mod.nextPollAction({ status: 500, failures: 3, sessionExpired: false }) === 'degrade',
    '连续失败 3 次 → 降频'
  )
  ok(
    mod.nextPollAction({ status: 500, failures: 9, sessionExpired: false }) === 'degrade',
    '失败更多仍降频（不退化为死循环停止）'
  )
  ok(mod.pollIntervalFor('continue') === 30000, 'continue 用 30s')
  ok(mod.pollIntervalFor('degrade') === 60000, 'degrade 用 60s')

  const fresh = mod.resetPollGuard()
  ok(fresh.failures === 0 && fresh.sessionExpired === false, 'resetPollGuard 清零')

  const state = mod.createPollGuardState()
  ok(state.failures === 0 && state.sessionExpired === false, 'createPollGuardState 初值')

  // ---- 2. api.js 401 广播（去重）----
  const api = fs.readFileSync(API, 'utf8')
  ok(/grape:session-expired/.test(api), 'api.js 广播会话失效事件')
  ok(/dispatchEvent\(\s*new CustomEvent\(\s*['"]grape:session-expired['"]/.test(api), '用 CustomEvent 广播')
  ok(/sessionExpiredNotified|_sessionExpiredNotified|sessionExpiredBroadcast/.test(api), '广播有模块级去重标记')
  ok(
    /sessionExpiredNotified\s*=\s*true|_sessionExpiredNotified\s*=\s*true|sessionExpiredBroadcast\s*=\s*true/.test(
      api
    ),
    '广播后置去重标记'
  )
  ok(/grape:session-expired/.test(api) && /showLogin/.test(api), '401 与 showLogin 在同一失效闭环')

  // ---- 3. IssuesView.vue 接线 ----
  const vue = fs.readFileSync(VIEW, 'utf8')
  ok(/from ['"]\.\.\/lib\/pollGuard\.js['"]/.test(vue), 'IssuesView 引入 pollGuard')
  ok(/nextPollAction\(/.test(vue), '轮询决策使用 nextPollAction')
  ok(/sessionExpired/.test(vue), '看板有 sessionExpired 状态')
  ok(/addEventListener\(\s*['"]grape:session-expired['"]/.test(vue), 'onMounted 监听会话失效')
  ok(/removeEventListener\(\s*['"]grape:session-expired['"]/.test(vue), 'onBeforeUnmount 移除监听')
  ok(/G\.window\._startScenePoll\s*=\s*startPoll/.test(vue), '_startScenePoll 与 _stopScenePoll 对称')
  ok(/G\.window\._stopScenePoll\s*=\s*stopPoll/.test(vue), '保留 _stopScenePoll 契约')
  ok(/sessionExpired\.value\)\s*\{[\s\S]{0,120}?abandonBoardRound/.test(vue), 'boardFetch 开头短路并收敛回合')
  ok(/e\.status\s*===\s*401/.test(vue), 'loadTasks/boardFetch catch 判 401')
  ok(/board\.session_expired/.test(vue), '横幅使用 i18n 文案键')
  ok(/board\.relogin/.test(vue), '横幅带重新登录按钮文案键')
  ok(/app\(\)\.showLogin\(\)/.test(vue), '横幅按钮回到登录视图')
  ok(/pollIntervalFor\(/.test(vue), '降频使用 pollGuard 间隔')
  ok(/isk-session-banner/.test(vue), '会话失效横幅进模板')
  ok(
    /function startPoll\(\) \{[\s\S]{0,240}?sessionExpired\.value = false/.test(vue),
    'startPoll 复位失效态（登录恢复即重启）'
  )
  ok(
    /function armPollTimer\(\) \{[\s\S]{0,160}?clearInterval\(pollTimer\)/.test(vue),
    '挂表前清旧 timer（防双定时器）'
  )

  // ---- 4. app.js 登录/登出收口 ----
  const app = fs.readFileSync(APP, 'utf8')
  ok(
    /_stopScenePoll/.test(app.slice(app.indexOf('function showLogin()'), app.indexOf('function showLogin()') + 4000)),
    'showLogin 停 tasks 轮询'
  )
  ok(/function _resumeScenePollAfterLogin\(\)/.test(app), '登录恢复后有回启 helper')
  ok(
    /_resumeScenePollAfterLogin\(\)/.test(app) && /_startScenePoll/.test(app),
    '登录恢复调用 _startScenePoll'
  )

  // ---- 5. i18n 键 ----
  const zh = JSON.parse(fs.readFileSync(path.join(root, 'src/api/frontend/shell/locales/zh/tasks.json'), 'utf8'))
  const en = JSON.parse(fs.readFileSync(path.join(root, 'src/api/frontend/shell/locales/en/tasks.json'), 'utf8'))
  ok(zh.board && zh.board.session_expired && zh.board.relogin, 'zh 含会话失效横幅键')
  ok(en.board && en.board.session_expired && en.board.relogin, 'en 含会话失效横幅键')

  console.log(`结果: ${pass} 通过, ${fail} 失败`)
  try {
    fs.unlinkSync(tmpFile)
  } catch (e) {}
  process.exit(fail === 0 ? 0 : 1)
})()