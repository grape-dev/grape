"""Issue #4082：`/api/runtime/runtimes/{runtime_id}/*` 非法 id 统一 400 预处理。

生产现象：worker 以 `runtime_id=0` 探活 `GET /api/runtime/runtimes/0/tags` 恒 401
（`abc` 则被 FastAPI 参数校验拦成 422）。两者都把**配置问题伪装成鉴权/参数问题**，
排障被误导，最终表现为「runtime 在线但不具备任何标签」却被记录成 401 噪声。

做法：给 runtime 域的两个 router 挂同一个 `APIRoute` 子类，在**任何端点逻辑与鉴权
依赖之前**校验路径里的 `runtime_id`：

- 合法（正整数）→ 原 handler 原样执行（行为、OpenAPI schema 均不变）；
- 非法（`0`/`-1`/`abc`/`1.5`/前导零）→ 直接 400 `invalid runtime_id`，不进入鉴权分支。

为什么用 `route_class` 而不是中间件/依赖：

- 中间件是进程级、对所有请求生效，为一个域加负担不划算；
- FastAPI 的路径参数校验（422）发生在端点解析阶段，依赖表达式靠内部顺序不保险；
- `get_route_handler` 包装在 FastAPI 内部 handler **之外**，是官方支持的扩展点，
  能确定性地先于参数校验与鉴权返回。
"""

import re

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

# 规范正整数形态（与服务端/worker 同口径：`0`、前导零、负数、小数、非数字一律非法）
_VALID_RUNTIME_ID_RE = re.compile(r"^[1-9][0-9]*$")

INVALID_RUNTIME_ID = "invalid runtime_id"


def is_valid_runtime_id_param(raw) -> bool:
    """路径参数是否为正整数形态（字符串比较，避免 `0`/`00`/`012` 被 int 归一放行）。"""
    if raw is None:
        return False
    return bool(_VALID_RUNTIME_ID_RE.match(str(raw).strip()))


def invalid_runtime_id_response(raw) -> JSONResponse:
    """400 响应体：`error` 固定为 `invalid runtime_id`，带原始值与可读原因。"""
    return JSONResponse(
        status_code=400,
        content={
            "code": 400,
            "error": INVALID_RUNTIME_ID,
            "runtime_id": "" if raw is None else str(raw),
            "message": f"invalid runtime_id {raw!r}: must be a positive integer "
                       f"(runtime_id=0 usually means the worker failed to backfill its "
                       f"registered id; check worker registration, not credentials)",
        },
    )


class RuntimeIdGuardRoute(APIRoute):
    """路由类：先验 `runtime_id` 路径参数，非法直接 400（不进入端点/鉴权）。"""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guard_handler(request: Request):
            raw = request.path_params.get("runtime_id")
            if raw is not None and not is_valid_runtime_id_param(raw):
                return invalid_runtime_id_response(raw)
            return await original(request)

        return guard_handler