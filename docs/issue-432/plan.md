# Plan: Issue #432

## Changes

### 1. runtimes.js — extract shared filter function
- New function `_filterEngines(runtimes)` extracted from `renderEngines` filter logic (lines 409-429).
- `renderEngines` calls `_filterEngines(runtimes)` → sorts result → renders.
- `_summaryStatsBar` calls `_filterEngines(data.runtimes)` for engines tab to get filtered list.

### 2. runtimes.js — i18n-ize _summaryStatsBar
- `共 ${total} 台` → `I18N.t('runtimes.stats.total', {count: total})`
- `在线 ${counts.online}` → `I18N.t('runtimes.health.online') + ' ' + counts.online`
- Same for recently_lost, offline, about_to_gc.

### 3. Locale JSONs — add stats.total
- zh: `"stats": { "total": "共 {count} 台" }`
- en: `"stats": { "total": "Total {count}" }`

### 4. Test — static assertion test
- Python test reading runtimes.js + locale JSONs, asserting:
  - No hardcoded Chinese in _summaryStatsBar
  - stats.total key exists in both zh/en
  - {count} interpolation placeholder present
  - _filterEngines function exists and is called by both renderEngines and _summaryStatsBar

## Risk
- Low: filter logic is pure (no side effects), extraction is mechanical.
- Workers tab unaffected (uses devices directly, no frontend filter).
