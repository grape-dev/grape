/*
 * 平台列表 bot 摘要的行为回归：只回答"有没有可用 bot"，不印裸计数。
 *
 * 运行：node tests/js/test_bot_summary.js
 *
 * 验证点：
 *   1. 无 bot 行 → 说"未配置（回落用户 PAT）"，而不是数字 0。
 *   2. 有行但全非 active → 说"全部不可用（回落用户 PAT）"，不能说成未配置。
 *   3. 有 active 行 → 计数只数 active，并带默认账号名。
 *   4. active 行里没有默认 → 只给计数，不拼半个 "默认:"。
 *   5. bot_accounts 缺失/null（旧快照）→ 按未配置处理，不抛。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '../..');
const SRC = path.join(root, 'src/api/frontend/app/src/lib/bot-summary.js');

const ZH = {
  'page.bots_none_short': 'bot 未配置（回落用户 PAT）',
  'page.bots_all_inactive': 'bot 全部不可用（回落用户 PAT）',
  'page.col_bots': 'bot 账号数',
  'page.bot_default': '默认',
};

async function load() {
  const code = fs.readFileSync(SRC, 'utf8').replace(/^export\s+/m, '');
  const url = 'data:text/javascript;base64,' + Buffer.from(code + '\nexport { botSummary };\n').toString('base64');
  return import(url);
}

(async () => {
  const { botSummary } = await load();
  const tr = (k) => ZH[k] || k;

  // 1. 无 bot
  assert.strictEqual(
    botSummary({ provider: 'gitee', bot_accounts: [] }, tr),
    'bot 未配置（回落用户 PAT）',
    '无 bot 行必须说明回落语义，而不是 0'
  );

  // 2. 行在，但全部不可用
  assert.strictEqual(
    botSummary({ bot_accounts: [{ username: 'a', status: 'revoked' }, { username: 'b', status: 'invalid' }] }, tr),
    'bot 全部不可用（回落用户 PAT）',
    '吊销/失效行不能被读成"未配置"或"可用"'
  );

  // 3. 两个 active，其中一个默认
  assert.strictEqual(
    botSummary(
      {
        bot_accounts: [
          { username: 'ci-bot', status: 'active', is_default: true },
          { username: 'mirror-bot', status: 'active' },
        ],
      },
      tr
    ),
    'bot 账号数 2 · 默认:ci-bot',
    '计数只数 active，并带默认账号'
  );

  // 4. active 但无默认
  assert.strictEqual(
    botSummary({ bot_accounts: [{ username: 'solo', status: 'active' }] }, tr),
    'bot 账号数 1',
    '没有默认账号时不得留半个"默认:"'
  );

  // 5. 旧快照 / 缺字段
  assert.strictEqual(botSummary({ provider: 'gitlab' }, tr), 'bot 未配置（回落用户 PAT）', '缺 bot_accounts 不得抛');
  assert.strictEqual(botSummary(null, tr), 'bot 未配置（回落用户 PAT）', 'null 平台不得抛');

  // 6. 状态缺省按 active（后端口径：status 默认 'active'）
  assert.strictEqual(
    botSummary({ bot_accounts: [{ username: 'legacy' }] }, tr),
    'bot 账号数 1',
    'status 缺省应视作 active'
  );

  console.log('test_bot_summary: 6 checks passed');
})();