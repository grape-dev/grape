# Plan: Issue #461 — 实现方案

## 后端
1. `src/api/db_setup.py` `_migrate_tables`：新增 `devops_pipeline_scene.deploy_url VARCHAR(512) DEFAULT ''` 幂等补列（与 managed_repos.deploy_url 迁移同模式）。
2. `src/api/routes/scenes.py`：
   - `GET /api/scenes/{scene_id}/deploy-url?repo=owner/repo` → `{deploy_url}`（场景不存在/未激活 → 404；缺 repo → 400）
   - `PUT /api/scenes/{scene_id}/deploy-url?repo=owner/repo`，body `{deploy_url}` → 复用 repos.py `_validate_deploy_url`（http(s) 前缀 + ≤512 + 空串清除）与 `check_repo_access`（越权 403），UPDATE 仅 `is_active=1` 行，rowcount=0 → 404。
   - 均为 `require_no_session_token` 鉴权（与 advanced-options 同口径，进 OpenAPI 契约）。

## 前端（dashboard.js）
3. `_renderLocalSceneDetailPage` 头部：复制场景按钮**前**插入 `<span id="sceneDeployGlobe">` 容器（`align-self:flex-end;margin-top:4px` 与按钮对齐）。
4. 新增 `_loadSceneDeployUrl(sceneId, owner, name)`（进入详情页加载）与 `_renderSceneDeployGlobe()` 三态渲染：
   - 未登记：灰 🌐（opacity .7，点击/右击 → 编辑）
   - 已登记：绿 🌐（href 打开新窗 + title 显示 URL + 右击/长按 600ms → 编辑；XSS 用 `_skillEsc` 转义 href/title/value）
   - 编辑：输入框 + 保存/取消（保存前校验 http(s) 前缀，与 issues.js `_isValidDeployUrl` 同规则）
   - 函数放在渲染函数外（与 `_loadAdvancedOptionsInline` 同级），通过 `window._currentSceneDeployCtx` 持上下文，避免闭包膨胀。
5. `index.html`：dashboard.js 缓存版本 v=98 → v=99。

## i18n
6. `locales/{zh,en}/dashboard.json`：`deploy_url_register`（登记访问地址（点击或右击编辑））、`deploy_url_edit_title`（登记场景访问 URL）、`deploy_url_saved`（访问地址已保存）、`deploy_url_save_failed`、`deploy_url_invalid`（URL 必须以 http:// 或 https:// 开头）。

## 文档与契约
7. `help/api/06-scenes.md`：新增 GET/PUT 两个端点块（test_api_docs_sync 强制）。
8. `tests/test_openapi.py` `EXPECTED_PUBLIC` 登记 `scenes_deploy_url_get`/`scenes_deploy_url_set`（test_public_ops_have_operation_id_summary_security 全集断言强制）。

## 测试
9. `tests/test_issue_461_scene_deploy_url.py`（TestClient）：正常 set/get、清空、XSS/前缀/长度拒绝、跨用户 403、场景不存在 404、缺 repo 400、场景级隔离（两场景互不影响、需求中心 managed_repos.deploy_url 不受影响）。
10. `tests/js/test_issue_461_scene_deploy_url.js`：前端源码断言（按钮顺序、三态渲染、_skillEsc 转义、校验、i18n key、缓存版本 bump、需求中心 issues.js 零改动）。

## 风险
- 场景级与需求级存储冲突 → 独立列 + 独立端点，互不读写
- 布局挤占 → 图标与按钮同行 flex 布局，尺寸同需求中心
- 旧库无列 → _ensure_column 幂等补列，启动自动迁移
