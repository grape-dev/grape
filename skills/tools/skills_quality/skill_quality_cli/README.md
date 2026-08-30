# skill-quality CLI — AI Skill 执行质量上报命令行工具

通过 APIG 公网接口上报，IAM 认证（AK/SK 自动从环境变量加载）。
后端自动从 Token 解析上报人信息（账号名/用户ID/域名）。

- 零第三方依赖（仅 Python 3 标准库）
- 上报失败静默，不影响主流程
- 同一 trace_id 服务端幂等去重

## 安装

```bash
# 方式一: 安装到 /usr/local/bin
cd tools/skills_quality/skill_quality_cli
sudo bash install.sh

# 方式二: 直接使用
chmod +x tools/skills_quality/skill_quality_cli/skill-quality
./tools/skills_quality/skill_quality_cli/skill-quality --version
```

## 使用

### 上报

```bash
# 上报成功
skill-quality report --skill-name "code-review" --status success

# 上报失败
skill-quality report --skill-name "rds-list" --status sys_fail \
  --error-code N01 --error-msg "下游接口超时" --cost-ms 5000

# 完整参数
skill-quality report \
  --skill-name "rds-list" \
  --skill-version "1.0.0" \
  --status success \
  --cost-ms 3200 \
  --input-param '{"region":"cn-north-4"}' \
  --output-result '{"count":5}'
```

### 自检

```bash
skill-quality check
# 输出: 自检成功! trace_id=xxx
```

### wrap模式（包裹命令执行）

```bash
skill-quality wrap --skill-name "my-skill" -- python3 my_skill.py --arg1 val1
# 命令成功 → 上报 success
# 命令失败 → 上报 biz_fail/sys_fail
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SKILL_QUALITY_ENDPOINT` | APIG公网地址 | 上报地址（生产由部署方注入） |
| `SKILL_QUALITY_REGION` | cn-north-4 | 华为云区域 |
| `SKILL_QUALITY_AK` | - | AK（优先级最高） |
| `SKILL_QUALITY_SK` | - | SK（优先级最高） |
| `HUAWEI_CLOUD_SDK_AK` | - | AK（华为云SDK标准，次优先） |
| `HUAWEI_CLOUD_SDK_SK` | - | SK（华为云SDK标准，次优先） |
| `HW_ACCESS_KEY` | - | AK（华为云CLI标准，最低优先） |
| `HW_SECRET_KEY` | - | SK（华为云CLI标准，最低优先） |
| `SKILL_QUALITY_TIMEOUT` | 3 | HTTP 超时秒数 |
| `SKILL_QUALITY_DISABLE` | 0 | 设 1 禁用上报 |

## 状态与错误码

| 状态 | 说明 |
|------|------|
| success | 执行成功 |
| biz_fail | 业务失败（如数据不存在） |
| sys_fail | 系统异常（如网络超时、代码异常） |
| cancel | 人工终止 |

| 分类 | 错误码 | 含义 |
|------|--------|------|
| 用户侧 U | U01/U02/U03/U04 | 入参缺失 / 格式非法 / 违规或无匹配数据 / 权限不足 |
| 配置侧 C | C01/C02/C03 | Skill 配置缺失 / 参数模板错误 / 环境配置失效 |
| 网络 N | N01/N02/N03 | 下游超时 / 网络抖动 / 下游熔断限流 4xx/5xx |
| 代码 B | B01/B02/B03/B04 | 空指针异常 / 死循环 / 边界缺陷 / 版本兼容 |
| 平台 P | P01/P02/P03 | 调度异常 / 资源不足 / 队列积压 |
