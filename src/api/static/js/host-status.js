/*
 * 托管状态与缺失项文案统一映射（Issue #2126）
 *
 * dashboard 横幅/修复结果与 repos 卡片/托管列表共用，避免各页面重复维护
 * bot/webhook/token/webhook_url 中文映射。本文件须在 dashboard.js / repos.js 之前加载。
 */
(function (global) {
  function hostMissingLabel(m, compact) {
    if (m === 'bot') return compact ? 'grape_bot 未加入' : 'grape_bot 未加入项目';
    if (m === 'webhook') return 'webhook 未创建';
    if (m === 'webhook_url') return '服务公网地址（webhook_url）未配置';
    if (m === 'token') return '用户 Token 缺失或已过期';
    return m;
  }
  function hostStatusLabel(hs, compact) {
    var labels = {
      bot_missing: compact ? 'grape_bot 未加入' : '该项目 grape_bot 未成功加入项目',
      webhook_missing: compact ? 'webhook 未创建' : '项目 webhook 缺失，Grape 无法接收仓库事件',
      partial: compact ? '托管部分完成' : '该项目托管流程未完成，Grape 部分功能不可用'
    };
    if (labels[hs]) return labels[hs];
    return hs ? '该项目托管流程未完成，Grape 部分功能不可用' : '';
  }
  global._hostMissingLabel = hostMissingLabel;
  global._hostStatusLabel = hostStatusLabel;
})(typeof window !== 'undefined' ? window : globalThis);