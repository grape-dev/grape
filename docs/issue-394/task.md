# Task Breakdown: runtime 引擎重置健康分（Issue #394）

## Task 1: health_scorer.reset_health 核心逻辑
**Description:** 在 `src/runtime/health_scorer.py` 新增公共函数 `reset_health(runtime_id)`：写入 `agent_runtimes.health_reset_at=now(UTC ISO)` → `invalidate_cache(runtime_id)` → `get_health_score(runtime_id, force_refresh=True)` 重算 → 返回健康分 dict。同时补齐 `_effective_window_start` 的重置语义测试（写入口依赖该语义）。

**Acceptance criteria:**
- [ ] `reset_health` 写入 `health_reset_at` 并返回新健康分（mock DB 断言 UPDATE 调用）
- [ ] 重置后强制重算（force_refresh 路径生效）
- [ ] `_effective_window_start` 四类场景测试：无重置/重置晚于窗口/重置早于窗口/非法时间戳

**Verification:**
- [ ] `python3 -m pytest tests/test_health_scorer.py -q` 通过

**Dependencies:** None

**Files likely touched:**
- `src/runtime/health_scorer.py`
- `tests/test_health_scorer.py`

**Estimated scope:** Small (2 files)

## Task 2: settings.py 管理接口
**Description:** 新增 `POST /api/settings/agent-runtimes/{rid}/health-reset`。权限：调用 `get_agent_runtimes(user, ...)` 定位引擎（不可见 → 404）；仅 `scope in (user, repo)` 允许，`global` → 403；调用 `health_scorer.reset_health`；`log.info` 审计（operator/runtime/重置点/新健康分）。

**Acceptance criteria:**
- [ ] 请求头带组织成员身份，repo/user scope 引擎重置成功返回 `{runtime_id, health_reset_at, health_score, ...}`
- [ ] 不可见/不存在引擎 → 404；global scope → 403
- [ ] 审计日志含 operator/rid/health_reset_at

**Verification:**
- [ ] `python3 -m pytest tests/test_settings_health_reset.py -q` 通过

**Dependencies:** Task 1

**Files likely touched:**
- `src/api/routes/settings.py`

**Estimated scope:** Small (1 file + tests)

## Task 3: 接口测试
**Description:** 新建 `tests/test_settings_health_reset.py`，覆盖权限与行为：401（未认证）、404（不可见）、403（global）、200（user/repo 成功重置 + health_score 返回）。

**Acceptance criteria:**
- [ ] 上述四类场景用例存在且通过
- [ ] 不依赖真实外部服务（用 DB fixture + patch）

**Verification:**
- [ ] `python3 -m pytest tests/test_settings_health_reset.py -q` 通过

**Dependencies:** Task 2

**Files likely touched:**
- `tests/test_settings_health_reset.py`

**Estimated scope:** Small (1 file)

## Task 4: 前端入口
**Description:** `runtimes.js` 引擎详情弹窗 `_renderEngineDetail` 健康分行旁新增「重置健康分」按钮；点击 → `confirm` 弹窗 → `API.post('/settings/agent-runtimes/{rid}/health-reset')` → 成功 toast + 关闭详情刷新列表；失败 toast。`locales/{zh,en}/runtimes.json` 补 `action.reset_health`/`health.reset_confirm`/`health.reset_success`/`health.reset_failed`/`health.reset_disabled_global` 文案。

**Acceptance criteria:**
- [ ] 详情弹窗出现「重置健康分」按钮（user/repo 引擎）
- [ ] 点击确认后调用接口，成功刷新健康分展示
- [ ] zh/en 文案齐全

**Verification:**
- [ ] 手工/静态检查按钮渲染与事件函数存在
- [ ] i18n JSON 语法合法（`python3 -m json.tool`）

**Dependencies:** Task 2

**Files likely touched:**
- `src/api/static/js/pages/runtimes.js`
- `src/api/static/locales/zh/runtimes.json`
- `src/api/static/locales/en/runtimes.json`

**Estimated scope:** Small-Medium (3 files)

## Checkpoint: 完成
- [ ] `python3 -m pytest tests/test_health_scorer.py tests/test_settings_health_reset.py -q` 全绿
- [ ] 既有健康分调度测试无回归
- [ ] 代码自审查通过（创建 PR 前）