"""验证 worker 包包含华为云 MCP 插件文件。

回归保护：worker_package.py MANIFEST 必须包含 .codeartsdoer/huaweicloud-plugins/
和 .codeartsdoer/mcp/，确保远端 worker 安装后有 MCP server 可用。
"""
import subprocess
import os

WORKER_PKG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "cache", "worker-package", "grape-worker.tar.gz"
)


def _list_package_files():
    """Return list of files in worker package tarball."""
    if not os.path.isfile(WORKER_PKG):
        return []
    r = subprocess.run(
        ["tar", "tzf", WORKER_PKG],
        capture_output=True, text=True, timeout=30,
    )
    return r.stdout.strip().split("\n") if r.stdout.strip() else []


class TestWorkerPackageContainsMCP:
    """Worker 包必须包含 MCP 插件文件。"""

    def test_package_exists(self):
        assert os.path.isfile(WORKER_PKG), f"Worker package not found: {WORKER_PKG}"

    def test_contains_mcp_server(self):
        files = _list_package_files()
        assert any("mcp-server.mjs" in f for f in files), \
            "mcp-server.mjs not found in worker package"

    def test_contains_tools_mjs(self):
        files = _list_package_files()
        assert any("tools.mjs" in f and "codeartsdoer" in f for f in files), \
            "tools.mjs not found in worker package"

    def test_contains_mcp_settings(self):
        files = _list_package_files()
        assert any("mcp_settings.json" in f for f in files), \
            "mcp_settings.json not found in worker package"

    def test_contains_huaweicloud_plugins_dir(self):
        files = _list_package_files()
        codearts_files = [f for f in files if "codeartsdoer" in f]
        assert len(codearts_files) >= 20, \
            f"Expected >=20 codeartsdoer files, got {len(codearts_files)}"

    def test_no_node_modules_in_package(self):
        files = _list_package_files()
        nm = [f for f in files if "node_modules" in f]
        assert len(nm) == 0, f"node_modules found in package: {nm[:3]}"
