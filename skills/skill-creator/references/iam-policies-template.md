# IAM 权限策略模板

生成 skill 时，必须为生成的 skill 生成 `references/iam-policies.md`，按以下模板：

## 模板

```markdown
# IAM 权限策略

## 该 Skill 需要的华为云权限

| 操作 | IAM 权限 | 说明 |
|------|---------|------|
| {操作1} | {service}:{action} | {为什么需要} |
| {操作2} | {service}:{action} | {为什么需要} |

## 最小权限策略

```json
{
  "Version": "1.1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "{service}:{action1}",
        "{service}:{action2}"
      ],
      "Resource": ["*"]
    }
  ]
}
```

## 权限说明

- 该 skill 只需要上述列出的权限（最小权限原则）
- 不需要管理员权限
- 不需要写权限（如果是只读查询类 skill）
```

## 规则

1. **最小权限原则**：只列出 skill 实际调用的 API 对应的 IAM 权限
2. **区分读写**：只读查询类 skill 只列只读权限（List/Show/Get）；变更类 skill 列出需要的写权限
3. **不模糊**：不用通配符 `*` 代替具体权限（除非确实需要全部权限）
4. **从 SDK/_http_info 反推**：API 端点对应的 IAM action 从 SDK 文档或 _http_info 的 method name 推导
