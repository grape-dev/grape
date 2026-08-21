# 华为云 Terraform 编码规范与避坑指南（Coding Agent 用）

## 凭证设置规范

编码时必须遵循以下凭证约定，绝不硬编码到 .tf 文件：

### 环境变量名

| 用途 | 环境变量 | 说明 |
|------|---------|------|
| 华为云 AK | `HW_ACCESS_KEY` | Provider 认证 + OBS backend |
| 华为云 SK | `HW_SECRET_KEY` | Provider 认证 + OBS backend |
| 区域 | `HW_REGION` | 如 cn-north-4 |
| AWS 兼容 AK | `AWS_ACCESS_KEY_ID` | OBS S3 backend init 必需，与 HW_ACCESS_KEY 同值 |
| AWS 兼容 SK | `AWS_SECRET_ACCESS_KEY` | 同上，与 HW_SECRET_KEY 同值 |
| AWS 兼容 Region | `AWS_DEFAULT_REGION` | 同上，与 HW_REGION 同值 |
| ECS 密码 | `TF_VAR_ecs_admin_pass` | 8-26位，含大小写+数字+特殊字符 !@$%^-_=+ |
| RDS 密码 | `TF_VAR_rds_password` | 8-32位，含大小写+数字+特殊字符 |

### TF variables.tf 规范

- 所有密码类变量必须标记 `sensitive = true`
- 密码变量通过 `TF_VAR_` 环境变量传入，不设 default 值
- AK/SK 不写入 variables.tf，由 provider block 通过环境变量自动读取

## cn-north-4 编码避坑

### RDS Flavor 选择

cn-north-4 仅支持 `n1` / `x1` / `arm` 类型 RDS flavor，不支持 `c2` 类型。

编写 variables.tf 时默认值选择：

| 错误默认值 | 正确默认值 | 规格 |
|-----------|-----------|------|
| rds.mysql.c2.large.2 | rds.mysql.n1.large.2 | 2c4g |
| rds.mysql.c2.large.4 | rds.mysql.n1.large.4 | 2c8g |

### RDS 存储类型选择

| 类型 | cn-north-4 状态 | 编码建议 |
|------|----------------|---------|
| ULTRAHIGH | 已弃用（报 DBS.280241） | 禁止使用 |
| CLOUDSSD | 可用 | 推荐设为默认值 |
| LOCALSSD | 可用 | 可选 |
| ESSD | 可用 | 可选 |

### OBS 操作工具选择

编码时涉及 OBS 对象操作，使用 obsutil 而非 curl SigV4 或 hcloud CLI：

| 操作 | hcloud（不存在） | obsutil（正确） |
|------|-----------------|----------------|
| 上传对象 | PutObject | cp |
| 列举对象 | ListBuckets | ls |
| 创建桶 | CreateBucket | mb |

### OBS SigV4 日期格式

如必须用 curl 签名（不推荐），日期格式必须无连字符：

```bash
# 正确（无连字符）
date -u +"%Y%m%dT%H%M%SZ"        # 20260820T154154Z

# 错误（含连字符，OBS 返回 HTTP 400）
date -u +"%Y-%m-%dT%H:%M:%SZ"
```

### 安全组规则编码规范

- 80/tcp 公网开放（0.0.0.0/0）
- 3306/tcp 仅限 VPC 内网（10.0.0.0/16）
- 遵循最小权限原则，不开放非必要端口
- inventory.json 上传时设置 `x-amz-acl: private` header

## 参考资源

- 华为云 Terraform Provider 文档：https://registry.terraform.io/providers/huaweicloud/huaweicloud/latest/docs
- OBS S3 兼容 API：https://support.huaweicloud.com/api-obs/obs_03_0152.html
- RDS Flavor 查询 API：https://support.huaweicloud.com/api-rds/rds_06_0005.html

## Provider 版本约束规范

生成 Terraform 代码时，`required_providers` 块中 huaweicloud provider 的版本约束遵循以下规则：

- ✅ 正确写法：不写版本约束（使用最新版），或写 `>= 1.50`（下限约束，允许高版本）
- ❌ 禁止写法：`~> 1.50`、`~> 1.60` 等 `~>` 上限约束——只匹配 minor 版本段，实际安装的 v1.94.0 会被排除导致 `terraform init` 下载 provider 失败

示例（正确）：
```hcl
terraform {
  required_providers {
    huaweicloud = {
      source = "huaweicloud/huaweicloud"
    }
  }
}
```

示例（错误，禁止）：
```hcl
terraform {
  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.60"  # ❌ 匹配不到 v1.94.0
    }
  }
}
```
