# Plan: Issue #4335

## 改动点
1. `src/domains/runtime/vcs/release_guard.py`
   - `_ANCHOR_RE`：采用 PR #2150（#4329）的词元/路径边界正则，对齐评论侧在途实现；
   - `first_issue_anchor`：主语判定收窄为「开头行 + markdown 标题行」内的锚点，
     正文段落内引用全部视为样例引用（白名单）；引用行跳过/链路引用剔除语义保留；
   - 更新模块与函数 docstring，说明 #4335 收窄规则与残余风险（漏放极少数真实主语
     正文，Spec 已按 Med 风险接受，409 能力保留不降级）。
2. 基线测试同步（语义收窄后失效的 2 处断言改为新语义等效断言，意图不变）：
   - `tests/runtime/test_issue_2225_release_guard.py`：`## 概述` 首行为标题且无锚点、
     正文段落内锚点不再作主语 → 断言 None，并补开头行主语正例；
   - `tests/runtime/test_issue_4257_release_guard_link_chain.py`：链路引用后接正文
     段落的锚点不再作主语 → 改为标题行形态断言原意图（显式主语仍可命中）。
3. 新增 `tests/runtime/test_issue_4335_release_guard_update_body.py`：
   - 真实反馈正文（首行 `## 用户反馈` + 段落内 `Issue #3888` 样例/「」错误消息引用）
     → update 传 body / 评论两侧均放行；
   - 真实主语不一致（开头行 / `# Spec:` 标题行 / 正文内 `## Issue #N` 标题行）
     → 两侧均 409；
   - 不传 body 行为不变（target 不一致 + 空 body 放行）；
   - #4329 样例前缀/测试文件名白名单、链路引用、引用行回归。

## 验证
`python3 -m pytest tests/runtime/test_issue_4335_release_guard_update_body.py
tests/runtime/test_issue_2225_release_guard.py
tests/runtime/test_issue_4257_release_guard_link_chain.py`
（全量 runtime 守卫相关测试不回归）
