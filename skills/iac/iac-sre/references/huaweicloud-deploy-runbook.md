# 华为云 Terraform 部署 Runbook（SRE 执行用）

## 适用场景

- 使用 Terraform 在华为云部署基础设施（ECS/RDS/VPC/安全组/EIP）
- 使用 OBS 作为 Terraform 远程状态后端（S3 兼容）
- 需要将资源清单（含敏感密码）保存到 OBS
- cn-north-4（华北4）区域部署

## 前置条件

- Terraform >= 1.0 已安装
- huaweicloud provider（最新版即可，实际验证 v1.94.0 可用；不要写 `~> 1.50` 或 `~> 1.60` 等上限约束，会匹配不到高版本）
- obsutil 已安装（用于 OBS 对象操作，替代 curl SigV4）
- 华为云 AK/SK 已获取（或通过 Grape 控制台注入）
- OBS bucket 已创建（或通过 scripts/bootstrap.sh 创建）

## 部署 Runbook

### 1. 凭证设置

```bash
# 华为云凭证（Provider 认证 + OBS backend 认证共用）
export HW_ACCESS_KEY="<AK>"
export HW_SECRET_KEY="<SK>"
export HW_REGION="cn-north-4"

# AWS 兼容凭证（OBS S3 backend init 必需，与 HW 同值）
export AWS_ACCESS_KEY_ID="$HW_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$HW_SECRET_KEY"
export AWS_DEFAULT_REGION="cn-north-4"

# 敏感密码（通过环境变量传入，绝不硬编码到 .tf 文件）
# ECS root 密码：8-26位，含大小写+数字+特殊字符 !@$%^-_=+
export TF_VAR_ecs_admin_pass='<ECS密码>'
# RDS root 密码：8-32位，含大小写+数字+特殊字符
export TF_VAR_rds_password='<RDS密码>'
```

### 2. 预建 OBS Bucket（首次部署）

```bash
./scripts/bootstrap.sh
```

### 3. 部署基础设施

```bash
cd examples/blog-infrastructure

# 初始化（配置 OBS 远程状态后端）
terraform init

# 预览资源变更
terraform plan

# 部署（RDS 创建可能需要 5-10 分钟）
terraform apply -auto-approve

# 输出资源清单（含 sensitive 密码）
terraform output -json
```

### 4. 保存资源清单到 OBS

```bash
cd ../..
./scripts/save-inventory-to-obs.sh
```

或使用 obsutil 手动上传：

```bash
obsutil cp ./inventory.json obs://my-terraform-state-bucket/blog-infrastructure/inventory.json
```

### 5. 验证

```bash
# 验证 OBS 清单可读
obsutil ls obs://my-terraform-state-bucket/blog-infrastructure/
obsutil cat obs://my-terraform-state-bucket/blog-infrastructure/inventory.json
```

## cn-north-4 区域已知坑点

### RDS Flavor 可用性

cn-north-4 仅支持 `n1` / `x1` / `arm` 类型 RDS flavor，不支持 `c2` 类型。

| 代码中配置 | cn-north-4 状态 | 替代方案 |
|-----------|----------------|---------|
| rds.mysql.c2.large.2 | 不存在 | rds.mysql.n1.large.2 (2c4g) |
| rds.mysql.c2.large.4 | 不存在 | rds.mysql.n1.large.4 (2c8g) |

部署前通过 ListFlavors API 确认可用规格：
```bash
# 查询可用 RDS flavor
curl -s -H "X-Auth-Token: ***" \
  "https://rds.cn-north-4.myhuaweicloud.com/v3/flavors?db_name=mysql&region=cn-north-4"
```

### RDS 存储类型

| 类型 | cn-north-4 状态 | 说明 |
|------|----------------|------|
| ULTRAHIGH | 已弃用 | 不再支持，会报 DBS.280241 |
| CLOUDSSD | 可用 | SSD 云盘，推荐 |
| LOCALSSD | 可用 | 本地 SSD |
| ESSD | 可用 | 增强型 SSD |

部署前通过 ListStorageTypes API 确认：
```bash
curl -s -H "X-Auth-Token: ***" \
  "https://rds.cn-north-4.myhuaweicloud.com/v3/storage-type?region=cn-north-4&engine=mysql"
```

### OBS SigV4 签名日期格式

OBS SigV4 签名中 `x-amz-date` header 必须使用 `yyyyMMddTHHmmssZ` 格式（无连字符），不能使用 ISO 8601 标准格式。

```bash
# 错误（含连字符，OBS 返回 HTTP 400）
date -u +"%Y-%m-%dT%H:%M:%SZ"   # 2026-08-20T15:41:54Z

# 正确（无连字符）
date -u +"%Y%m%dT%H%M%SZ"        # 20260820T154154Z
```

建议：优先使用 obsutil 替代手动 SigV4 curl 签名，避免格式坑点。

### hcloud CLI OBS 命令差异

hcloud CLI 与 obsutil 的 OBS 命令语法不同：

| 操作 | hcloud（不存在） | obsutil（正确） |
|------|-----------------|----------------|
| 上传对象 | PutObject | cp |
| 列举对象 | ListBuckets | ls |
| 创建桶 | CreateBucket | mb |

## 常见问题排查

### terraform init 失败：No valid credential sources found

- 根因：环境变量 HW_ACCESS_KEY / HW_SECRET_KEY 未设置
- 解决：在 Grape 控制台注入 AK/SK，或手动 `export HW_ACCESS_KEY=... HW_SECRET_KEY=...`
- 注意：同时需要设置 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY（S3 backend 兼容）

### terraform apply 失败：DBS.280434 Invalid specification code

- 根因：RDS flavor 在 cn-north-4 不可用（如 c2 类型）
- 解决：通过 ListFlavors API 查询可用规格，用 `-var="rds_flavor=rds.mysql.n1.large.2"` 覆盖
- 永久修复：修改 variables.tf 默认值

### terraform apply 失败：DBS.280241 Invalid storage type

- 根因：存储类型已弃用（如 ULTRAHIGH）
- 解决：用 `-var="rds_volume_type=CLOUDSSD"` 覆盖
- 永久修复：修改 variables.tf 默认值

### save-inventory-to-obs.sh 失败：HTTP 400

- 根因：SigV4 签名日期格式错误（含连字符）
- 解决：使用 obsutil 替代 curl，或修复 date 格式为 `date -u +"%Y%m%dT%H%M%SZ"`
- 注意：hcloud CLI 不存在 PutObject 命令，必须使用 obsutil

### RDS 创建耗时较长

- RDS MySQL 单机实例创建通常需要 5-10 分钟
- terraform apply 中 RDS 是最后一个创建的资源
- 属正常行为，无需干预

## 部署验证清单

- [ ] `terraform fmt -check -recursive` PASS
- [ ] `terraform validate` PASS
- [ ] `terraform init` PASS（OBS backend 配置成功，provider 下载成功）
- [ ] `terraform plan` PASS（资源变更预览正确）
- [ ] `terraform apply` PASS（全部资源创建成功）
- [ ] `terraform output -json` 获取完整资源清单（含 sensitive 密码）
- [ ] `save-inventory-to-obs.sh` 上传成功（或 obsutil cp 成功）
- [ ] OBS GET 验证 inventory.json 可读（HTTP 200）
- [ ] ECS 公网 80 端口可访问
- [ ] RDS 3306 端口 VPC 内网可达

## 资源清单 inventory.json 结构

```json
{
  "generated_at": "<时间戳>",
  "resources": {
    "ecs": {
      "id": "<ECS UUID>",
      "public_ip": "<公网 IP>",
      "admin_pass": "<ECS root 密码>"
    },
    "rds": {
      "id": "<RDS 实例 ID>",
      "private_ip": "<私网 IP>",
      "password": "<RDS root 密码>"
    }
  }
}
```

## 安全实践

1. AK/SK 通过环境变量传入，不写入任何文件/日志/评论
2. ECS/RDS 密码通过 TF_VAR_ 环境变量传入，variables.tf 中标记 `sensitive = true`
3. 临时凭证文件（如 /tmp/tf-deploy-env.sh）使用 `shred` 安全销毁
4. Terraform 远程状态存储于 OBS 私有 bucket
5. 安全组规则最小化：80/tcp 公网开放 + 3306/tcp 仅限 VPC 内网（10.0.0.0/16）
6. inventory.json 上传时设置 `x-amz-acl: private` header 双重保障
7. 密码获取途径：`terraform output -json`（sensitive 字段）或从 OBS bucket 读取

## 参考资源

- 华为云 Terraform Provider 文档：https://registry.terraform.io/providers/huaweicloud/huaweicloud/latest/docs
- OBS S3 兼容 API：https://support.huaweicloud.com/api-obs/obs_03_0152.html
- RDS Flavor 查询 API：https://support.huaweicloud.com/api-rds/rds_06_0005.html
