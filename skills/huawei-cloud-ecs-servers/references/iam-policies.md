# IAM 权限策略

## 该 Skill 需要的华为云权限

| 操作 | IAM 权限 | 说明 |
|------|---------|------|
| 查询服务器列表 | ecs:servers:list | ListServersDetails |
| 查询服务器详情 | ecs:servers:get | ShowServer |
| 查询规格列表 | ecs:flavors:list | ListFlavors |
| 查询可用区 | ecs:availabilityZones:list | NovaListAvailabilityZones |

## 最小权限策略

```json
{
  "Version": "1.1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:servers:list",
        "ecs:servers:get",
        "ecs:flavors:list",
        "ecs:availabilityZones:list"
      ],
      "Resource": ["*"]
    }
  ]
}
```

## 权限说明

- 该 skill 只需要只读权限（最小权限原则）
- 推荐系统策略：ECS ReadOnlyAccess
