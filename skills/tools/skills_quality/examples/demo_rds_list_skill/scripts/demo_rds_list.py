#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_rds_list.py — 华为云 RDS 实例列表查询(质量上报 SDK 集成演示)

模拟真实 skill 的完整接入方式, 覆盖四种质量场景:
  成功 / 业务失败(无数据 U03) / 参数非法(U02) / 系统失败(网络超时 N01)

接入点(三种方式各演示一处):
  1. 装饰器          @quality_report()  包住 run_query (成功/异常自动上报)
  2. 上下文管理器     with quality_context()  (业务失败手动标记)
  3. 手动上报         report()            (预校验失败直接上报后返回)

运行:
  python3 demo_rds_list.py --region cn-north-4          # 正常
  python3 demo_rds_list.py --region cn-north-4 --empty  # 业务失败: 无数据
  python3 demo_rds_list.py --region mars-1              # 参数非法
  python3 demo_rds_list.py --region cn-north-4 --network-error  # 系统失败: 超时
  python3 demo_rds_list.py --all                        # 全场景连跑(演示)
"""
import argparse
import random
import sys
import time

# ---- 1. 复制 SDK 到 scripts/ 目录后直接导入(零依赖) ----
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from skill_quality_sdk import (
    quality_report, quality_context, report,
    QualityError, ERROR_U02, ERROR_U03, ERROR_N01, STATUS_SYS_FAIL,
)

VALID_REGIONS = {"cn-north-4", "cn-east-3", "cn-south-1"}

# 模拟华为云 RDS 服务(代替真实 SDK 调用)
class FakeRdsClient:
    def list_instances(self, region):
        if region not in VALID_REGIONS:
            raise ValueError(f"region 非法: {region}, 合法值: {sorted(VALID_REGIONS)}")
        time.sleep(random.uniform(0.05, 0.3))          # 模拟网络耗时
        return [{"id": f"rds-{region}-{i}", "name": f"实例{i}", "status": "ACTIVE"}
                for i in range(1, 3)]

client = FakeRdsClient()


# ---- 2. 装饰器接入: 主查询函数(成功/异常自动上报) ----
@quality_report(skill_name="huawei-cloud-rds-list",
                skill_id="huawei-cloud-rds-list",
                skill_version="1.0.0", trigger_type="manual")
def run_query(region: str) -> list:
    """查询 RDS 实例列表。返回实例列表; 无实例抛业务失败; 异常抛系统失败。"""
    instances = client.list_instances(region)          # 非法 region 抛 ValueError → 自动记 SysFail/B01
    if not instances:
        raise QualityError(ERROR_U03, "未查询到该地域的 RDS 实例")   # 记 BizFail/U03
    return instances


# ---- 3. 上下文管理器接入: 带精确入参/输出的查询 ----
def query_with_context(region: str) -> list:
    with quality_context("huawei-cloud-rds-list", skill_version="1.0.0",
                         trigger_type="agent", timeout_threshold_ms=3000) as q:
        q.input = {"region": region, "page": 1, "limit": 10}
        try:
            instances = client.list_instances(region)
        except Exception as e:
            # 网络类异常由 __exit__ 兜底捕获并上报(推断错误码)
            raise
        if not instances:
            q.fail(ERROR_U03, "未查询到该地域的 RDS 实例")
            return []
        q.output = {"count": len(instances), "instances": instances}
        return instances


# ---- 4. 手动上报接入: 参数预校验(装饰器管不到的分支) ----
def precheck(region: str) -> bool:
    if region not in VALID_REGIONS:
        report(skill_name="huawei-cloud-rds-list", skill_version="1.0.0",
               trigger_type="manual", status=STATUS_SYS_FAIL,
               error_code=ERROR_U02, error_msg=f"入参格式非法: region={region} 不在枚举内",
               input_param={"region": region}, cost_ms=1)
        print(f"  [手动上报] region 非法 -> SysFail/U02 已上报\n")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="cn-north-4")
    ap.add_argument("--empty", action="store_true", help="模拟无实例(业务失败)")
    ap.add_argument("--network-error", action="store_true", help="模拟网络超时(系统失败)")
    ap.add_argument("--all", action="store_true", help="全场景连跑")
    args = ap.parse_args()

    print("=" * 56)
    print("huawei-cloud-rds-list — 质量上报 SDK 集成演示")
    print("=" * 56)

    if args.all:
        print("\n--- 场景1: 正常查询(装饰器) ---")
        r = run_query("cn-north-4")
        print(f"  返回 {len(r)} 个实例 -> success 上报")

        print("\n--- 场景2: 业务失败-无数据(上下文管理器) ---")
        import unittest.mock as mock
        with mock.patch.object(client, "list_instances", return_value=[]):
            r = query_with_context("cn-south-1")
        print(f"  返回 {len(r)} 个实例 -> BizFail/U03 上报")

        print("\n--- 场景3: 参数非法(手动上报) ---")
        precheck("mars-1")

        print("\n--- 场景4: 系统失败-网络超时(装饰器) ---")
        import unittest.mock as mock
        with mock.patch.object(client, "list_instances", side_effect=TimeoutError("下游接口超时: rds endpoint")):
            try:
                run_query("cn-north-4")
            except TimeoutError:
                print("  TimeoutError 已捕获 -> SysFail/N01 + 堆栈上报")
        print()
    else:
        if not precheck(args.region):
            return
        if args.empty:
            print("  模拟: 该地域无实例...")
            import unittest.mock as mock
            with mock.patch.object(client, "list_instances", return_value=[]):
                r = query_with_context(args.region)
            print(f"  返回 {len(r)} 个实例 -> BizFail/U03 上报")
        elif args.network_error:
            print("  模拟: 网络超时...")
            try:
                raise TimeoutError("下游接口超时: rds endpoint")
            except TimeoutError:
                print("  异常已捕获 -> SysFail/N01 上报")
                report(skill_name="huawei-cloud-rds-list", skill_version="1.0.0",
                       trigger_type="manual", status=STATUS_SYS_FAIL,
                       error_code=ERROR_N01, error_msg="下游接口超时: rds endpoint",
                       full_stack="Traceback (most recent call last):\nTimeoutError: 下游接口超时",
                       input_param={"region": args.region}, cost_ms=5120, is_timeout=1)
        else:
            r = run_query(args.region)
            print(f"  返回 {len(r)} 个实例 -> success 上报")

    print("演示完成。数据已上报到运营台「运行检测 → Skill 运行查询」\n")


if __name__ == "__main__":
    main()
