"""文件物化 + 文件写入策略 Protocol 定义（Bridge 抽象层 + 实现层接口）。

设计原则：
- FileMaterializer 约束输出语义（物化文件 + 替换占位符 + 返回 prompt），不约束输入
- FileSink 约束写入能力（write_file / upload_skill / skill_dir / repo_dir）
- 两者正交——Materializer 不碰 IO，Sink 不碰业务逻辑
"""

from __future__ import annotations
from typing import Protocol


class FileSink(Protocol):
    """文件写入策略接口——Bridge 实现层。

    Materializer 只依赖此接口，不直接碰 sandbox 或 open(w)。
    切换环境时只换 FileSink 实现，Materializer 代码不动。

    同步语义：所有方法都是同步的。
    Materializer 侧负责包 asyncio.to_thread。
    """

    @property
    def skill_dir(self) -> str:
        """skill 物化目标目录。sandbox git clone 失败时返回空串。"""
        ...

    @property
    def repo_dir(self) -> str:
        """repo 根目录（知识物化的父目录）。"""
        ...

    def write_file(self, dest_dir: str, rel: str, content: str) -> None:
        """写文本文件到 dest_dir/rel。自动建父目录。

        Args:
            dest_dir: 目标目录绝对路径（由 Materializer 用 sink property 拼接）
            rel: 相对路径（如 "project-graph.md" 或 "skill_name/SKILL.md"）
            content: 文本内容
        """
        ...

    def upload_skill(self, obs_url: str) -> None:
        """OBS zip 下载解压到 skill_dir。

        CloudSandboxSink: sandbox.upload_skill（SDK gRPC）
        LocalFileSink: _fetch_skill_zip + zipfile 解压 + 权限恢复
        """
        ...


class FileMaterializer(Protocol):
    """文件物化顶层接口——约束输出语义，不约束输入。

    输出契约：
    - 物化文件到执行环境（本地文件系统 or 远端沙箱）
    - 替换各自负责的 prompt 占位符
    - 返回替换后的 prompt

    输入差异由各实现类的构造函数解决，不走统一 ctx。

    子领域实现（互不依赖）：
    - SkillMaterializer：skill 物化 + {{SKILL_DIR}} 替换
    - KnowledgeMaterializer：知识物化 + {{PROJECT_INFO_DIR}} / {{KNOWLEDGE_DIR}} 替换

    组合入口：
    - DefaultMaterializer：编排 skill + knowledge，注入 sink
    """

    async def materialize(self, task: dict, prompt: str) -> str:
        """物化文件 + 替换占位符，返回替换后的 prompt。"""
        ...
