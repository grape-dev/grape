/*
 * Issue #3508 — 流程配置页（#tasks）引擎健康双指标展示与接口错误兜底
 *
 * 覆盖（对应 Spec 验收）：
 *   1. IssuesView（#tasks 创建弹窗）：按需调用 /scenes/{id}/engine-status
 *      （旧实现读 with-pipeline 的 all_offline，该字段 Issue #501 已迁出，
 *      告警永远不显示——用户反馈「不好查看」的根因）；
 *      区分「物理不可达」（offline_nodes）与「健康分不足」（low_score_nodes）；
 *      接口失败展示可读错误 + 重试（retrySceneWarn），不白屏。
 *   2. DashboardView（场景流程配置引擎区域）：engine-status 结构化字段
 *      {health_score, reachability, reason} 双指标渲染（engineHealthChipHtml）；
 *      接口失败可重试（retrySceneEngineHealth）；引擎选择弹窗展示健康标签。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const ISSUES = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/IssuesView.vue'),
  'utf8'
);
const DASH = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/DashboardView.vue'),
  'utf8'
);

// 文案已 key 化（架构复审 D1）：源文件只保留 t()/td() 调用，中文在语言包里
const ZH_ISSUES = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/issues.json'), 'utf8')
);
const ZH_DASH = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/dashboard.json'), 'utf8')
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

console.log('Issue #3508: #tasks 流程配置页引擎健康双指标与错误兜底');

check('IssuesView 按需调用 /scenes/{id}/engine-status（不再依赖已失效的 all_offline）', () => {
  assert.ok(ISSUES.includes('/engine-status'), '应调用 engine-status 端点');
  assert.ok(
    !ISSUES.includes('s.all_offline'),
    '不应再读 with-pipeline 的 all_offline（Issue #501 已迁出，恒为 undefined）'
  );
});

check('IssuesView 告警区分「物理不可达」与「健康分不足」双指标', () => {
  assert.ok(ISSUES.includes('engine_unreachable_nodes'), '不可达文案应走 td() 键');
  assert.ok(ISSUES.includes('engine_low_score_nodes'), '低分文案应走 td() 键');
  assert.ok(
    (ZH_ISSUES.engine_unreachable_nodes || '').includes('物理不可达'),
    'zh 语言包应有物理不可达文案'
  );
  assert.ok(
    (ZH_ISSUES.engine_low_score_nodes || '').includes('健康分不足'),
    'zh 语言包应有健康分不足文案'
  );
  assert.ok(ISSUES.includes('low_score_nodes'), '应消费 low_score_nodes 字段');
  assert.ok(ISSUES.includes('offline_nodes'), '应消费 offline_nodes 字段');
});

check('IssuesView 异常原因可读（engines[].reason 透出）', () => {
  assert.ok(ISSUES.includes('.reason'), '应展示 engines[].reason');
});

check('IssuesView 接口失败兜底：可读错误 + 重试，不白屏', () => {
  assert.ok(ISSUES.includes('sceneHealth.error'), '应有 sceneHealth.error 错误态');
  assert.ok(ISSUES.includes('retrySceneWarn'), '应有重试入口 retrySceneWarn');
});

check('DashboardView 消费 engine-status 结构化字段 {health_score, reachability, reason}', () => {
  assert.ok(DASH.includes('loadSceneEngineHealth'), '应有 loadSceneEngineHealth');
  assert.ok(DASH.includes('reachability'), '应消费 reachability 字段');
  assert.ok(DASH.includes('health_score'), '应消费 health_score 字段');
  assert.ok(DASH.includes('engineHealthChipHtml'), '应有双指标渲染函数');
});

check('DashboardView 双指标文案：物理不可达 / 健康分不足分列', () => {
  assert.ok(DASH.includes("t('engine_unreachable')"), '不可达文案应走 t() 键');
  assert.ok(DASH.includes("t('engine_low_score')"), '低分文案应走 t() 键');
  assert.ok(
    (ZH_DASH.engine_unreachable || '').includes('物理不可达'),
    'zh 语言包应有物理不可达文案'
  );
  assert.ok(
    (ZH_DASH.engine_low_score || '').includes('健康分不足'),
    'zh 语言包应有健康分不足文案'
  );
});

check('DashboardView 接口失败兜底：可重试，不白屏', () => {
  assert.ok(DASH.includes('retrySceneEngineHealth'), '应有重试入口');
  assert.ok(DASH.includes('dbv-enghealth-err'), '应有错误兜底样式块');
});

check('DashboardView 引擎选择弹窗展示健康标签（含低分/不可达双态）', () => {
  assert.ok(DASH.includes('engineModalHealthChip'), '弹窗应有健康标签函数');
  const i = DASH.indexOf('function engineModalHealthChip');
  const seg = DASH.slice(i, i + 800);
  assert.ok(
    seg.includes("t('engine_unreachable')") && seg.includes("t('engine_low_score')"),
    '弹窗健康标签应走 t() 键'
  );
});

check('DashboardView 健康分类收敛为单一函数，避免重复判定', () => {
  assert.ok(DASH.includes('function classifyEngineHealth'));
  assert.ok(DASH.includes('engineHealthMap'), '应有 computed 健康索引，避免逐行重建');
});

check('DashboardView 换绑候选引擎（未绑定）也有健康标签', () => {
  assert.ok(DASH.includes('/runtime/admin/machine-health'), '候选健康应拉 machine-health');
  assert.ok(DASH.includes('candidateEngineHealth'), '应有候选引擎健康回退函数');
});

check('DashboardView 节点健康匹配与后端 node_agent_role 同口径（id 优先）', () => {
  const i = DASH.indexOf('function engineHealthEntriesFor');
  const seg = DASH.slice(i, i + 260);
  assert.ok(seg.includes('node.id || node.tag'), '应按 id 优先匹配 engines[].node');
  assert.ok(!/node\.tag\s*\|\|\s*node\.id/.test(seg), '不应仍 tag 优先');
});

console.log('\n全部通过：' + passed + ' 项检查');
