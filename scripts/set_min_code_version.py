#!/usr/bin/env python3
"""Issue #536 · 部署脚本配套：蓝绿/双主全部集群升级完成后写 meta 表 min_code_version。

时机（设计文档 D3，硬性）：只在**全部**集群节点都升级到目标版本之后执行——
写早了会让落后实例提前放行 cleanup（DROP 旧列），砸掉还依赖旧 schema 的节点。

版本号格式（QA R2 修复）：必须带**数值点分前缀**，与主控自身版本可比——
  - 主控自身版本（git 检出）：g-2026.10.30-abc123def456（提交日期+短哈希）；
  - 部署目标版本：建议同形日期式，如 2026.12.01 或 2026.12.01-rc1。
无数值点分前缀的值（如纯哈希 abc123）会让 _version_ge 退化为字符串相等，
cleanup 门槛永久不达标——本脚本写入前做形状校验并拒绝。

用法（在升级完成后的任一主控上、或部署流水线末步执行）：
  python3 scripts/set_min_code_version.py <目标版本号>
  python3 scripts/set_min_code_version.py 2026.12.01

幂等：重复执行只覆盖同一个 meta key。写完可用
  python3 -c "from src.api.db_setup import get_min_code_version; print(get_min_code_version())"
核对（所有节点读同一 DB，天然读到同一值）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 形状校验：带数值点分前缀（2026.12.01 / 2026.12.01-rc1 / 与自身版本同形的
# g-2026.10.30-abc123 均通过；纯哈希/空串拒绝）。
_VERSION_SHAPE_RE = re.compile(r"^(g-)?\d{4}(\.\d{1,2}){1,2}([.-].*)?$")


def _validate_version(value: str) -> str:
    """版本号形状校验：必须带数值点分前缀（与 _version_ge 可比）。校验失败返回原因。"""
    if not value:
        return "版本号为空"
    if not _VERSION_SHAPE_RE.match(value):
        return (f"版本号必须带数值点分前缀（如 2026.12.01 或 2026.12.01-rc1），"
                f"与主控自身版本（g-2026.10.30-abc123）可比；收到: {value!r}")
    return ""


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    value = argv[1].strip()
    reason = _validate_version(value)
    if reason:
        print(f"ERROR: {reason}")
        return 2
    # 先初始化 DB（幂等）确保 meta 表就绪
    from src.api.db_setup import init_database, set_min_code_version, get_min_code_version
    init_database()
    set_min_code_version(value)
    readback = get_min_code_version()
    if readback != value:
        print(f"ERROR: 回读不一致（写入 {value}，读回 {readback}）")
        return 1
    print(f"OK: meta.min_code_version = {readback}（cleanup 迁移门槛已生效；"
          f"自身版本 ≥ 该值的实例将放行 kind=cleanup 迁移）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))