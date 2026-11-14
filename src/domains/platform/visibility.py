"""平台可见性（用户面）——单一判据，真源是平台行的 `enabled` 列。

口径（2026-11-15 用户拍板，方案甲）：
- **唯一开关 = `grape_t_repo_platforms.enabled`**（行级、DB 权威、配置导入永不覆盖、
  管理面有开关 UI）。`meta.vcs_platforms_hidden` 是上一版的部署级名单，**已退役**：
  迁移 `202611170021` 把名单里的平台写成 `enabled=0`，然后删除该 meta 键。
- 用户面：目录不返回 `enabled=0` 的平台；`GET /api/repos/member?provider=<disabled>` → 403
  `PLATFORM_DISABLED`；不带 provider 的合并列表不发起 disabled 平台拉取。
- 内网/脚本面：托管/建仓/bot/webhook 注册照旧可用，平台方言/host 仍从平台卡解析——
  **关的是"用户面可见"，不是平台能力**。
- 管理面：平台列表照常返回，每项带 `disabled` 标记。

为什么判据必须落在平台行：名单在 meta 里没有管理面入口（只能改 meta/SQL），
行级开关天然有 UI、有导入保护，还自带"是哪个平台"的语义；两套并存只会让运维不知道该用哪个。
"""
from __future__ import annotations

from src.kernel.config import log


def platform_enabled(provider: str) -> bool:
    """该平台在对象模型里是否 enabled（表无行/未迁移/读失败 → 按可见处理，fail-open）。"""
    p = str(provider or "").strip()
    if not p:
        return False
    try:
        from sqlalchemy.orm import Session

        from src.domains.platform.importer import is_table_backed
        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        if not is_table_backed():
            return True
        with Session(get_engine()) as s:
            row = RepoPlatformRepo.get(s, p)
        if row is None:
            # 纯配置平台（尚未导入成行）用户面照常可见，避免"导入前一律不可用"
            return True
        return bool(getattr(row, "enabled", 1))
    except Exception as e:
        log.warning(f"platform_enabled({p}) 读取失败，按可见处理（fail-open）: {e}")
        return True


def is_platform_visible(provider: str) -> bool:
    """该平台是否对**用户面**可见（= 平台行 enabled）。"""
    return platform_enabled(provider)