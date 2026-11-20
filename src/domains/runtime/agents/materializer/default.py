"""DefaultMaterializer——文件物化组合入口（Bridge 编排层）。

编排 skill + knowledge 物化，注入 sink。
不绑定具体 sink 类型，由调用方注入。

cloud_worker 注入 CloudSandboxSink，agent.py（未来）注入 LocalFileSink。

职责：只做编排，不含业务逻辑。
失败语义：skill 失败中断，knowledge 失败不中断。
"""

import logging

log = logging.getLogger(__name__)


class DefaultMaterializer:
    """文件物化组合入口——编排 skill + knowledge，注入 sink。

    Bridge 组合层：不绑定具体 sink 类型，由调用方注入。

    失败语义：
    - skill 降级全部失败 → 抛 SkillMaterializeError，调用方 catch 后中断任务
    - knowledge 失败 → log warning，不中断（best-effort）
    """

    def __init__(self, sink, env: dict, progress=None, client=None, tid: str = "", log_tag: str = ""):
        """
        Args:
            sink: FileSink 实例（Bridge 注入）
            env: 环境变量 dict（含 SKILL_OBS_URL 等）
            progress: async 回调 (stage: str, detail: str) -> None，用于日志上报
            client: grape client（只用于降级统计 report_messages，不用于进度日志）
            tid: 任务 ID
            log_tag: 日志前缀
        """
        from .skill import SkillMaterializer
        from .knowledge import KnowledgeMaterializer

        self._skill = SkillMaterializer(sink, env, progress, log_tag)
        self._knowledge = KnowledgeMaterializer(sink, client, tid, progress, log_tag)
        self._log_tag = log_tag

    async def materialize(self, task: dict, prompt: str) -> str:
        """编排 skill + knowledge 物化，返回替换后的 prompt。

        skill 失败则抛 SkillMaterializeError，knowledge 失败不中断。
        """
        from .skill import SkillMaterializeError

        # 1. skill 物化——失败则中断
        try:
            prompt = await self._skill.materialize(task, prompt)
        except SkillMaterializeError:
            raise  # 向上抛，调用方 catch 后 return error

        # 2. 知识物化——best-effort，失败不中断
        try:
            prompt = await self._knowledge.materialize(task, prompt)
        except Exception as e:
            log.warning(f"{self._log_tag}knowledge materialize failed (best-effort, continuing): {e}")

        return prompt
