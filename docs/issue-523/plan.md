# Plan: Issue #523

## 方案
`_post_gitcode_issue` 抛出的 `RuntimeError("GitCode API POST issues failed (403): <上游正文>")` 已含上游正文；在调用方 403 except 分支中提取 `(403): ` 之后正文，统一脱敏（过滤 token/password/authorization 等键值）+ 截断 ≤200 字符，按关键词分流生成提示，替换单一 assignee 断言。

### 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 分流依据 | 上游正文关键词（非仅 HTTP code） | HTTP code 相同语义不同（#523 双端对照实证） |
| 正文回显 | 截断 + 脱敏 | 与现有 `_err_msg[:200]` 惯例一致，避免泄漏与超长 |
| 仓库类型识别 | 上游正文关键词 | 上游 403 正文是权威信号，预判需额外 API 且与 GitCode 行为耦合 |

## 改动
| 模块 | 类型 | 说明 |
|------|------|------|
| `src/api/issues_center.py` | 修改 | 403 分支改为正文提取 + 语义分流 |
| `tests/test_issue_523_create_403_attribution.py` | 新增 | 四类回归用例（仓库类型限制 / 权限类 / 未识别 403 / 正常建单成功） |

## 依赖
`create_task_via_issue` → `_post_gitcode_issue` 既有调用不变，仅 except 分支消费正文；无新增模块依赖。

## 风险与缓解
- 上游正文措辞变化漏匹配 → 未识别正文回退通用 403 提示并附脱敏原文，不猜测归因。
- 正文回显泄漏 → 截断 ≤200 字符 + 过滤 token 类模式。
- 影响正常建单 → 分流仅位于 except 分支，成功路径零改动，回归用例覆盖。
