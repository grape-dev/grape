"""Native ClawHub uploader: concurrent per-file uploads (Plan B).

The official CLI (`clawhub skill publish`) uploads files one-by-one with two
serial HTTP requests per file (upload-url then binary upload), which makes
large skills painfully slow on high-latency links. This module implements the
same API contract but uploads multiple files concurrently.

Version control is preserved: the version is set explicitly in the final
`POST /api/v1/skills` request, exactly like `clawhub skill publish --version
X.Y.Z`. Concurrency only affects how file bytes are transported.

Content-idempotency probing (unchanged/new/update) is intentionally delegated
to the CLI's `--dry-run` (see clawhub.skill_probe): its bundle fingerprint is
computed with Node's ICU collation, which is not worth reimplementing. The
probe is a single lightweight request per skill; the bottleneck this module
fixes is the per-file serial upload.

API contract replicated from clawhub CLI v0.23.3 (dist/cli/commands/publish.js,
dist/schema/schemas.js, dist/http.js, dist/skills.js):
  - upload-url: POST {base}/api/v1/skills/-/upload-url
               body {path,size,sha256,contentType} -> {uploadUrl, uploadTicket}
  - binary:   POST <uploadUrl> (raw bytes) -> {storageId}
  - publish:  POST {base}/api/v1/skills
               body {slug,displayName,ownerHandle?,version,changelog,
                     acceptLicenseTerms,tags,files:[{path,size,storageId,sha256,
                     contentType,uploadTicket}]}
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Mirrors clawhub CLI limits (publish.js).
MAX_PUBLISH_FILE_BYTES = 10 * 1024 * 1024
MAX_PUBLISH_TOTAL_BYTES = 50 * 1024 * 1024
# Mirrors http.js UPLOAD_TIMEOUT_MS / REQUEST timeouts.
UPLOAD_TIMEOUT = 120
REQUEST_TIMEOUT = 60
CONNECT_TIMEOUT = 15
RETRY_COUNT = 2  # mirrors http.js RETRY_COUNT

# Directory / file names always skipped (mirrors listSkillFiles + stripGeneratedSkillCards).
SKIP_DIRS = {".git", "node_modules", ".claw", ".clawhub"}
SKIP_FILES = {"skill-card.md"}


class ClawhubApiError(Exception):
    """Raised on unrecoverable ClawHub API failures."""


# --------------------------------------------------------------------------- #
# auth + HTTP
# --------------------------------------------------------------------------- #

def _load_auth(config_path: str) -> Tuple[str, str]:
    """Read (registry, token) from the clawhub config JSON (same file the CLI
    reads via CLAWHUB_CONFIG_PATH)."""
    if not config_path or not os.path.isfile(config_path):
        raise ClawhubApiError(f"clawhub config not found: {config_path!r}")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    registry = (data.get("registry") or "https://clawhub.ai").rstrip("/")
    token = data.get("token") or ""
    if not token:
        raise ClawhubApiError(f"no token in clawhub config {config_path!r}")
    return registry, token


def _request(
    url: str,
    method: str = "GET",
    token: str = "",
    body: Optional[dict] = None,
    raw: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = RETRY_COUNT,
) -> Tuple[int, object]:
    """Perform an HTTP request, retrying transient failures like the CLI's
    runWithRetries. Returns (status, parsed_json_or_text)."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if raw is not None:
        data = raw
        if content_type:
            headers["Content-Type"] = content_type
    elif body is not None or method == "POST":
        data = json.dumps(body if body is not None else {}).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_resp = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            # HTTP error responses carry useful bodies (e.g. version conflicts).
            raw_resp = e.read()
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            continue
        try:
            return status, json.loads(raw_resp.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return status, raw_resp.decode("utf-8", errors="replace")
    raise ClawhubApiError(f"request failed after {retries + 1} attempts: {last_err}")


def _api(base: str, path: str, token: str, **kw) -> Tuple[int, object]:
    return _request(f"{base}{path}", token=token, **kw)


# --------------------------------------------------------------------------- #
# skill file listing (mirrors listSkillFiles)
# --------------------------------------------------------------------------- #

def _is_dot_segment(rel_path: str) -> bool:
    return any(seg.startswith(".") for seg in rel_path.split("/"))


def list_skill_files(folder) -> List[dict]:
    """List publishable files: relPath, size, sha256, contentType.

    Mirrors the CLI's listSkillFiles filtering: skip .git/ node_modules/ dot
    path segments and the generated skill-card.md. Limits mirror the CLI's
    MAX_PUBLISH_FILE_BYTES / MAX_PUBLISH_TOTAL_BYTES.
    """
    folder = Path(folder)
    entries: List[dict] = []
    total = 0
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        if any(seg in SKIP_DIRS for seg in rel.split("/")):
            continue
        if rel.lower() in SKIP_FILES:
            continue
        if _is_dot_segment(rel):
            continue
        size = p.stat().st_size
        if size > MAX_PUBLISH_FILE_BYTES:
            raise ClawhubApiError(
                f'File "{rel}" exceeds {MAX_PUBLISH_FILE_BYTES} bytes'
            )
        total += size
        if total > MAX_PUBLISH_TOTAL_BYTES:
            raise ClawhubApiError(
                f"Skill bundle exceeds {MAX_PUBLISH_TOTAL_BYTES} bytes"
            )
        entries.append({
            "relPath": rel,
            "size": size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "contentType": mimetypes.guess_type(rel)[0] or "application/octet-stream",
        })
    if not entries:
        raise ClawhubApiError("No files found")
    return entries


# --------------------------------------------------------------------------- #
# publish (concurrent uploads)
# --------------------------------------------------------------------------- #

def publish_native(folder, version: str, config_path: str = "",
                   concurrency: int = 6) -> dict:
    """Publish a skill with an explicit version using concurrent uploads.

    Content-idempotency must be checked by the caller (clawhub.skill_probe
    --dry-run) before invoking this; the probe uses the CLI's exact fingerprint
    so "already published" content is never re-uploaded.

    Returns the parsed publish response on success, or an error dict
    {"error": ..., "returncode": status} on failure — the same shape the
    gitsync self-heal loop expects from skill_publish(). Never raises for
    recoverable failures (config, file listing, uploads, publish request).
    """
    folder = Path(folder)
    try:
        registry, token = _load_auth(config_path)
        files = list_skill_files(folder)
    except ClawhubApiError as e:
        return {"error": str(e), "returncode": 1}
    slug = folder.name
    owner = _default_owner(registry, token)

    # 1) obtain upload-url + upload ticket for every file (concurrently)
    uploaded: List[dict] = []
    workers = max(1, min(int(concurrency), len(files)))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_upload_one, registry, token, f, folder / f["relPath"]): f
            for f in files
        }
        for fut in as_completed(futures):
            try:
                uploaded.append(fut.result())
            except ClawhubApiError as e:
                return {"error": str(e), "returncode": 1}
            done += 1
            # per-file progress (2026-08-16): lets operators see exactly where a
            # hung publish is stuck
            if done % 5 == 0 or done == len(files):
                print(
                    f"[native-publish] {slug}: {done}/{len(files)} files "
                    f"uploaded ({done * 100 // len(files)}%)",
                    flush=True,
                )

    # 2) final publish with explicit version (version control lives here)
    body = {
        "slug": slug,
        "displayName": _title_case(slug),
        "version": version,
        "changelog": "",
        "acceptLicenseTerms": True,
        "tags": ["latest"],
        "files": uploaded,
    }
    if owner:
        body["ownerHandle"] = owner
    pub_status, pub_data = _api(
        registry, "/api/v1/skills", token, method="POST", body=body,
        timeout=REQUEST_TIMEOUT,
    )
    if pub_status not in (200, 201):
        msg = _error_text(pub_data, pub_status)
        return {"error": msg, "returncode": pub_status}
    if not isinstance(pub_data, dict):
        return {"error": f"unexpected publish response: {pub_data}", "returncode": pub_status}
    return pub_data


def _upload_one(registry: str, token: str, f: dict, abs_path: Path) -> dict:
    """Two serial requests per file (mirrors publish.js): upload-url then
    binary upload. Runs concurrently across files via the caller's pool."""
    st, ticket = _api(
        registry, "/api/v1/skills/-/upload-url", token, method="POST",
        body={
            "path": f["relPath"],
            "size": f["size"],
            "sha256": f["sha256"],
            "contentType": f["contentType"],
        },
        timeout=REQUEST_TIMEOUT,
    )
    if st != 200 or not isinstance(ticket, dict):
        raise ClawhubApiError(
            f"upload-url failed for {f['relPath']}: {_error_text(ticket, st)}"
        )
    upload_url = ticket.get("uploadUrl")
    upload_ticket = ticket.get("uploadTicket")
    if not upload_url:
        raise ClawhubApiError(f"no uploadUrl for {f['relPath']}")

    st2, storage = _request(
        upload_url, "POST", token=token, raw=abs_path.read_bytes(),
        content_type=f["contentType"], timeout=UPLOAD_TIMEOUT,
    )
    if st2 != 200 or not isinstance(storage, dict):
        raise ClawhubApiError(
            f"binary upload failed for {f['relPath']}: {_error_text(storage, st2)}"
        )
    return {
        "path": f["relPath"],
        "size": f["size"],
        "storageId": storage.get("storageId"),
        "sha256": f["sha256"],
        "contentType": f["contentType"],
        "uploadTicket": upload_ticket,
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _default_owner(registry: str, token: str) -> str:
    """Mirror resolveDefaultOwnerHandle: GET /api/v1/whoami -> user.handle."""
    try:
        status, data = _api(registry, "/api/v1/whoami", token)
        if status == 200 and isinstance(data, dict):
            user = data.get("user") or {}
            handle = str(user.get("handle") or "").strip().lstrip("@")
            if handle:
                return handle
    except ClawhubApiError:
        pass
    return ""


def _error_text(data: object, status: int) -> str:
    if isinstance(data, dict):
        msg = data.get("error") or data.get("message") or data.get("detail")
        if msg:
            return f"HTTP {status}: {msg}"
        return f"HTTP {status}: {json.dumps(data)[:200]}"
    return f"HTTP {status}: {str(data)[:200]}"


def _title_case(slug: str) -> str:
    """Mirror the CLI's titleCase(basename) for displayName."""
    words = [w for w in slug.replace("_", " ").replace("-", " ").split(" ") if w]
    return " ".join(w[0].upper() + w[1:] for w in words) if words else slug
