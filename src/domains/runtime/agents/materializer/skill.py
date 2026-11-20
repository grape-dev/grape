"""SkillMaterializer——skill 物化 + {{SKILL_DIR}} 替换。

独立类，不依赖 KnowledgeMaterializer 或 FileContentResolver。
依赖 FileSink 接口（Bridge），不直接碰 sandbox。

降级链（OBS zip → skill_files 文本，是降级关系）：
① 有 OBS URL → sink.upload_skill 下载
   成功 → 结束
   失败 → 降级到 skill_files
② skill_files 文本写入 → sink.write_file
   成功 → 结束
   失败 → 抛 SkillMaterializeError（skill 是 agent 前置依赖）
③ 都没有（无 OBS URL 且无 skill_files）
   → 不物化，替换 {{SKILL_DIR}}，继续

日志上报通过 progress 回调（async），不直接依赖 client。
"""

import json
import asyncio
import logging

log = logging.getLogger(__name__)


class MaterializeStage:
    """物化阶段常量——不裸写字符串，防止拼写错误。

    progress 回调的 stage 参数用这些常量，不裸写字符串。
    """
    SKILL_UPLOADED = "skill_uploaded"
    SKILL_UPLOAD_FAIL = "skill_upload_fail"
    SKILL_MATERIALIZED = "skill_materialized"
    SKILL_MATERIALIZE_FAIL = "skill_materialize_fail"
    SKILL_FAIL = "skill_fail"
    PROJECT_INFO_MATERIALIZED = "project_info_materialized"
    KNOWLEDGE_MATERIALIZED = "knowledge_materialized"
    KNOWLEDGE_FAIL = "knowledge_fail"


class SkillMaterializeError(Exception):
    """skill 物化失败——降级链全部失败时抛出，cloud_worker catch 后中断任务。"""
    pass


class SkillMaterializer:
    """Skill 物化——OBS zip → skill_files 降级 + {{SKILL_DIR}} 替换。

    依赖 FileSink 接口（Bridge），不直接碰 sandbox。
    日志上报通过 progress 回调（async），不直接依赖 client。
    """

    def __init__(self, sink, env: dict, progress=None, log_tag: str = ""):
        """
        Args:
            sink: FileSink 实例（Bridge 注入）
            env: 环境变量 dict（取 SKILL_OBS_URL）
            progress: async 回调 (stage: str, detail: str) -> None，用于日志上报。
                      吞异常，不影响主流程。
            log_tag: 日志前缀（如 "|engine:hermes"）
        """
        self._sink = sink
        self._env = env
        self._progress_cb = progress
        self._log_tag = log_tag

    async def _progress(self, stage: str, detail: str = "") -> None:
        """日志上报——吞异常，不影响主流程。"""
        if not self._progress_cb:
            return
        try:
            await self._progress_cb(stage, detail)
        except Exception:
            pass

    async def materialize(self, task: dict, prompt: str) -> str:
        """物化 skill + 替换 {{SKILL_DIR}}，返回替换后的 prompt。

        降级全部失败时抛 SkillMaterializeError。
        """
        _skill_obs_url = self._env.get("SKILL_OBS_URL", "")
        _skill_files = task.get("skill_files", "")

        # ③ 都没有或空 → 不物化，直接替换占位符
        # 注意: skill_files 可能是 "{}"（空 JSON 字符串），not "{}" = False（非空字符串）
        # 需要解析判断是否真的有技能文件
        _has_skill_files = False
        if _skill_files:
            try:
                _sf_check = json.loads(_skill_files) if isinstance(_skill_files, str) else _skill_files
                _has_skill_files = bool(_sf_check) and any(bool(v) for v in _sf_check.values() if isinstance(v, dict))
            except Exception:
                _has_skill_files = bool(_skill_files)

        if not _skill_obs_url and not _has_skill_files:
            return self._replace_skill_dir(prompt)

        _materialized = False

        # ① OBS zip 直连
        if _skill_obs_url:
            try:
                await asyncio.to_thread(self._sink.upload_skill, _skill_obs_url)
                log.info(f"{self._log_tag}skill uploaded: url={_skill_obs_url[:60]}")
                await self._progress(MaterializeStage.SKILL_UPLOADED, f"url={_skill_obs_url[:60]}")
                _materialized = True
            except Exception as e:
                log.warning(f"{self._log_tag}skill OBS upload failed, fallback to skill_files: {e}")
                await self._progress(MaterializeStage.SKILL_UPLOAD_FAIL, f"err={e}")

        # ② skill_files 文本降级（OBS 失败或无 OBS URL 时）
        if not _materialized and _has_skill_files:
            try:
                _sf = json.loads(_skill_files)
                _skill_dir = self._sink.skill_dir
                if not _skill_dir:
                    _skill_dir = f"{self._sink.repo_dir}/.skills"
                _count = 0
                for _sn, files in _sf.items():
                    for rel, content in (files or {}).items():
                        try:
                            await asyncio.to_thread(
                                self._sink.write_file, _skill_dir, f"{_sn}/{rel}", content
                            )
                            _count += 1
                        except Exception as e:
                            log.warning(f"{self._log_tag}skill write_file failed: rel={rel}, err={e}")
                if _count > 0:
                    log.info(f"{self._log_tag}skill_files materialized: files={_count}")
                    await self._progress(MaterializeStage.SKILL_MATERIALIZED, f"files={_count}")
                    _materialized = True
                else:
                    log.warning(f"{self._log_tag}skill_files materialize produced 0 files")
                    await self._progress(MaterializeStage.SKILL_MATERIALIZE_FAIL, "0 files")
            except Exception as e:
                log.warning(f"{self._log_tag}skill_files materialize failed: {e}")
                await self._progress(MaterializeStage.SKILL_MATERIALIZE_FAIL, f"err={e}")

        # 降级全部失败 → 中断
        if not _materialized:
            await self._progress(MaterializeStage.SKILL_FAIL, "OBS + skill_files both failed")
            raise SkillMaterializeError(
                "skill materialize failed: OBS upload failed + skill_files fallback failed"
            )

        # 替换 {{SKILL_DIR}}
        return self._replace_skill_dir(prompt)

    def _replace_skill_dir(self, prompt: str) -> str:
        """替换 {{SKILL_DIR}} 占位符。"""
        _skill_dir = self._sink.skill_dir or '/home/user/repo/.skills'
        return prompt.replace("{{SKILL_DIR}}", _skill_dir)
