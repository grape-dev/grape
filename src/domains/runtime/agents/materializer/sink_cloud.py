"""CloudSandboxSink——云沙箱写入策略（Bridge 实现层）。

封装 sandbox.write_file / sandbox.upload_skill，
Materializer 不直接依赖 sandbox。

所有方法同步语义，Materializer 侧包 asyncio.to_thread。
"""

import logging

log = logging.getLogger(__name__)


class CloudSandboxSink:
    """云沙箱写入策略——通过 sandbox SDK 写入远端。

    Bridge 实现层：封装 sandbox.write_file / sandbox.upload_skill，
    Materializer 不直接依赖 sandbox。

    所有方法同步语义，Materializer 侧包 asyncio.to_thread。
    """

    def __init__(self, sandbox):
        """
        Args:
            sandbox: 已 __enter__ 的 CloudSandbox 实例
        """
        self._sandbox = sandbox

    @property
    def skill_dir(self) -> str:
        """skill 物化目标目录。sandbox git clone 失败时返回空串。"""
        return getattr(self._sandbox, 'skill_dir', '') or ''

    @property
    def repo_dir(self) -> str:
        """repo 根目录。"""
        return getattr(self._sandbox, 'repo_dir', '') or '/home/user/repo'

    def write_file(self, dest_dir: str, rel: str, content: str) -> None:
        """通过 sandbox.write_file 写入远端沙箱。

        SDK 自动建父目录（CubeSandbox files.write / DevBox files.write）。
        """
        _write = getattr(self._sandbox, 'write_file', None)
        if not _write:
            log.warning("[CloudSandboxSink] sandbox has no write_file, skip")
            return
        dest = f"{dest_dir}/{rel}"
        _write(dest, content)

    def upload_skill(self, obs_url: str) -> None:
        """通过 sandbox.upload_skill 下载 OBS zip 并解压到 skill_dir。"""
        _upload = getattr(self._sandbox, 'upload_skill', None)
        if not _upload:
            raise RuntimeError("sandbox has no upload_skill method")
        _upload(obs_url)
