# Code Review Checklist

## Code Quality
- [ ] Follows project coding style and conventions
- [ ] No dead code or commented-out code
- [ ] Proper error handling and logging
- [ ] No hardcoded sensitive values (AK/SK/passwords)
- [ ] Variable and function names are descriptive
- [ ] Code complexity is manageable
- [ ] No code duplication

## Functional Correctness
- [ ] Implements all acceptance criteria from the spec
- [ ] Edge cases are handled (empty input, null, boundary values)
- [ ] Error paths are properly managed
- [ ] No regression on existing features

## Testing
- [ ] Unit tests cover new code
- [ ] Tests are meaningful (test behavior, not implementation)
- [ ] Edge cases have test coverage
- [ ] Tests pass locally

## Security
- [ ] No secrets/tokens in code or config files
- [ ] Input validation is in place
- [ ] No command injection vulnerabilities
- [ ] Dependency versions are up to date

## Documentation
- [ ] Inline comments for non-obvious logic
- [ ] Public APIs have docstrings
- [ ] CHANGELOG or release notes updated if needed

## 华为云 IaC 专项（自审查用）

提交 PR 前对照以下检查项，与 `references/huaweicloud-tf-conventions.md` 保持一致。

### 凭证与敏感信息
- [ ] AK/SK 未硬编码到任何 .tf 文件，仅通过环境变量（HW_ACCESS_KEY / HW_SECRET_KEY）传入
- [ ] ECS/RDS 密码未硬编码，通过 TF_VAR_ 环境变量传入
- [ ] variables.tf 中所有密码类变量标记 `sensitive = true`
- [ ] variables.tf 中密码变量无 default 值（强制运行时注入）

### OBS Backend 配置
- [ ] backend 配置包含 `skip_s3_checksum = true`（TF 1.7~1.10 必需）
- [ ] backend 配置包含 `skip_requesting_account_id = true`（TF 1.7+ 必需）
- [ ] backend 配置包含 `skip_region_validation`、`skip_credentials_validation`、`skip_metadata_api_check`
- [ ] `endpoint` 值带 `https://` 前缀（如 `https://obs.cn-north-4.myhuaweicloud.com`）
- [ ] backend 配置**未使用** `force_path_style = true`（cn-north-4 OBS 要求 virtual-host-style）

### Provider 版本约束
- [ ] huaweicloud provider 未使用 `~> 1.x` 上限约束（会匹配不到高版本，用 `>= 1.50` 或不写 version）

### cn-north-4 资源规格
- [ ] RDS flavor 使用 cn-north-4 支持的类型（n1 / x1 / arm），非 c2
- [ ] RDS 存储类型使用可用类型（CLOUDSSD / LOCALSSD / ESSD），非 ULTRAHIGH

### 安全组规则
- [ ] 80/tcp 公网开放需有明确业务理由
- [ ] 3306/tcp 禁止公网开放，仅限 VPC 内网（如 10.0.0.0/16）
- [ ] 无 0.0.0.0/0 开放非必要端口（22/tcp、3306/tcp、6379/tcp）