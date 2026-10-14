/**
 * Issue #329 — Cross-pipeline handoff frontend tests.
 *
 * Verifies:
 * - issues.js contains _renderCrossPipelineButton function
 * - issues.js contains openCrossPipelineModal function
 * - issues.js contains crossPipelineSubmit function
 * - issues.js _renderPipelineSection handles source_issue_number annotation
 * - pipeline.js contains _renderCrossSceneBadge function
 * - I18N locale files contain cross_pipeline entries
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');

const ISSUES_JS = path.join(ROOT, 'src/api/static/js/pages/issues.js');
const PIPELINE_JS = path.join(ROOT, 'src/api/static/js/pages/pipeline.js');
const ZH_ISSUES = path.join(ROOT, 'src/api/static/locales/zh/issues.json');
const EN_ISSUES = path.join(ROOT, 'src/api/static/locales/en/issues.json');
const ZH_PIPE = path.join(ROOT, 'src/api/static/locales/zh/pipeline.json');
const EN_PIPE = path.join(ROOT, 'src/api/static/locales/en/pipeline.json');

describe('Issue #329: Cross-pipeline handoff frontend', () => {

  test('issues.js defines _renderCrossPipelineButton', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    expect(src).toContain('_renderCrossPipelineButton');
    expect(src).toContain('openCrossPipelineModal');
    expect(src).toContain('crossPipelineSubmit');
  });

  test('issues.js cross-pipeline modal uses custom DOM (not confirm())', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    // Extract the openCrossPipelineModal function body
    const idx = src.indexOf('window.openCrossPipelineModal');
    expect(idx).toBeGreaterThan(-1);
    const body = src.substring(idx, idx + 3000);
    // Should create a div overlay, not use confirm()
    expect(body).toContain('createElement');
    expect(body).toContain('crossPipelineOverlay');
    // Should NOT use confirm() within the modal
    expect(body).not.toContain('confirm(');
  });

  test('issues.js cross-pipeline submit reaches the A2A chain (Issue #337: via backend proxy)', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    const idx = src.indexOf('window.crossPipelineSubmit');
    expect(idx).toBeGreaterThan(-1);
    const body = src.substring(idx, idx + 3000);
    // Issue #337: submit now goes through the backend proxy endpoint, which reuses the
    // /api/a2A cross-pipeline chain server-side (targets/confirm handled there).
    expect(body).toContain("API.post('/issues/' + t.id + '/handoff'");
    expect(body).toContain('target_scene_id');
    // Double-confirm guard (Issue #337 anti-misclick)
    expect(body).toContain('_grapeConfirm');
  });

  test('issues.js _renderPipelineSection includes source_issue_number annotation', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    expect(src).toContain('source_issue_number');
    expect(src).toContain('continuation_from');
  });

  test('issues.js event history includes cross-scene annotation', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    expect(src).toContain('cross_scene_handoff');
    expect(src).toContain('assignee_changed');
    expect(src).toContain('crossSceneIcon');
  });

  test('pipeline.js defines _renderCrossSceneBadge', () => {
    const src = fs.readFileSync(PIPELINE_JS, 'utf8');
    expect(src).toContain('_renderCrossSceneBadge');
    expect(src).toContain('cross_scene_handoff');
  });

  test('pipeline.js uses _renderCrossSceneBadge in flow title', () => {
    const src = fs.readFileSync(PIPELINE_JS, 'utf8');
    // Both renderPipeline and selPipe should call _renderCrossSceneBadge
    const count = (src.match(/_renderCrossSceneBadge/g) || []).length;
    expect(count).toBeGreaterThanOrEqual(3); // definition + 2 call sites
  });

  test('zh issues.json contains cross_pipeline entries', () => {
    const zh = JSON.parse(fs.readFileSync(ZH_ISSUES, 'utf8'));
    expect(zh.cross_pipeline).toBeDefined();
    expect(zh.cross_pipeline.handoff_btn).toBeDefined();
    expect(zh.cross_pipeline.modal_title).toBeDefined();
    expect(zh.cross_pipeline.target_scene).toBeDefined();
    expect(zh.cross_pipeline.engine_selection).toBeDefined();
    expect(zh.cross_pipeline.continuation_from).toBeDefined();
  });

  test('en issues.json contains cross_pipeline entries', () => {
    const en = JSON.parse(fs.readFileSync(EN_ISSUES, 'utf8'));
    expect(en.cross_pipeline).toBeDefined();
    expect(en.cross_pipeline.handoff_btn).toBeDefined();
    expect(en.cross_pipeline.modal_title).toBeDefined();
    expect(en.cross_pipeline.continuation_from).toBeDefined();
  });

  test('zh/en pipeline.json contains cross_scene_handoff', () => {
    const zh = JSON.parse(fs.readFileSync(ZH_PIPE, 'utf8'));
    const en = JSON.parse(fs.readFileSync(EN_PIPE, 'utf8'));
    expect(zh.cross_scene_handoff).toBeDefined();
    expect(en.cross_scene_handoff).toBeDefined();
  });

  test('index.html bumped issues.js and pipeline.js versions', () => {
    const html = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');
    // Version moves forward over time (v109 at #329, bumped since); assert >= v109.
    const m = html.match(/issues\.js\?v=(\d+)/);
    expect(m).not.toBeNull();
    expect(parseInt(m[1], 10)).toBeGreaterThanOrEqual(109);
    expect(html).toContain('pipeline.js?v=44');
  });

  test('issues.js handoff button uses Apple design (0.5px border, rounded-lg)', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    // Locate the function definition (call sites appear earlier in the file)
    const idx = src.indexOf('function _renderCrossPipelineButton');
    const body = src.substring(idx, idx + 500);
    expect(idx).toBeGreaterThan(-1);
    expect(body).toContain('tsk-btn');
    // Check for rounded border style
    expect(body).toMatch(/border-radius/i);
  });

  test('issues.js cross-pipeline modal uses app.showToast (not alert)', () => {
    const src = fs.readFileSync(ISSUES_JS, 'utf8');
    const idx = src.indexOf('window.crossPipelineSubmit');
    const body = src.substring(idx, idx + 3000);
    expect(body).toContain('app.showToast');
    // Should not use alert() for success messages
    expect(body).not.toMatch(/alert\s*\(/);
  });
});
