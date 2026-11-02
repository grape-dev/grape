# Plan — Issue #595 嵌套键兜底解析 PR_NUMBER

## 方案

方案 A（Spec 选定）：改动内聚于 `_inject_issue_pr_env` 单函数，不扩散到入队流程。

```
def _inject_issue_pr_env(...):
    payload = evt.get("payload", {}) or {}
    _pr_raw = (payload.get("pr_number")
               or evt.get("_pr_sub_pr_number")
               or payload.get("pull_request", {}).get("number")
               or payload.get("merge_request", {}).get("number")
               or payload.get("object_attributes", {}).get("iid")
               or "")
    _pr_num = str(_pr_raw).lstrip("#")
```

- 优先级：顶层 `payload.pr_number` → `evt._pr_sub_pr_number` → 嵌套三键
  （pull_request.number → merge_request.number → object_attributes.iid），
  兜底仅在既有来源均空时生效，零回归风险。
- 校验沿用 `lstrip("#").isdigit()`，非数字同样不注入（保持 Issue #528 语义）。
- 嵌套键取值容忍 dict 缺失 / 值为 None（`or ""` 链式兜底）。

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | `_inject_issue_pr_env` PR 号提取链增加嵌套兜底三键 |
| `help/usage/agent-environment-variables.md` | PR_NUMBER 行补提取来源说明 |
| `tests/test_issue_595_pr_number_nested_payload.py` | 新增：两形态 env 注入 + 优先级 + 非数字防御 |

## 测试计划

- PR 关联真实 Issue（payload.pull_request.number）→ PR_NUMBER 注入
- GitLab 形态（merge_request.number / object_attributes.iid）→ 注入
- 顶层 pr_number 优先于嵌套键；_pr_sub_pr_number 优先于嵌套键
- PR 订阅形态（仅 _pr_sub_pr_number）不回归
- 嵌套值非数字 → 不注入
- 全量运行 `tests/test_issue_528_pr_sub_env_injection.py` 及相关 event_pipeline 测试防回归

## 风险与缓解

- 嵌套键误命中非 PR 事件：仅 PR 类事件 payload 才有这些嵌套结构，且 isdigit 过滤。
- 多 provider payload 差异：三键顺序覆盖 GitHub/GitLab/GitCode；未命中保持原行为（空）。
