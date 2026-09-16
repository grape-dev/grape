"""重生成 OpenAPI 契约（帮助 | api-docs-standardization-design.md P0 R1）。

把当前代码的对外安全路由契约落盘两份：
- help/api/openapi.json      （规范副本）
- src/api/static/openapi.json（固定下载 URL /static/openapi.json）

用法：
    python scripts/regenerate_openapi.py [--help-path PATH] [--static-path PATH]

不启动服务、不跑 lifespan，仅复用 custom_openapi 渲染，DB 走临时 sqlite 测试库。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DEVOPS_DB_PATH",
    os.path.join(tempfile.gettempdir(), "grape_openapi_regen.db"),
)
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from src.api.openapi import custom_openapi
from src.help_page_gen import write_openapi_contract
from src.api.main import app


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Regenerate OpenAPI contract artifacts")
    parser.add_argument("--help-path", default=None, help="overwrite help/api/openapi.json path")
    parser.add_argument("--static-path", default=None, help="overwrite static/openapi.json path")
    args = parser.parse_args()

    schema = custom_openapi(app)
    n = write_openapi_contract(schema, help_path=args.help_path, static_path=args.static_path)
    print(f"OpenAPI: contract dumped ({n} paths)")
    print("  -> help/api/openapi.json")
    print("  -> src/api/static/openapi.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())