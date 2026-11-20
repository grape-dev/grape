# Plan: Issue #3202

## 关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 技能标签上限 | 技能侧 `SKILL_MAX_TAGS=5` 显式传参 | kernel 默认 3 保持场景市场 #2761 契约不变 |
| 标签长度校验 | kernel 新增 `oversized_tags()`（`MAX_TAG_LEN=12`） | 共用解析逻辑，技能侧只做规则挂载 |
| 系统标签重名 | Agent 类型名 + `list_skill_categories()` 名称，大小写不敏感 | 与表单侧同一套口径 |
| PATCH 鉴权 | 复用 delete/update 的三分支（official 403 / user 属主 / repo 成员） | 权限与既有接口一致 |
| PATCH 写路径 | 新增具名写语句 `skill_meta_patch_update` | 任意拓扑可用，避免裸 SQL |
| 版本号 | PATCH 不写 `skill_version` | C8 要求「改元信息版本号不变」 |
| 摘要必填 | upload/update 空摘要 400；`_clean_summary` 保证 ≤50 | 保留既有 D3 清洗 |
| 列表 summary | `_SKILL_LIST_COLS` / 精选列补 `summary`，无值时回退 description | 卡片 3 行摘要依赖 |

## 测试

- `tests/mgmt/test_issue3202_skill_card_meta.py`：标签规则、摘要必填、PATCH 三分支鉴权、updated_at、版本不变、清空标签。
- 既有 skills/OpenAPI/i18n/缓存版本 ratchet 同步更新。
