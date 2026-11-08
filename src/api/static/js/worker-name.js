/*
 * worker 显示名统一取值（Issue #701）
 *
 * 全平台 worker/设备名称展示唯一来源：runtime_credentials.name（凭据自定义名）。
 * rc.name 为空 → 统一占位符 i18n common.unnamed_worker（未命名设备）；
 * 不回退边端自报 device_name / host_name（#465 的回退口径已废弃）。
 *
 * 各展示组件（observability.js Worker 卡片/runtimes 表所属设备列、
 * runtimes.js 设备卡片、events.js 任务执行机信息等）一律调用本函数。
 *
 * engineDisplayName（Issue #1310）：引擎显示名统一「引擎名（凭据名）」。
 * - 引擎名：rt.display_name || rt.engine_name || rt.name || rt.instance_name
 *   （agent-runtimes 出口 name 已 to_display；dashboard 数据源 name=instance_name，
 *   由 engine_name 兜底；instance_name 仅作最后兼容匹配，避免显示设备派生名）
 * - 凭据名：rt.worker_name（=runtime_credentials.name），空 → 与 workerDisplayName
 *   同源占位（i18n common.unnamed_worker）；本机直连 cid=0 由后端置 worker_name=host_name
 */
(function (global) {
  function unnamedWorker() {
    if (typeof I18N !== 'undefined' && I18N && typeof I18N.t === 'function') {
      return I18N.t('common.unnamed_worker');
    }
    return '未命名设备';
  }
  function workerDisplayName(wk) {
    var n = (wk && wk.name) || '';
    if (String(n).trim()) return String(n).trim();
    return unnamedWorker();
  }
  function engineDisplayName(rt) {
    var en = (rt && (rt.display_name || rt.engine_name || rt.name || rt.instance_name)) || '';
    en = String(en).trim();
    var wn = (rt && rt.worker_name) || '';
    wn = String(wn).trim();
    var wd = wn || unnamedWorker();
    return en ? en + '（' + wd + '）' : wd;
  }
  global.workerDisplayName = workerDisplayName;
  global.engineDisplayName = engineDisplayName;
  if (typeof window !== 'undefined') {
    window.workerDisplayName = workerDisplayName;
    window.engineDisplayName = engineDisplayName;
  }
})(typeof window !== 'undefined' ? window : globalThis);
