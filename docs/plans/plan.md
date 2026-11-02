# Plan: Issue #284

1. 修改 `_ensure_task_conversation`：
   - 新建标题：`Issue #N · {title}`（title 空回退 `Issue #N`，超长截断 ~100 字符）
   - 复用匹配：优先 `conv_type=='task' and issue_number==N`，回退 `title == f"Issue #{N}"`
   - 存量升级：命中旧标题会话时刷新为新格式（不覆盖用户自定义标题）

2. 新增测试 `tests/test_issue_284_conversation_title.py`：
   - 标题格式（新建会话标题含需求标题）
   - 复用不重复建（同一 issue 重复 ensure 只一条会话）
   - 旧标题升级（存量 Issue #N 会话 → 新格式）
   - 空标题回退（title 为空 → Issue #N）
