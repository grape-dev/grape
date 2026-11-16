"""KnowledgeMaterializer + FileContentResolver——知识物化 + 降级链。

KnowledgeMaterializer：项目知识 + 共享知识物化 + 占位符替换。
  独立类，不依赖 SkillMaterializer。
  依赖 FileSink 接口（Bridge），不直接碰 sandbox。
  永远不抛异常（best-effort）。

FileContentResolver：文件内容解析降级链——纯内容获取，不碰文件系统。
  仅被 KnowledgeMaterializer 使用。SkillMaterializer 不引用它。
  三级降级：URL 下载 → content → storage_key 直连。
"""

import json
import asyncio
import logging

log = logging.getLogger(__name__)


class FileContentResolver:
    """文件内容解析降级链——纯内容获取，不碰文件系统。

    三级降级（职责链模式）：
    ① UrlHandler：file_urls 有 url → urllib HTTP GET（timeout=10s）
    ② ContentHandler：files_dict 有 content → 直接用内联内容
    ③ StorageHandler：file_urls 有 storage_key → storage.read_batch

    只返回 {rel: content}，写入由 KnowledgeMaterializer 通过 sink 负责。
    """

    @staticmethod
    async def resolve_async(files_dict: dict, file_urls: dict,
                            client=None, tid: str = "", tag: str = "") -> dict:
        """异步解析——返回 {rel: content_str}。

        三级降级：URL 下载 → content → storage_key 直连。
        """
        import urllib.request as _urllib

        if not files_dict and not file_urls:
            return {}

        result = {}
        need_storage = {}
        stats = {"url_fail": 0, "storage_fail": 0, "empty": 0}

        for rel, content in (files_dict or {}).items():
            if rel == ".metadata.json":
                continue

            meta = file_urls.get(rel, {}) if file_urls else {}
            url = meta.get("url", "")
            got_content = None

            # ① URL 下载
            if url:
                try:
                    def _fetch(u):
                        with _urllib.urlopen(u, timeout=10) as resp:
                            raw = resp.read()
                        return raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")
                    got_content = await asyncio.to_thread(_fetch, url)
                    if not got_content:
                        raise ValueError("empty response")
                except Exception as e:
                    log.warning(f"[{tag}] url download failed: rel={rel}, err={type(e).__name__}")
                    stats["url_fail"] += 1
                    got_content = None

            # ② content 降级
            if got_content is None and content:
                got_content = content

            # ③ 收集 storage_key
            if got_content is None:
                storage_key = meta.get("storage_key", "")
                if storage_key:
                    need_storage[storage_key] = rel
                else:
                    stats["empty"] += 1
                    log.error(f"[{tag}] all fallback failed, empty content: rel={rel}")
                continue

            result[rel] = got_content

        # ③ storage_key 批量读取
        if need_storage:
            result.update(
                await FileContentResolver._read_storage_async(need_storage, stats, tag)
            )

        # 降级统计上报
        FileContentResolver._report_degradation(stats, client, tid, tag)

        return result

    @staticmethod
    async def _read_storage_async(need_storage: dict, stats: dict, tag: str) -> dict:
        """storage 批量读取。"""
        try:
            from src.domains.runtime.storage import get_storage
            storage = get_storage()
        except Exception:
            storage = None

        if storage is None:
            for key, rel in need_storage.items():
                stats["storage_fail"] += 1
                log.error(f"[{tag}] storage unavailable, read failed: rel={rel}, key={key}")
            return {}

        keys = list(need_storage.keys())
        results = await storage.read_batch(keys)
        log.info(f"[{tag}] batch read: {sum(1 for v in results.values() if v)}/{len(keys)} succeeded")

        out = {}
        for key, rel in need_storage.items():
            content = results.get(key, "")
            if content:
                out[rel] = content
            else:
                stats["storage_fail"] += 1
                log.error(f"[{tag}] storage read failed: rel={rel}, key={key}")
        return out

    @staticmethod
    def _report_degradation(stats: dict, client, tid: str, tag: str):
        """降级统计上报（保留现有 client.report_messages 逻辑）。

        client 只用于业务消息上报（降级统计），不用于进度日志。
        边界：progress 回调 = 日志，client.report_messages = 业务消息，互不替代。
        """
        total = stats["url_fail"] + stats["storage_fail"] + stats["empty"]
        if total > 0:
            log.warning(f"[{tag}] degraded: url_fail={stats['url_fail']}, "
                        f"storage_fail={stats['storage_fail']}, empty={stats['empty']}")
            if client and tid:
                try:
                    asyncio.create_task(client.report_messages(tid, [{
                        "type": "text",
                        "data": f"⚠️ {tag} 文件降级：URL下载失败 {stats['url_fail']} 个，"
                                f"存储直连失败 {stats['storage_fail']} 个，空内容 {stats['empty']} 个"
                    }]))
                except Exception:
                    pass


class KnowledgeMaterializer:
    """项目知识 + 共享知识物化 + 占位符替换。

    独立类，不依赖 SkillMaterializer。
    依赖 FileSink 接口（Bridge），不直接碰 sandbox。

    物化目标：
    - project_info_files → sink.repo_dir/.project-info/
    - knowledge_files → sink.repo_dir/.knowledge/

    占位符替换在 materialize() 内部完成：
    - {{PROJECT_INFO_DIR}} → repo_dir/.project-info
    - {{KNOWLEDGE_DIR}} → repo_dir/.knowledge

    无论物化是否成功，都替换占位符。
    永远不抛异常（best-effort）。

    日志上报通过 progress 回调（async），不直接依赖 client。
    client 只用于降级统计 report_messages。
    """

    def __init__(self, sink, client=None, tid: str = "", progress=None, log_tag: str = ""):
        """
        Args:
            sink: FileSink 实例（Bridge 注入）
            client: grape client（只用于降级统计 report_messages，不用于进度日志）
            tid: 任务 ID
            progress: async 回调 (stage: str, detail: str) -> None，用于日志上报。
                      吞异常，不影响主流程。
            log_tag: 日志前缀
        """
        self._sink = sink
        self._client = client
        self._tid = tid
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
        """物化知识 + 替换占位符，返回替换后的 prompt。永远不抛异常。"""
        from .skill import MaterializeStage

        repo_dir = self._sink.repo_dir

        # project-info（baseline + member）
        proj_dir = f"{repo_dir}/.project-info"
        try:
            proj_files = json.loads(task.get("project_info_files") or "{}")
            proj_urls = json.loads(task.get("project_info_file_urls") or "{}")
            await self._write_files_async(proj_files, proj_urls, proj_dir, tag="project-info")
            await self._progress(MaterializeStage.PROJECT_INFO_MATERIALIZED, f"dir={proj_dir}")
        except Exception as e:
            log.warning(f"{self._log_tag}project-info materialize failed: {e}")
            await self._progress(MaterializeStage.KNOWLEDGE_FAIL, f"project-info: {e}")
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)

        # knowledge（agent 沉淀）
        know_dir = f"{repo_dir}/.knowledge"
        try:
            know_files = json.loads(task.get("knowledge_files") or "{}")
            know_urls = json.loads(task.get("knowledge_file_urls") or "{}")
            await self._write_files_async(know_files, know_urls, know_dir, tag="knowledge")
            await self._progress(MaterializeStage.KNOWLEDGE_MATERIALIZED, f"dir={know_dir}")
        except Exception as e:
            log.warning(f"{self._log_tag}knowledge materialize failed: {e}")
            await self._progress(MaterializeStage.KNOWLEDGE_FAIL, f"knowledge: {e}")
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)

        return prompt

    async def _write_files_async(self, files_dict, file_urls, dest_dir, tag):
        """降级链获取内容 + sink.write_file 异步写入。"""
        contents = await FileContentResolver.resolve_async(
            files_dict, file_urls,
            client=self._client, tid=self._tid, tag=tag
        )

        _count = 0
        for rel, content in contents.items():
            try:
                await asyncio.to_thread(self._sink.write_file, dest_dir, rel, content)
                _count += 1
            except Exception as e:
                log.warning(f"{self._log_tag}[{tag}] write_file failed: rel={rel}, err={e}")

        if _count > 0:
            log.info(f"{self._log_tag}{tag} materialized: files={_count}")
