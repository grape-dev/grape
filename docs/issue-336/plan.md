# Plan: Issue #336 — version 校验放宽

## 方案设计

### 改动点（2 个文件）
1. `src/user/validator.py` `validate_skill()`：
   - 正则 `^v?\d+\.\d+(\.\d+)?$` → `^v?\d+(\.\d+){0,2}$`（单行改动，方案 B：正则量化）；
   - 注释与报错文案同步：`（x.y.z）` → `（x / x.y / x.y.z，兼容 v 前缀）`。
2. `tests/test_skills_obs.py`：新增 `TestSkillVersionFormat` 测试类（TDD：先写用例跑 RED，改正则后跑 GREEN）。

### 测试矩阵
| 用例 | 输入 version | 期望 |
|------|-------------|------|
| 单段数字 | `1` | 200（user + repo 两种 scope） |
| 两段数字 | `1.2` | 200 |
| 三段数字 | `1.2.3` | 200 |
| v 前缀 | `v1.2.3` | 200 |
| 缺失 | 不写 version | 200（默认 1.0.0） |
| 纯字母 | `abc` | 400 errors 含提示 |
| 四段 | `1.2.3.4` | 400 |
| 非数字段 | `1.x` | 400 |
| 尾点 | `1.` | 400 |

### 风险与对策
- 正则误放宽（`1.x`、`1.`）→ 非法集合单测固定覆盖。
- 存储 v2.4 去版本化 → key 不含 version，无迁移需求，无影响。

## 任务拆解
- [x] T1 读写用例（tests/test_skills_obs.py 补 TestSkillVersionFormat）→ RED
- [x] T2 放宽 validator.py 正则 + 文案同步 → GREEN
- [x] T3 回归 tests/test_skills_obs.py 全量 + 既有 validator 相关测试
- [x] T4 自审查（5 轴）
- [x] T5 提交 + 推送 feat/issue-336 + 建 PR（base=local-liufei）+ 评论 + A2A QA
