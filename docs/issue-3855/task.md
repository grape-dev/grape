# Task: Issue #3855

- [x] 后端名称校验工具（必填 / ≤20 码点 / 同 scope 去重）
- [x] upload 路由接入 display_name（必填校验 + 重名阻断 + 写入）
- [x] patch_meta 白名单加 display_name；官方仅放开名称
- [x] upsert_skill_obs 保留/回填语义；重新上传不覆盖
- [x] 前端上传/编辑弹窗名称字段 + 计数器 + 官方入口 + 保存刷新
- [x] zh/en i18n 文案
- [x] 新增测试（必填/边界/Unicode/重名/跨 scope/官方改名/同步保留/存量回退）
- [x] 既有相关测试回归