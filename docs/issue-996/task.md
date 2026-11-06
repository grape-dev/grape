# Issue #996 任务拆解

- [x] T1 `event_types.normalize_files` + 文件事件类型 + 截断策略
- [x] T2 GitCode/GitHub/GitLab `normalize()` 输出 payload.files
- [x] T3 `files_enrich` payload 缺失时 provider API 兜底
- [x] T4 `guard_impact` 守卫脚本扫描 + 产物目录列举
- [ ] T5 `cleanup` 删除前守卫预检接入生产清理链路
      —— 预检/清理函数与单测已完成，平台内无删除调用方；待 devdzr-merge-bot
      按 `contract-devdzr-merge-bot.md` 接线后勾选
- [x] T6 prompt / pipeline events 展示 files、guard_impact
- [x] T7 单测覆盖五类场景 + 回归测试