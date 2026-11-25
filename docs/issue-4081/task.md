# Issue #4081 任务拆解

| # | 任务 | 产物 | 状态 |
|---|---|---|---|
| T1 | 指标注册：`grape_plugin_missing_report_total{result}`、`grape_plugin_agent_unimplemented_total{method}` + 名字常量 | `src/domains/insight/observability/__init__.py` | done |
| T2 | `report-missing` 结果埋点（submitted/degraded）+ 降级 WARNING | `src/domains/mgmt/routes/plugins_agent.py` | done |
| T3 | `/api/plugins/agent/*` 末位兜底：未登记路径/方法 → 501 `NOT_IMPLEMENTED` + 计数 | 同上 | done |
| T4 | `api_error()` 支持 `error_code`（可选，向后兼容） | `src/api/errors.py` | done |
| T5 | 能力位 `plugin_agent_capabilities(app)`（登记清单 × 注册面，30s 缓存，fail-closed） | `src/api/openapi.py` | done |
| T6 | 能力位出口 `GET /api/plugins/agent/capabilities`（`include_in_schema=False`；nginx 把 `/api/ping` 指向 worker，不能放那里） | `src/domains/mgmt/routes/plugins_agent.py` | done |
| T7 | `deploy/alerts.yml` 两条告警规则 | `deploy/alerts.yml` | done |
| T8 | 回归用例（6 组/21 例）：指标、501 语义、能力位、告警表达式、错误码 | `tests/mgmt/test_issue_4081_plugin_agent_contract.py` | done |
| T9 | 门禁：`make lint`、`python3 scripts/check-arch.py`、相邻套件（1090/1089/openapi/contracts/2085/3963/deploy） | 全绿（1 例预存在失败，已确认基线同样失败） | done |
| T10 | 自审查 + pre-pr 增量检查（黑名单 + 敏感扫描 + commit 英文） | — | done |

## 验证命令

```bash
python3 -m pytest tests/mgmt/test_issue_4081_plugin_agent_contract.py -q
python3 -m pytest tests/api/test_ping_endpoint.py tests/api/test_openapi.py \
  tests/contracts/test_contract_required_operations_2081.py \
  tests/mgmt/test_issue_1090_plugin_capability.py tests/mgmt/test_issue_1089_agent_plugins.py \
  tests/test_issue_2085_contract_guard_alert.py -q
python3 scripts/check-arch.py
make lint
```

## 未包含（后续跟进）

- 生产 mgmt（127.0.0.1:8081）构建版本核对与发布：sre 动作，发布后按 `/api/ping` 的
  `plugin_agent.available=true` + 两端点 200 验收。
- 调用侧（runtime 插件代理客户端，不在本仓）：遇 `404/405/501` 记一次结构化 WARNING、
  同任务内不重试、只上报一次；本单已交付其依赖的能力位与错误码。