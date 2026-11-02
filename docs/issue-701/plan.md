# Plan — Issue #701

## 展示位置清单（排查结果 → 改造）

### 前端
| # | 位置 | 现状 | 改造 |
|---|------|------|------|
| F1 | `js/worker-name.js`（新增）+ index.html 引入 | 无统一函数 | `workerDisplayName(wk)`：`(wk.name||'').trim() \|\| I18N.t('common.unnamed_worker')`（无 I18N 时兜底中文占位） |
| F2 | observability.js `_workerDisplayName`（#465） | `wk.name \|\| wk.device_name` | 委托统一函数，去除回退 |
| F3 | observability.js `_workerCardHTML` 标题（#465） | `wk.name \|\| wk.device_name \|\| worker_id` | 统一函数（空名→占位） |
| F4 | observability.js:162 资源曲线引擎下拉 | `r.device_name \|\| r.name \|\| '#id'` | `r.name` 优先 + 占位 |
| F5 | observability.js:1232/1799 runtimes 表「所属设备」列 | `rti.device_name \|\| '-'` | 空显示占位（后端已下发 rc.name） |
| F6 | runtimes.js:469 `_rebuildRtEngineIndex` worker_name | `d.name \|\| d.device_name` | 统一函数 |
| F7 | runtimes.js:668 Worker 设备卡片标题 | `d.name \|\| d.device_name \|\| '?'` | 统一函数 |
| F8 | events.js:1339 任务日志执行机信息 | `info.device_name`（后端 rc.device_name） | 后端改下发 rc.name；前端空名时显示占位 |

不改（功能字段非展示名）：observability.js 设备筛选/排序键（消费后端已对齐的 device_name 字段）、
wid 派生键、runtimes.js 引擎实例名/别名展示（引擎标识符）、autopilot.js host_name（本机主控标注）。

### 后端（展示名下发口径 → rc.name）
| # | 位置 | 现状 | 改造 |
|---|------|------|------|
| B1 | dashboard.py:560 runtimes 行 device_name（#465） | `rc_name or rdev or ''` | `rc_name or ''`（远端；本机 cid=0 沿用 host_name） |
| B2 | events.py:219/261/268 + engine_device | `COALESCE(rc.device_name,'')` | `COALESCE(rc.name,'')` |
| B3 | task_logs.py /{id}/info device_name | `rc.device_name` | `rc.name`（响应字段名不变） |
| B4 | observability.py:230 日志 source | `device_name or name` | `name or device_name or worker-{id}`（name 优先） |
| B5 | runtime.py:357 worker 日志 worker_name | `device_name or name` | name 优先 |

不改（已正确或属标识符）：settings.py `worker_name=cred["name"]`（已正确）、
`instance_name()` 派生（引擎实例唯一名标识符）、chat.py/agents.py 实例名派生、
本机直连（cid=0）host_name 标注（无凭据，非 worker 名称）。

## 统一取值函数
```js
window.workerDisplayName = function (wk) {
  var n = (wk && wk.name) || '';
  if (String(n).trim()) return String(n).trim();
  if (typeof I18N !== 'undefined' && I18N.t) return I18N.t('common.unnamed_worker');
  return '未命名设备';
};
```
i18n：`locales/{zh,en}/common.json` 新增 `unnamed_worker`（未命名设备 / Unnamed device）。

## 测试 seam
- 前端：node 单测直接 eval `worker-name.js`（I18N stub 双语）；
  更新 `tests/js/test_issue_465_worker_card_name.js`（去回退 → 占位）。
- 后端：`tests/test_issue_465_runtime_device_name.py` 回退用例改为「空名下发空值」；
  新增 `tests/test_issue_701_worker_name_display.py`（dashboard runtimes 空名下发空串、
  rc.name 正常下发；events/task_logs 口径）。
