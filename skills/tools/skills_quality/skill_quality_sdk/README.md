# skill-quality-sdk — AI Skill 执行质量上报 SDK

Skills 一键接入 skillsopr 运营管理台「运行检测 → Skill 运行查询」的质量上报 SDK。
每次 Skill 执行结束自动上报：trace_id / 耗时 / 状态 / 错误码 / 脱敏入参输出 / 堆栈。

- 零第三方依赖（仅 Python 3 标准库）
- 上报失败静默，绝不影响 Skill 主流程
- 同一 trace_id 服务端幂等，重复上报自动去重
- 入参/输出自动脱敏（手机号 / 密钥 / Token / AK-SK）

## 快速集成

### 方式一：装饰器（推荐，最简单）

```python
from skill_quality_sdk import quality_report

@quality_report(skill_name="huawei-cloud-rds-list", skill_version="1.0.0")
def rds_list(instance_id):
    # ... 你的 Skill 主逻辑 ...
    return result
```

成功自动上报 `success`；抛异常自动上报 `sys_fail` + 堆栈 + 推断错误码；
抛 `QualityError` 自动上报 `biz_fail`。

### 方式二：上下文管理器（需要中途标记状态 / 精确控制入参输出）

```python
from skill_quality_sdk import quality_context, QualityError, ERROR_U03

def run(region):
    with quality_context("huawei-cloud-rds-list", skill_version="1.0.0",
                         timeout_threshold_ms=5000) as q:
        q.input = {"region": region}
        data = fetch(region)
        if not data:
            raise QualityError(ERROR_U03, "未查询到该地域的 RDS 实例")
        q.output = data
        return data
```

### 方式三：手动上报（完全自主控制）

```python
from skill_quality_sdk import report, STATUS_SYS_FAIL, ERROR_N01

report(skill_name="my-skill", status=STATUS_SYS_FAIL,
       error_code=ERROR_N01, error_msg="下游接口超时",
       full_stack=traceback.format_exc(), input_param=req, cost_ms=1234)
```

## 业务失败与错误码

| 分类 | 错误码 | 含义 |
|------|--------|------|
| 用户侧 U | U01/U02/U03/U04 | 入参缺失 / 格式非法 / 违规或无匹配数据 / 权限不足 |
| 配置侧 C | C01/C02/C03 | Skill 配置缺失 / 参数模板错误 / 环境配置失效 |
| 网络 N | N01/N02/N03 | 下游超时 / 网络抖动 / 下游熔断限流 4xx/5xx |
| 代码 B | B01/B02/B03/B04 | 空指针异常 / 死循环 / 边界缺陷 / 版本兼容 |
| 平台 P | P01/P02/P03 | 调度异常 / 资源不足 / 队列积压 |

业务失败用 `raise QualityError(ERROR_U03, "未查询到匹配数据")`（记 BizFail）；
系统异常 SDK 自动按消息特征推断错误码，也可在捕获处显式 `report(status=STATUS_SYS_FAIL, error_code=ERROR_N01, ...)` 覆盖。

## 配置（环境变量，均可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| SKILL_QUALITY_ENDPOINT | http://localhost:18081/api/quality/report | 运营平台上报地址 |
| SKILL_QUALITY_NAME | 函数名 | Skill 名称（装饰器参数优先） |
| SKILL_QUALITY_ID | 空 | Skill ID |
| SKILL_QUALITY_VERSION | 空 | 版本号 |
| SKILL_QUALITY_AGENT | hermes/unknown | 执行该 skill 的 agent 名称（如 hermes/opencode；未设置时依次探测 HERMES_AGENT_NAME/AGENT_NAME/Hermes 会话特征，最后 unknown，不填机器名） |
| SKILL_QUALITY_TRIGGER | agent | auto/manual/agent/workflow |
| SKILL_QUALITY_DISABLE | 0 | 设 1 禁用上报（本地调试） |
| SKILL_QUALITY_TIMEOUT | 3 | 上报 HTTP 超时秒数 |

## 自检

```bash
python3 skill_quality_sdk.py
# 输出: 上报成功, trace_id=xxx, endpoint=http://...
# 然后到运营台「运行检测 → Skill 运行查询」刷新即可看到 __sdk_self_check__ 记录
```

## 集成清单

1. 把 `skill_quality_sdk.py` 复制到 skill 的 `scripts/` 目录（或 pip 安装到环境）
2. Skill 主函数加 `@quality_report(skill_name="...")` 装饰器
3. 设置 `SKILL_QUALITY_ENDPOINT` 指向运营平台（生产环境由部署方注入）
4. 跑一次自检确认可见
