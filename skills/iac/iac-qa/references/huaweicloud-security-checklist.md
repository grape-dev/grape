# 华为云 IaC 安全检查清单（QA Agent 用）

在通用安全检查（references/security-checklist.md）基础上，审查华为云 Terraform IaC PR 时增加以下专项检查。

## 敏感信息检查

- [ ] AK/SK 未硬编码到任何 .tf 文件，仅通过环境变量（HW_ACCESS_KEY / HW_SECRET_KEY）传入
- [ ] ECS/RDS 密码未硬编码，通过 TF_VAR_ 环境变量传入
- [ ] variables.tf 中所有密码类变量标记 `sensitive = true`
- [ ] variables.tf 中密码变量无 default 值（强制运行时注入）
- [ ] terraform plan / state 输出中无明文密码
- [ ] 临时凭证文件（如 /tmp/tf-deploy-env.sh）使用 `shred` 销毁，非 rm

## OBS 远程状态安全

- [ ] Terraform 远程状态存储于 OBS 私有 bucket（非公开）
- [ ] inventory.json 上传时设置 `x-amz-acl: private` header
- [ ] OBS bucket 访问策略限制到指定 AK/SK

## 安全组规则审查

- [ ] 安全组规则最小化（最小权限原则）
- [ ] 80/tcp 公网开放需有明确业务理由
- [ ] 3306/tcp（数据库端口）禁止公网开放，仅限 VPC 内网（如 10.0.0.0/16）
- [ ] 无 0.0.0.0/0 开放非必要端口（如 22/tcp SSH、3306/tcp、6379/tcp Redis）

## cn-north-4 资源规格合规性

- [ ] RDS flavor 使用 cn-north-4 支持的类型（n1 / x1 / arm），非 c2
- [ ] RDS 存储类型使用可用类型（CLOUDSSD / LOCALSSD / ESSD），非 ULTRAHIGH
- [ ] 如 PR 引入新的资源规格，确认已通过 ListFlavors / ListStorageTypes API 验证可用性

## 部署验证清单对照

审查 PR 时核对以下部署验证项是否齐全：

- [ ] `terraform fmt -check -recursive` PASS
- [ ] `terraform validate` PASS
- [ ] `terraform init` PASS（OBS backend 配置成功）
- [ ] `terraform plan` PASS（资源变更预览正确）
- [ ] `terraform apply` PASS（全部资源创建成功）
- [ ] `terraform output -json` 获取完整资源清单
- [ ] inventory.json 已上传到 OBS 并验证可读
- [ ] ECS 公网 80 端口可访问
- [ ] RDS 3306 端口 VPC 内网可达
