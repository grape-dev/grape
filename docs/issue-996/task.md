# Issue #996 任务拆解

- [x] T1 `event_types.normalize_files` + 文件事件类型 + 截断策略
- [x] T2 GitCode/GitHub/GitLab `normalize()` 输出 payload.files
- [x] T3 `files_enrich` payload 缺失时 provider API 兜底
- [x] T4 `guard_impact` 守卫脚本扫描 + 产物目录列举
- [x] T5 `cleanup.clean_issue_artifacts` 删除前守卫预检
- [x] T6 prompt / pipeline events 展示 files、guard_impact
- [x] T7 单测覆盖五类场景 + 回归测试