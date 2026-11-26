# Spec: Issue #4445 — worker 节点 /tmp（tmpfs）残留回收、水位可观测与派发预检

## Objective

给节点 `/tmp`（tmpfs，占用即钉住物理内存）补齐「残留回收 → 容量可观测 → 派发前预检」闭环：
回收 agent/技能自建的任务派生残留，暴露 tmpfs 水位与清理量指标，并在目标节点
`/tmp` 可用 <10% 时拒绝派发。

## 背景与根因

- 线上 24h 内 3 台节点 `/tmp` 可用字节见底（issue-001-0001/0002、coding-001-0002），
  同批节点 24h OOM 最高 132 次（Issue #4445 正文实测）。
- `cleanup.py::_sweep_tmp_residuals` 只清固定前缀；`_sweep_opencode_tmpdirs` 要求
  `.grape-session-tmpdir` 标记。`qa-* / pr-* / verify-* / base<digits> / rev<digits> /
  deploy-ws / venv-* / pkg-* / head<digits> / wt-*` 等任务派生目录无标记 → 永不过期。
- 无 tmpfs 水位告警规则、无派发前容量门，写满后只表现为工具层拒绝。
- 远端 worker 最小依赖不含 `prometheus_client`（Issue #2923），`src.domains.runtime.engine.agent`
  进程没有 `/metrics` 端点 → 只靠 worker 侧 gauge 时节点水位告警永不触发。

## 范围

In scope：
1. `cleanup.py` 新增 `_sweep_tmp_task_residuals()`：前缀白名单 + mtime TTL（默认 2h）
   清理任务派生残留，活跃任务（含 `qa-<task_id>` 归属形态）/当前 `TASK_TMPDIR`/符号链接豁免；
   活跃任务探测失败时整轮跳过（fail-closed）。
2. 清理累计量经心跳落 `runtime_credentials`，控制面 `/metrics` 按 host 导出
   `grape_node_tmp_janitor_removed_items_total` / `_bytes_total` 与 `grape_node_tmp_used_ratio`；
   控制面本机另导出 `grape_tmpfs_used_ratio{mount} / grape_tmp_janitor_*`。
3. `deploy/alerts.yml`：节点通路（`grape_node_tmp_used_ratio`）与控制面本机通路
   （`grape_tmpfs_used_ratio{mount="/tmp"}`）各 >80% warning / >90% critical。
4. 派发预检：目标节点 `/tmp` 使用率 ≥90% 时被过滤；`DISPATCH_TMP_HEADROOM_GUARD=off` 可关。

Out of scope：换节点策略、临时根迁出 tmpfs（与 #4374 共用/另立单）、全量 `*.log` 通配清理。

## 验收标准

- [ ] 无标记、超 TTL 的 `qa-* / pr<digits>-* / verify<digits> / base<digits> / rev<digits> /
      deploy-ws / venv<digits> / pkg-* / head<digits> / wt-*` 被清理，日志出现
      `tmp janitor: removed N items, freed X bytes, skipped M(active/protected)`；
      `dry` 模式日志为 `tmp janitor(dry-run): would remove ...` 且累计量不变。
- [ ] 活跃任务目录（`qa-<task_id>` / `pr<task_id>-head` 等）、当前 `TASK_TMPDIR`、符号链接、
      白名单外第三方文件（含 `baseline*`/`review*`/`headless*`）不被删除（单测）。
- [ ] 活跃任务探测失败 → 本轮不删（fail-closed，单测）。
- [ ] `grape_node_tmp_used_ratio` / `grape_node_tmp_janitor_*` 在控制面 `/metrics` 可按 host 抓到；
      `tmp_janitor_removed_items_total` / `_bytes_total` 与 `grape_tmpfs_used_ratio` 在配置
      `METRICS_ADDR` 且装了 `prometheus_client` 的进程可抓到；缺 `prometheus_client` 时启动日志
      有显式 warning；新增迁移 post_check 通过。
- [ ] `promtool check rules deploy/alerts.yml` 通过。
- [ ] 目标节点 ≥90% 时派发被拒（`_filter_by_tmp_headroom`），<90% 不受影响。