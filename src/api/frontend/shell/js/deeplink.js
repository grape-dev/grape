/*
 * 深链解析（需求详情索引）：/app/<owner>/<repo>/#tasks/<N>
 *
 * 选型说明：前端是 hash 路由（#view/param），hash 里没有仓库维度 → `#tasks/12`
 * 只能落在「当前选中仓库」上，换个仓库打开同一条链接会解锁到别的单。
 * 这里把仓库放进**路径**（服务端 SPA catch-all 对任意非 /api、/static 路径都回
 * index.html），hash 继续只承载视图与参数：
 *
 *     https://<host>/app/<owner>/<repo>/#tasks/<N>
 *     https://<host>/app/<owner>/<repo>/#tasks/<N>   ← 带不带结尾斜杠都认
 *
 * 只做纯字符串解析，不碰 DOM / location / 网络，便于 node 单测。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.GrapeDeepLink = api;
})(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  function _decode(seg) {
    try { return decodeURIComponent(seg); } catch (e) { return ''; }
  }

  /**
   * 解析 `/app/<owner>/<repo>[/]`。
   * 返回 {owner, repo, full} 或 null（不是深链路径 / 段为空 / 段非法）。
   * 多余层级（/app/o/r/abc）视为普通 SPA 路径，不当深链，避免误判。
   */
  function parseAppPath(pathname) {
    if (typeof pathname !== 'string' || !pathname) return null;
    var p = pathname.split('?')[0].split('#')[0];
    // 英文站走 /en 前缀（spa.py 有独立 /en 路由，i18n.js 用 pathname 判语言）——
    // /en/app/<owner>/<repo>/ 同样是合法深链，prefix 原样回带给 buildAppPath。
    var m = /^(\/en)?\/app\/([^/]+)\/([^/]+)\/?$/.exec(p);
    if (!m) return null;
    var owner = _decode(m[2]).trim();
    var repo = _decode(m[3]).trim();
    if (!owner || !repo) return null;
    if (owner === '.' || owner === '..' || repo === '.' || repo === '..') return null;
    if (owner.indexOf('/') !== -1 || repo.indexOf('/') !== -1) return null;
    return { owner: owner, repo: repo, full: owner + '/' + repo,
             prefix: m[1] ? '/en' : '' };
  }

  /** 反向拼路径：buildAppPath('octo','demo') → '/app/octo/demo/'（host 与 hash 由调用方拼）。
   *  prefix='/en' 时拼成 /en/app/octo/demo/（保持英文站的语言前缀）。 */
  function buildAppPath(owner, repo, prefix) {
    if (!owner || !repo) return '/';
    var pfx = prefix === '/en' ? '/en' : '';
    return pfx + '/app/' + encodeURIComponent(owner) + '/' + encodeURIComponent(repo) + '/';
  }

  /** 仓库全名比较：GitCode/GitHub 的 owner/repo 不区分大小写。 */
  function sameRepo(a, b) {
    return String(a || '').toLowerCase() === String(b || '').toLowerCase();
  }

  return { parseAppPath: parseAppPath, buildAppPath: buildAppPath, sameRepo: sameRepo };
});