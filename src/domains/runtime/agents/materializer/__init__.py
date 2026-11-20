"""文件物化抽象——桥接模式（Bridge）。

物化逻辑（SkillMaterializer / KnowledgeMaterializer）× 写入方式（FileSink）两个正交维度独立变化。
切换环境时只换 FileSink 实现，Materializer 代码不动。

模块结构：
  protocol.py    — FileMaterializer + FileSink Protocol
  skill.py       — SkillMaterializer + SkillMaterializeError + MaterializeStage
  knowledge.py   — KnowledgeMaterializer + FileContentResolver
  sink_cloud.py  — CloudSandboxSink（当前实现）
  sink_local.py  — LocalFileSink（后续实现，替换 agent.py / event_pipeline）
  default.py     — DefaultMaterializer（组合入口，注入 sink）
"""

from .protocol import FileMaterializer, FileSink
from .skill import SkillMaterializer, SkillMaterializeError, MaterializeStage
from .knowledge import KnowledgeMaterializer, FileContentResolver
from .sink_cloud import CloudSandboxSink
from .default import DefaultMaterializer

__all__ = [
    "FileMaterializer", "FileSink",
    "SkillMaterializer", "SkillMaterializeError", "MaterializeStage",
    "KnowledgeMaterializer", "FileContentResolver",
    "CloudSandboxSink",
    "DefaultMaterializer",
]
