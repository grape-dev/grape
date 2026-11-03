"""统一安全 JSON 序列化出口（Issue #684）。

所有 API 端点（含 git-repo 全部 `--json-out` 透传端点）经
`SafeJSONResponse`（main.py 注册为 default_response_class）序列化：

- 字符串值中的原始控制字符（U+0000–U+001F、U+007F）由 json.dumps 统一
  转义为合法 JSON 转义序列（\\n / \\u0001 …），下游 `json.loads` 无预处理
  直接解析成功；
- 孤立代理项（lone surrogate，如上游返回 "\\ud800"）会使
  `ensure_ascii=False` 的结果无法 encode 为 UTF-8（历史行为：500），
  render 阶段捕获后降级 `ensure_ascii=True` 重渲染（全量 \\uXXXX 转义），
  保证字节流合法且可解析——单一出口保证，不逐端点散修。
"""
import json

from fastapi.responses import JSONResponse


def dumps_safe(content) -> bytes:
    """统一序列化：优先紧凑 UTF-8；不可编码（孤立代理项等）时降级 ensure_ascii。

    Returns:
        bytes: 保证 `json.loads(bytes)` 可直接解析的响应体。
    """
    try:
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        # 孤立代理项等无法编码为 UTF-8：全量 \uXXXX 转义重渲染（合法 JSON、合法 UTF-8）
        return json.dumps(content, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")


def render_safe(content) -> bytes:
    """SafeJSONResponse.render 的实现入口（dumps_safe 别名，便于单测复用）。"""
    return dumps_safe(content)


class SafeJSONResponse(JSONResponse):
    """统一 JSON 响应类：控制字符转义 + 孤立代理项降级，输出恒可 json.loads。

    main.py 以 default_response_class 注册后，所有未显式指定 response_class
    的端点共用此出口（Issue #684 Spec：统一序列化出口，非逐端点补丁）。
    """

    def render(self, content) -> bytes:
        return render_safe(content)
