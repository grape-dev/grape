// 平台列表里的 bot 摘要：只回答“这个平台有没有可用 bot”，不印裸计数。
//
// 语义来源：托管动作的取 token 链是 RepoBotToken → GlobalBotToken → UserToken
// （见 src/domains/platform/credential_resolver.py 的模块注释：bot 不在用户凭据层，
// 没有 bot 时回落用户凭据）。所以“0 条 bot”不是缺陷，而是“回落到用户 PAT”。
//
// D110：一平台一个 bot（平台行），这里的行只是能力视图摘要；status=disabled 表示无 token。

export function botSummary(platform, tr) {
  const rows = (platform && platform.bot_accounts) || []
  const active = rows.filter((b) => (b && b.status ? b.status : 'active') === 'active')
  if (!active.length) {
    return rows.length ? tr('page.bots_all_inactive') : tr('page.bots_none_short')
  }
  const def = active.find((b) => b.is_default)
  return tr('page.col_bots') + ' ' + active.length + (def ? ' · ' + tr('page.bot_default') + ':' + def.username : '')
}
