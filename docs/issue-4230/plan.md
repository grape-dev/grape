# 方案: Issue #4230 — 纯文本摘要（方案 B）

## 决策

卡片采用纯文本摘要，不做 Markdown 渲染：无 XSS 面、改动仅限卡片、详情页零改动。

## 实现

1. `lib/plainTextSummary.js`
   - 移除代码围栏（含内容）与行内代码反引号；
   - 剥离标题/强调/删除线/列表/引用/水平线标记；
   - 链接、图片保留可见文字，简单 HTML 标签剥离并解码常见实体；
   - 按空行切段，取第一个非空段落，逐行空白归一；
   - 超长时优先在句末标点截断，退化为空白边界，最后按字符上限兜底并追加 `…`。
   - 导出 `plainTextSummary(input, maxLength)`；`maxLength=0` 返回完整首段，用于 title 悬停。
2. `SceneCard.vue`
   - `descText` 使用默认上限摘要；为空时回退 `tf('no_description')`。
   - `descFull` 使用不截断摘要作为 `title`。
   - `DEFAULTS` 增加 `no_description`，沿用既有 `DEFAULTS` + `tf()` 模式。
3. 词条：`skills.json`（zh/en）新增 `no_description`。

## 缓存与守卫

改动 `SceneCard.vue` 与新 lib 会影响 `scene-market.js` chunk 源、`all-views.css` 源及语言包指纹，需同步抬版本并更新：

- `src/api/frontend/shell/index.html`：`scene-market.js?v`、`all-views.css?v`、`__I18N_CACHE_VER__`。
- `tests/api/test_frontend_bundle_source_ratchet.py`（FROZEN/SHIPPED）。
- `tests/api/test_frontend_cache_version_ratchet.py`（FROZEN、ALL_VIEWS）。
- `tests/api/test_i18n_locale_cache_ratchet.py`（CACHE_VER、LOCALE_FINGERPRINT）。

## 测试

- 新增 `tests/js/test_issue_4230_plain_text_summary.js`：标题/列表/强调/引用/链接/图片/代码/HTML/空值/超长/组件接线。
- 运行 `node tests/js/test_issue_4230_plain_text_summary.js` 与相关前端守卫 pytest。