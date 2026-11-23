# grape CLI 参考（plugin-tester 子集）

> 本文件是 **grape CLI 命令契约**（v0.3.x，经 `grape commands` 核对）。平台交互一律走 grape CLI。被测插件目标产品的探测调用（方法名探测/401 分层）不属平台交互（curl，30s 超时）。

## 1. 认证（自动，无需配置）

`grape` 自动读取服务端注入的 `$GRAPE_SESSION_TOKEN`——**严禁**寻找/读取其他 token 用于 grape 命令。

## 2. 命令子集（本技能真实使用）

### 2.1 上下文与被测对象

```bash
grape tasks current --json-out
grape git-repo pulls get   --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
```

### 2.2 报告（固定标记开头）

```bash
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过\n\n{四层矩阵}"}'
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body": "[QA Agent] 🔴 测试失败\n\n{失败项：规则编号+原因+修复建议}"}'
```

- 结论标记可检索：`[QA Agent] ✅ 测试通过` / `[QA Agent] 🔴 测试失败`。

### 2.3 A2A 分流（通过 → sre / 失败 → coding）

```bash
grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out   # 确认下游节点存在
grape a2a handoff --json '{"agent": "sre", "payload": {"repository": "'"$REPO_FULL_NAME"'", "account": "'"$ACCOUNT"'", "issue_number": '"$ISSUE_NUMBER"', "pr_number": '"${PR_NUMBER:-0}"', "context": "{交棒说明}"}}' --json-out
grape a2a handoff --json '{"agent": "coding", "payload": {"repository": "'"$REPO_FULL_NAME"'", "account": "'"$ACCOUNT"'", "issue_number": '"$ISSUE_NUMBER"', "pr_number": '"${PR_NUMBER:-0}"', "context": "{打回原因}"}}' --json-out
```

### 2.4 任务收尾

```bash
grape tasks complete --json '{"result_text": "{结论}", "next_step": "a2a_sre", "actions": ["..."], "conclusion": "{...}", "artifacts": ["..."], "issue_summary": "{...}"}'
```

- `already:true` 返回属正常（任务已终态），忽略并结束。
