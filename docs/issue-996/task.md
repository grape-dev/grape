# Issue #996 任务拆解

- [x] T1 `event_types.normalize_files` + 文件事件类型 + 截断策略
- [x] T2 GitCode/GitHub/GitLab `normalize()` 输出 payload.files
- [x] T3 `files_enrich` payload 缺失时 provider API 兜底
- [x] T4 `guard_impact` 守卫脚本扫描 + 产物目录列举
- [x] T5 `cleanup` 删除前守卫预检接入生产清理链路
      —— 平台新增 `POST .../issues/{number}/artifacts/clean`（默认 `dry_run=true`）：
      列举 `docs/issue-N/*` → 守卫预检 → 未命中才逐条 contents DELETE；
      devdzr-merge-bot 可直接调用该端点，无需在旁仓重写预检
- [x] T6 prompt / pipeline events 展示 files、guard_impact
- [x] T7 单测覆盖五类场景 + 回归测试