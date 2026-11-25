/*
 * 托管状态与缺失项文案统一映射（Issue #2126）
 *
 * dashboard 横幅/修复结果与 repos 卡片/托管列表共用，避免各页面重复维护
 * bot/webhook/token/webhook_url 中文映射。本文件须在 i18n.js 之后、
 * dashboard.js / repos.js 之前加载。
 * 文案统一走语言包 common.host_*（zh/en 双写），fallback 保留中文缺省值
 * 以覆盖语言包未加载完成的首次绘制（I18N.t 缺 key 时返回 key 本身）。
 */
(function (global) {
  function t(key, fallback) {
    try {
      if (typeof I18N !== 'undefined' && I18N && I18N.t) return I18N.t(key, null, fallback);
    } catch (e) {}
    return fallback;
  }
  function hostMissingLabel(m, compact) {
    if (m === 'bot') return t(compact ? 'common.host_missing_bot_compact' : 'common.host_missing_bot', compact ? 'grape_bot 未加入' : 'grape_bot 未加入项目');
    if (m === 'webhook') return t('common.host_missing_webhook', 'webhook 未创建');
    if (m === 'webhook_url') return t('common.host_missing_webhook_url', '服务公网地址（webhook_url）未配置');
    if (m === 'token') return t('common.host_missing_token', '用户 Token 缺失或已过期');
    return m;
  }
  function hostStatusLabel(hs, compact) {
    var labels = {
      bot_missing: compact
        ? t('common.host_status_bot_missing_compact', 'grape_bot 未加入')
        : t('common.host_status_bot_missing', '该项目 grape_bot 未成功加入项目'),
      webhook_missing: compact
        ? t('common.host_status_webhook_missing_compact', 'webhook 未创建')
        : t('common.host_status_webhook_missing', '项目 webhook 缺失，Grape 无法接收仓库事件'),
      // Issue #4241：地址漂移三态（落盘注册地址与当前权威地址不一致）
      webhook_stale: compact
        ? t('common.host_status_webhook_stale_compact', 'webhook 地址漂移')
        : t('common.host_status_webhook_stale', '项目 webhook 地址已漂移（与实际注册地址不一致），Grape 无法接收仓库事件，需修复托管'),
      partial: compact
        ? t('common.host_status_partial_compact', '托管部分完成')
        : t('common.host_status_partial', '该项目托管流程未完成，Grape 部分功能不可用')
    };
    if (labels[hs]) return labels[hs];
    return hs ? t('common.host_status_fallback', '该项目托管流程未完成，Grape 部分功能不可用') : '';
  }
  global._hostMissingLabel = hostMissingLabel;
  global._hostStatusLabel = hostStatusLabel;
})(typeof window !== 'undefined' ? window : globalThis);
