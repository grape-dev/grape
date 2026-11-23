"""引擎元数据只读路由（P0b B3/B6，D-P0-3 拍板：新建只读路由，不进 settings.py）。

前端（agents.js 徽标颜色、引擎类型判定）自此从 /api/engines/meta 读取
descriptor 派生数据——显示层不再硬编码引擎名/颜色，新增引擎=落一份
descriptor JSON，前端零改动（spec 验收 A8/B6）。
"""
from fastapi import APIRouter

from src.domains.runtime.engine.descriptor import get_registry

router = APIRouter(prefix="/api/engines", tags=["engines"])


@router.get("/meta", operation_id="engines_meta",
            summary="Get engine metadata",
            description="Read-only engine metadata derived from engine descriptors "
                        "(canonical/aliases/protocol/display/probe). New engines ship "
                        "one descriptor JSON and appear here automatically.")
def engines_meta():
    reg = get_registry()
    engines = []
    for canonical, d in reg.all().items():
        engines.append({
            "canonical": canonical,
            "aliases": d.get("aliases", []),
            "protocol": d.get("protocol", ""),
            "page_type": reg.page_type(canonical),
            "display": {
                "name": d.get("display", {}).get("name", canonical),
                "dot_color": d.get("display", {}).get("dot_color", ""),
                # [B6 尾] 页面引擎图标资产（Runtimes 水印等）；键粒度=page_type
                #（hwcloud 与 aishell 共用 AIShell.svg、aishell-dsh 独立资产）
                "icon": d.get("display", {}).get("icon", ""),
            },
            "discover": {"cli": d.get("discover", {}).get("cli", "")},
            "probe": {"mode": d.get("probe", {}).get("mode", "none")},
            "capabilities": d.get("capabilities", {}),
        })
    return {"engines": engines}
