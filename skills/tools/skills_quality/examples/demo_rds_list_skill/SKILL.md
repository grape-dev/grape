---
name: huawei-cloud-rds-list
description: 查询华为云 RDS 实例列表(演示 skill 质量上报 SDK 集成)。Triggers include: "查询rds实例", "rds列表", "华为云rds", "list rds instances", "rds instances".
---

# huawei-cloud-rds-list — RDS 实例列表查询(质量上报 SDK 演示版)

查询指定地域的华为云 RDS 实例列表。本 skill 为「AI Skill 执行质量上报」演示版，
完整展示了 skill_quality_sdk 的三种集成方式(装饰器 / 上下文管理器 / 手动上报)。

## 前置条件

- Python 3
- `skill_quality_sdk.py` 复制到本 skill 的 `scripts/` 目录
- 环境变量 `SKILL_QUALITY_ENDPOINT`(默认 http://localhost:18081/api/quality/report)

## 使用

```bash
# 正常查询(成功)
python3 scripts/demo_rds_list.py --region cn-north-4

# 业务失败演示: 未查询到实例(记 BizFail/U03)
python3 scripts/demo_rds_list.py --region cn-south-1 --empty

# 参数非法演示(记 BizFail/U02)
python3 scripts/demo_rds_list.py --region mars-1

# 系统失败演示: 模拟网络超时(记 SysFail/N01 + 超时标记)
python3 scripts/demo_rds_list.py --region cn-north-4 --network-error

# 一次跑完所有场景(演示用)
python3 scripts/demo_rds_list.py --all
```

## 执行后查看

运营管理台「运行检测 → Skill 运行查询」或:

```bash
curl -s "http://localhost:18080/api/quality/stats?skillName=huawei-cloud-rds-list"
curl -s "http://localhost:18080/api/quality/reports?skillName=huawei-cloud-rds-list&size=10"
```
