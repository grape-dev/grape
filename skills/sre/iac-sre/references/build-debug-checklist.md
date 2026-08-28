# Build & Debug Checklist

## Pre-Diagnosis
- [ ] Full error log collected (no truncation)
- [ ] Error type classified: compile/link/dependency/test/runtime
- [ ] Error frequency established (first occurrence vs recurring)
- [ ] Past Issue/PR search conducted for similar error patterns
- [ ] Environment version info gathered (OS, runtime, toolchain)
- [ ] **Terraform CLI version confirmed** (OBS S3 backend 兼容性窗口: 1.7.5 ~ 1.10.5)

## Build Diagnostics
- [ ] Compiler/linter output parsed for error locations
- [ ] Missing dependencies identified
- [ ] Version conflicts checked (dependency tree)
- [ ] Platform-specific issues considered (OS, architecture)
- [ ] Incremental build vs clean build compared

## Dependency Management
- [ ] Lock file vs manifest file version mismatch checked
- [ ] Direct and transitive dependency versions verified
- [ ] Deprecated or EOL dependencies flagged
- [ ] Compatible version ranges confirmed

## Test Failure Analysis
- [ ] Test report parsed (pass/fail/skip counts)
- [ ] Failure stack trace analyzed for root cause
- [ ] Test environment vs CI environment differences noted
- [ ] Flaky test patterns identified (timing, race conditions)
- [ ] Code change diff correlated with failing tests

## Runtime Problem Localization
- [ ] Application logs examined (error level)
- [ ] Stack trace unwound to origin
- [ ] Recent code changes reviewed for introducing commit
- [ ] Resource constraints checked (memory/CPU/disk/network)
- [ ] Configuration drift between environments considered

## OBS Backend 故障排查
- [ ] 检查 `terraform version` 是否在 1.7.5 ~ 1.10.5 兼容区间内
- [ ] 检查 backend 配置是否包含 `skip_s3_checksum = true` 和 `skip_requesting_account_id = true`
- [ ] 检查 `endpoint` 是否带 `https://` 前缀
- [ ] 检查 backend 配置是否误用了 `force_path_style = true`（cn-north-4 禁用）
- [ ] 检查工作目录是否存在 `errored.tfstate` 文件（存在 = state 写入 OBS 失败）
- [ ] 错误信息匹配：
  - `XAmzContentSHA256Mismatch` → TF 版本不对或缺少 `skip_s3_checksum`
  - `Retrieving AWS account details` / `STS GetCallerIdentity` → 缺少 `skip_requesting_account_id`
  - `VirtualHostDomainRequired` → 误用了 `force_path_style`
  - `Unsupported argument` → TF 版本不支持该 backend 参数
  - `unsupported protocol scheme` → `endpoint` 缺少 `https://` 前缀

## Output Requirements
- Root cause clearly stated
- Reproduction steps documented
- Fix recommendation with priority level
- Workaround provided if available
- Declare "past Issue/PR search conducted: <pattern keywords>"