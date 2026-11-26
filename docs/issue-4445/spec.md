# Spec: Issue #4445 — worker 节点 /tmp（tmpfs）残留回收、水位可观测与派发预检

## Objective

给节点 `/tmp`（tmpfs，占用即钉住物理内存）补齐「残留回收 → 容量可观测 → 派发前预检」闭环：
回收 agent/技能自建的任务派生残留，暴露 tmpfs 水位与清理量指标，并在目标节点
`/tmp` 可用 <10% 时拒绝派发。

## 背景与根因

- 线上 24h 内 3 台节点 `/tmp` 可用字节见底（issue-001-0001/0002、coding-001-0002），
  同批节点 24h OOM 最高 132 次（Issue #4445 正文实测）。
- `cleanup.py::_sweep_tmp_residuals` 只清固定前缀；`_sweep_opencode_tmpdirs` 要求
  `.grape-session-tmpdir` 标记。`qa* / pr* / verify* / base* / rev* / deploy-ws /
  venv* / pkg-* / *_head / wt-*` 等任务派生目录无标记 → 永不过期。
- 无 tmpfs 水位告警规则、无派发前容量门，写满后只表现为工具层拒绝。

## 范围

In scope：
1. `cleanup.py` 新增 `_sweep_tmp_task_residuals()`：前缀白名单 + mtime TTL（默认 2h）
   清理任务派生残留，活跃任务/当前 `TASK_TMPDIR`/符号链接豁免。
2. 清理累计量经心跳落 `runtime_credentials`，并导出
   `grape_tmp_janitor_removed_items_total` / `_bytes_total`。
3. `grape_tmpfs_used_ratio{mount="/tmp"}` 指标 + `deploy/alerts.yml` >80% warning /
   >90% critical。
4. 派发预检：目标节点 `/tmp` 使用率 ≥90% 时被过滤；`DISPATCH_TMP_HEADROOM_GUARD=off`
   可关。

Out of scope：换节点策略、临时根迁出 tmpfs（与 #4374 共用/另立单）、全量 `*.log` 通配清理。

## 验收标准

- [ ] 无标记、超 TTL 的 `qa*/pr*/verify*/base*/rev*/deploy-ws/venv*/pkg-*/*_head/wt-*`
      被清理，日志出现 `tmp janitor: removed N items, freed X bytes, skipped M(active)`。
- [ ] 活跃任务目录、当前 `TASK_TMPDIR`、符号链接、白名单外第三方文件不被删除（单测）。
- [ ] `tmp_janitor_removed_items_total` / `_bytes_total` 与 `grape_tmpfs_used_ratio`
      可在 `/metrics` 查到；新增迁移 post_check 通过。
- [ ] `promtool check rules deploy/alerts.yml` 通过。
- [ ] 目标节点 ≥90% 时派发被拒（`_filter_by_tmp_headroom`），<90% 不受影响。