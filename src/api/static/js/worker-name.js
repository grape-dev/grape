/*
 * worker 显示名统一取值（Issue #701）
 *
 * 全平台 worker/设备名称展示唯一来源：runtime_credentials.name（凭据自定义名）。
 * rc.name 为空 → 统一占位符 i18n common.unnamed_worker（未命名设备）；
 * 不回退边端自报 device_name / host_name（#465 的回退口径已废弃）。
 *
 * 各展示组件（observability.js Worker 卡片/runtimes 表所属设备列、
 * runtimes.js 设备卡片、events.js 任务执行机信息等）一律调用本函数。
 */
(function (global) {
  function workerDisplayName(wk) {
    var n = (wk && wk.name) || '';
    if (String(n).trim()) return String(n).trim();
    if (typeof I18N !== 'undefined' && I18N && typeof I18N.t === 'function') {
      return I18N.t('common.unnamed_worker');
    }
    return '未命名设备';
  }
  global.workerDisplayName = workerDisplayName;
  if (typeof window !== 'undefined') window.workerDisplayName = workerDisplayName;
})(typeof window !== 'undefined' ? window : globalThis);
