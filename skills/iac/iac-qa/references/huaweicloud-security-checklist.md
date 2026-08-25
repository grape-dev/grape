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
- [ ] **Issue/PR 评论中不包含 OBS 预签名 URL**（预签名 URL 任何人持有即可访问，等同于泄露密码）
- [ ] 敏感凭证只通过 OBS 桶名 + 对象路径告知，由维护者用 AK/SK 自行获取

## OBS Backend 配置审查（⚠️ 关键）

- [ ] backend 配置包含 `skip_s3_checksum = true`（TF 1.7~1.10 必需，缺失会导致 state 写入 OBS 失败）
- [ ] backend 配置包含 `skip_requesting_account_id = true`（TF 1.7+ 必需，跳过 AWS STS 调用）
- [ ] backend 配置包含 `skip_region_validation`、`skip_credentials_validation`、`skip_metadata_api_check`
- [ ] `endpoint` 值带 `https://` 前缀（如 `https://obs.cn-north-4.myhuaweicloud.com`）
- [ ] backend 配置**未使用** `force_path_style = true`（cn-north-4 OBS 要求 virtual-host-style）
- [ ] 如 .tf 文件或文档中指定了 Terraform CLI 版本约束，确认在 1.7.5 ~ 1.10.5 区间内

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

- [ ] `terraform version` 在 1.7.5 ~ 1.10.5 区间内
- [ ] `terraform fmt -check -recursive` PASS
- [ ] `terraform validate` PASS
- [ ] `terraform init` PASS（OBS backend 配置成功，provider 下载成功）
- [ ] `terraform plan` PASS（⚠️ 不加 -backend=false，使用真实 OBS backend 验证 state 读写；plan 还会调用 provider API 验证镜像/flavor/规格是否存在）
- [ ] plan 输出中无报错（如镜像不存在、RDS flavor 不可用、provider 不兼容等）
- [ ] `terraform apply` PASS（全部资源创建成功，**无 errored.tfstate 文件生成**）
- [ ] 工作目录下**不存在** `errored.tfstate` 文件（存在则说明 state 写入 OBS 失败，需排查 TF 版本和 backend 配置）
- [ ] `terraform output -json` 获取完整资源清单
- [ ] inventory.json 已上传到 OBS 并验证可读
- [ ] ECS 实例状态为 ACTIVE（运行中），EIP 已绑定
- [ ] RDS 实例状态为可用
- [ ] OBS bucket 创建成功

**不在验证范围内（应用层，SRE/QA 不检查）：**
- ~~ECS 公网 80 端口可访问~~（应用是否启动是应用层问题）
- ~~RDS 3306 端口实际连接测试~~（应用层连接逻辑）
