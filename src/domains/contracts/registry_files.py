"""契约登记表必需文件清单（单一事实源）。

Issue #3756：`/openapi.json` 渲染与完整性门禁依赖 `contracts/` 下的登记表；
打包（`scripts/release_pack.py`）与部署预检（`scripts/deploy_preflight.py`）
必须对「哪些文件缺了就不许出包/启动」持同一口径。此前两处各自维护清单，
预检只校验 2/4 文件，缺 `openapi_baseline.json` / `required_operations.json`
时预检 PASS 而运行期契约仍 fail-fast。本模块收敛为唯一来源，任何一方新增
必需文件都只改这里。
"""
from __future__ import annotations

# 契约目录名（相对仓库根）。
CONTRACTS_DIR = "contracts"

# 相对 contracts/ 的必需文件（顺序即打包/预检输出顺序）。
REQUIRED_REGISTRY_FILES: tuple[str, ...] = (
    "deprecated_endpoints.json",
    "deprecated_exemptions.json",
    "openapi_baseline.json",
    "required_operations.json",
)

# 相对仓库根的必需文件路径（打包期存在性断言用）。
CONTRACTS_REQUIRED_FILES: tuple[str, ...] = tuple(
    f"{CONTRACTS_DIR}/{name}" for name in REQUIRED_REGISTRY_FILES
)

# `released/` 下至少需要一份冻结发布快照（观察期门禁基准）。
REQUIRED_RELEASED_GLOB = f"{CONTRACTS_DIR}/released/*.json"