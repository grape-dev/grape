"""插件市场路由：L0 校验发布门禁（Issue #1079，总纲 #1078 A1）。

发布链路接入点：上传/上架前强校验 plugin.json 与插件包。
- POST /api/plugin-market/validate          manifest JSON 门禁（schema 结构校验 + 可选探测）
- POST /api/plugin-market/validate-package  插件包 zip 门禁（schema + 禁二进制 + 可选探测）
拒绝时返回结构化错误清单（check/code/path/reason/hint/severity）。
"""
import io
import os
import tempfile
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key
from src.domains.mgmt.plugin_market.validator import validate_for_publish, validate_manifest

router = APIRouter(prefix="/api/plugin-market", tags=["plugin-market"])

# 插件包 zip 大小上限（压缩包本身；与 skills 上传同量级）
MAX_PACKAGE_BYTES = int(os.environ.get("PLUGIN_MAX_PACKAGE_BYTES", str(50 * 1024 * 1024)))
# zip 炸弹防护：解压后总大小上限（复用 skills 上传 MAX_EXTRACT_BYTES 同模式，解压前校验不落盘）
MAX_EXTRACT_BYTES = int(os.environ.get("PLUGIN_MAX_EXTRACT_BYTES", str(100 * 1024 * 1024)))
# 上传流分块读取块大小：累计超限立即中断，避免全量缓冲超大包
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class ManifestValidateBody(BaseModel):
    manifest: dict
    probe: bool = False


# 桶B B4：显式 OID（auto 生成含 api_ 被 #1259 校验拒）
@router.post("/validate", operation_id="plugin_market_validate")
def validate_plugin(body: ManifestValidateBody, _user: dict = Depends(get_current_user_or_api_key)):
    """上架前 manifest 门禁：schema 结构校验；probe=true 时附带免凭据命令探测。

    直接复用校验器公开入口 validate_manifest（探测 exec 白名单强制），
    路由层不自行拼装报告/反 import 校验器私有件。
    """
    return validate_manifest(body.manifest, probe=body.probe)


def _locate_plugin_dir(root: str) -> str:
    """定位含 plugin.json 的目录：根目录优先，其次唯一一级子目录（zip 外层包了目录）。"""
    if os.path.isfile(os.path.join(root, "plugin.json")):
        return root
    entries = [e for e in os.listdir(root) if not e.startswith("__")]
    dirs = [e for e in entries if os.path.isdir(os.path.join(root, e))]
    if len(dirs) == 1:
        candidate = os.path.join(root, dirs[0])
        if os.path.isfile(os.path.join(candidate, "plugin.json")):
            return candidate
    return root


def _read_upload_limited(file: UploadFile, limit: int) -> bytes:
    """分块读取上传流：累计超过 limit 立即 413 中断，不全量缓冲超大包。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail={
                "code": 413, "message": f"插件包超过大小上限 {limit} 字节"})
        chunks.append(chunk)
    return b"".join(chunks)


# 桶B B4：显式 OID
@router.post("/validate-package", operation_id="plugin_market_validate_package")
def validate_plugin_package(
    file: UploadFile = File(...),
    probe: bool = Form(False),
    _user: dict = Depends(get_current_user_or_api_key),
):
    """上传门禁：插件包 zip 解包后做 schema + 禁二进制（+ 可选探测）。

    同步 def 路由（FastAPI 自动进线程池）：zip 校验/解压/子进程探测均为阻塞
    操作，不占用事件循环；上传流分块读取，累计超限立即 413，不全量缓冲。
    """
    content = _read_upload_limited(file, MAX_PACKAGE_BYTES)
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        if zf.testzip() is not None:
            raise zipfile.BadZipFile("zip 内存在损坏条目")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "非法 zip 包：无法解压，请检查打包产物"})
    with tempfile.TemporaryDirectory(prefix="plugin-validate-") as td:
        total = sum((info.file_size or 0) for info in zf.infolist())
        if total > MAX_EXTRACT_BYTES:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"ZIP 解压后总大小超过 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB 上限"})
        zf.extractall(td)  # ZipFile.extract 对路径穿越条目做了净化
        return validate_for_publish(_locate_plugin_dir(td), probe=probe)
