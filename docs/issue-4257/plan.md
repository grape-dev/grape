# Issue #4257 方案

## 定位
守卫实现在 `src/domains/runtime/vcs/release_guard.py`：
- `_ANCHOR_RE` 负责正文主语锚点匹配；
- `first_issue_anchor` 跳过引用行后取首个锚点；
- `check_issue_consistency` 依据锚点与当前任务 Issue 比对，不一致由调用方 409。

## 改动
1. 收紧 `_ANCHOR_RE`：`issues?\s+(?:#\s*)?(\d+)`，要求 `Issue` 与编号之间至少一个空白，
   使 `issue#N` / `Issue#N` 紧邻链路形态不再命中；`Issue #N` / `Issue N` 仍命中。
2. 新增 `_CHAIN_REF_RE = \w+#\d+` 与 `_strip_chain_refs`：在 `first_issue_anchor` 搜索前剔除
   `段名#序号` 链路引用，避免链路中的编号被当成主语。
3. 新增回归测试 `tests/runtime/test_issue_4257_release_guard_link_chain.py`：
   单元层覆盖锚点解析与三态断言，端到端覆盖 PR 评论写回链路（验收①②③）。

## 兼容性
- 标题引用 `first_issue_ref` 逻辑不变（显式锚点优先，否则首个 `#N`），不受影响。
- `Issue #N` 明确主语仍参与断言，真跨 Issue 拦截语义不放宽。
- 引用行（`>` 开头）跳过逻辑保持不变。