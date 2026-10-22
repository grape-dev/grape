# R31: Skill Sync & Scene Sync Merge — Design Document

> **Requirement**: R31 — Merge skill sync and scene sync into a single serial scheduled task, eliminating dependency race conditions and avoiding duplicate ZIP downloads.
>
> **Branch**: `feat/skill-scene-sync-merge`
>
> **Baseline**: `weekly-08` (rebased onto head c100ddd31; docs-only addition, no merge conflict)
>
> **Files affected**: `src/storage/skill_sync.py`, `src/storage/scene_sync.py`, `src/api/main.py`
>
> **Status**: Design — pending review before implementation.

---

## 1. Background

### 1.1 Current Architecture

Two independent asyncio scheduled tasks run on a 30-minute interval, each launched separately during application startup:

```
main.py:207  start_skill_sync_scheduler()  →  _skill_sync_worker  (every 1800s)
main.py:214  start_scene_sync_scheduler()   →  _scene_sync_worker   (every 1800s)
```

#### Skill sync flow (`skill_sync.py:214` — `sync_official_skills`)

| Step | Description | Depends on |
|------|-------------|------------|
| 1 | Download grape-skills repo ZIP | — |
| 2 | Merge `skill-description.json` | Step 1 |
| 3 | Sync skill directories (hash-based incremental copy) | Step 1 |
| 4 | DB sync (INSERT/UPDATE `skill` table) | Step 2 |
| 5 | OBS sync (hash-based incremental upload) | Steps 3, 4 |

#### Scene sync flow (`scene_sync.py:303` — `sync_official_scenes`)

| Step | Description | Depends on |
|------|-------------|------------|
| 1 | Download grape-skills repo ZIP (same repo) | — |
| 2 | Traverse `scenes/` directory, read `scene.json` | Step 1 |
| 3 | Validate scene nodes | Step 2 |
| 4 | **Pre-check skill references**: `_check_skills_exist()` queries `skill` table | **Skill sync step 4 completed** |
| 5 | Skill not found → skip scene (`stats["failed"] += 1`) | — |
| 6 | Skill found → INSERT/UPDATE `scene_market` | Step 4 passed |
| 7 | Rebuild `scene_skill_ref` (query `skill_id` and write association) | Skill sync step 4 completed |

### 1.2 Current Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         main.py (lifespan)                          │
│                                                                     │
│  ┌──────────────────────────┐    ┌────────────────────────────────┐ │
│  │  start_skill_sync_scheduler│    │  start_scene_sync_scheduler   │ │
│  │         (line 207)        │    │         (line 214)            │ │
│  └─────────────┬────────────┘    └──────────────┬─────────────────┘ │
│                │                                 │                   │
│                ▼                                 ▼                   │
│  ┌──────────────────────────┐    ┌────────────────────────────────┐ │
│  │   _skill_sync_worker     │    │    _scene_sync_worker          │ │
│  │   lock: "skill_sync"     │    │    lock: "scene_sync"          │ │
│  └─────────────┬────────────┘    └──────────────┬─────────────────┘ │
│                │                                 │                   │
│                ▼                                 ▼                   │
│  ┌──────────────────────────┐    ┌────────────────────────────────┐ │
│  │  sync_official_skills()  │    │  sync_official_scenes()        │ │
│  │                          │    │                                │ │
│  │  1. Download ZIP ─────────┼──┐ │  1. Download ZIP ──────────────┼─┤ ← duplicate download
│  │  2. Merge JSON           │  │ │  2. Traverse scenes/           │ │
│  │  3. Copy skill dirs      │  │ │  3. Validate nodes             │ │
│  │  4. DB sync (skill)      │  │ │  4. _check_skills_exist() ─────┼─┤ ← race: skill table
│  │  5. OBS sync             │  │ │  5. INSERT/UPDATE scene_market │ │    may be empty
│  │                          │  │ │  6. Rebuild scene_skill_ref    │ │
│  │  finally: cleanup tmp    │  │ │  finally: cleanup tmp          │ │
│  └──────────────────────────┘  │ └────────────────────────────────┘ │
│                                │                                     │
│                    same ZIP downloaded twice                        │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 Dependency Relationship

Scene sync steps 4–7 directly depend on skill sync step 4 (skill table DB sync being complete):

```
scene_sync._check_skills_exist()       (scene_sync.py:154)
  → SELECT skill_name FROM skill WHERE skill_name IN (...)
  → Depends on skill table having records
  → Depends on skill_sync._sync_skills_to_db() having executed  (skill_sync.py:64)

scene_sync._rebuild_scene_skill_ref()  (scene_sync.py:238)
  → SELECT id FROM skill WHERE skill_name=? AND is_active=1
  → Depends on skill table having skill_id records
  → Depends on skill_sync._sync_skills_to_db() having executed
```

### 1.4 Problems

#### Problem 1: Dependency race condition

Since the two workers run concurrently, there is no ordering guarantee between skill DB sync and scene skill pre-check. If scene sync's `_check_skills_exist` executes before skill sync's `_sync_skills_to_db`:

- All referenced skills are not found in the `skill` table.
- All scenes are marked `stats["failed"] += 1` → **all skipped**.
- `scene_market` table remains empty.
- Users see a blank scene market page.
- The next 30-minute cycle fixes it (skills are now in the DB).

**First startup scenario**: Both tables are empty. Both workers start simultaneously. Scene sync is highly likely to check skills before skill sync finishes DB insertion → 100% scene skip rate on first cycle.

#### Problem 2: Duplicate ZIP download

Both workers call the same `_download_repo_zip` function (defined in `scene_sync.py:202`, imported by `skill_sync.py:221`) to download the same grape-skills repository ZIP from the same URL. Since the two workers run as separate asyncio tasks on independent 30-minute intervals, each calls `_download_repo_zip` independently, resulting in two separate downloads of the same file:

```
skill_sync:  _download_repo_zip(client, token)  →  downloads https://raw.gitcode.com/<ORG>/<SKILLS_REPO>/archive/refs/heads/main.zip
scene_sync:  _download_repo_zip(client, token)  →  downloads the same URL
```

Measured (2026-10-22): the grape-skills ZIP is **1.97 MB** (768 entries, anonymous 302→OBS download works — token is not a hard prerequisite). Each cycle downloads ~4 MB instead of ~2 MB. Over 48 daily cycles, this wastes ~96 MB/day of bandwidth and (pre-P45-fix) leaks ~96 MB/day of temp directories. Earlier drafts of this document assumed a 50 MB ZIP and claimed 2.4 GB/day — that estimate was ~25× too high and is corrected here.

#### Problem 3: Redundant distributed lock

Two separate distributed locks are acquired:

```
skill_sync:  run_with_distributed_lock("skill_sync", ttl=1800)
scene_sync:  run_with_distributed_lock("scene_sync", ttl=1800)
```

After merging, only one lock is needed since scene sync runs inside the skill sync lock scope.

---

## 2. Proposed Architecture

### 2.1 Optimized Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         main.py (lifespan)                          │
│                                                                     │
│  ┌──────────────────────────┐                                      │
│  │  start_skill_sync_scheduler│                                      │
│  │         (line 207)        │                                      │
│  └─────────────┬────────────┘                                      │
│                │                                                    │
│                ▼                                                    │
│  ┌──────────────────────────┐                                      │
│  │   _skill_sync_worker     │                                      │
│  │   lock: "skill_sync"     │   ← only one distributed lock        │
│  └─────────────┬────────────┘                                      │
│                │                                                    │
│                ▼                                                    │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  sync_official_skills()                                      │   │
│  │                                                              │   │
│  │  1. Download ZIP (once)                                      │   │
│  │  2. Merge JSON                                               │   │
│  │  3. Copy skill dirs                                          │   │
│  │  4. DB sync (skill table)                                    │   │
│  │  5. OBS sync                                                 │   │
│  │  ─────────────────────────────────────────────────────       │   │
│  │  6. Call sync_official_scenes(repo_root=<existing path>)     │   │
│  │     │  → Traverse scenes/                                    │   │
│  │     │  → Validate nodes                                      │   │
│  │     │  → _check_skills_exist() ← skill table already ready   │   │
│  │     │  → INSERT/UPDATE scene_market                          │   │
│  │     │  → Rebuild scene_skill_ref                             │   │
│  │     │  → No cleanup (repo_root owned by caller)              │   │
│  │  ─────────────────────────────────────────────────────       │   │
│  │  7. Cleanup tmp dir (after scene sync completes)             │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  start_scene_sync_scheduler()  ← REMOVED (scene sync via skill_sync)│
│                                                                     │
│  _scene_sync_worker / start_scene_sync_scheduler  ← DELETED (decision A)│
│  single merged worker: skill sync → serial scene sync               │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Execution Flow Summary

```
_skill_sync_worker (every 30 min)
  │
  ├── acquire distributed lock "skill_sync"
  │
  ├── sync_official_skills()
  │     ├── 1. Download ZIP → repo_root
  │     ├── 2. Merge skill-description.json
  │     ├── 3. Sync skill directories (hash incremental copy)
  │     ├── 4. DB sync (_sync_skills_to_db)
  │     ├── 5. OBS sync (sync_official_skills_to_obs)
  │     ├── 6. sync_official_scenes(repo_root=repo_root)  ← SERIAL: skill table ready
  │     └── 7. Cleanup tmp dir (repo_root parent)
  │
  ├── release lock
  │
  └── sleep(interval)
```

---

## 3. Detailed Design: `skill_sync.py` Changes

### 3.1 Temp Directory Lifecycle Extension

**Current behavior** (`skill_sync.py:214–339`):

```python
async def sync_official_skills() -> dict:
    tmp_dir = None
    try:
        # ... download ZIP, merge JSON, copy dirs, DB sync, OBS sync ...
        tmp_dir = repo_root
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)  # ← cleanup immediately
```

The temp directory is cleaned in the `finally` block before the function returns. This prevents passing `repo_root` to scene sync.

**Proposed behavior**:

> **NOTE**: the pseudocode below shows the step-5 bare cleanup for readability; the AUTHORITATIVE structure is the whole-body try/finally of §3.5 — do not implement the bare form.

The `finally` block cleanup is **deferred** — the temp directory is kept alive through the scene sync call and only cleaned after scene sync completes (or fails).

```python
async def sync_official_skills() -> dict:
    import httpx
    from src.storage.scene_sync import _download_repo_zip, _get_sync_token

    result = {...}
    repo_root = None  # path to the extracted ZIP root directory

    # 1. Remote sync: download ZIP → merge JSON → copy skill directories
    try:
        token = _get_sync_token()
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                repo_root = await _download_repo_zip(client, token)
            except Exception as e:
                logger.warning(f"skill sync: download repo zip failed: {e}")
                repo_root = None

        if repo_root:
            # ... merge JSON, sync skill directories, acquire_lock, etc. ...
            # (existing logic unchanged — operates on repo_root)
    except Exception as e:
        logger.exception(f"skill sync: remote sync failed: {e}")

    # 2. DB sync
    try:
        # ... _sync_skills_to_db(...) ... (unchanged)
    except Exception as e:
        logger.exception(f"skill sync: DB sync failed: {e}")

    # 3. OBS sync
    try:
        obs_result = await sync_official_skills_to_obs()
        # ... (unchanged)
    except Exception as e:
        logger.exception(f"skill sync: OBS sync failed: {e}")

    # 4. Serial scene sync — reuse repo_root, skill table already populated
    try:
        from src.storage.scene_sync import sync_official_scenes
        await sync_official_scenes(repo_root=repo_root)
    except Exception as e:
        logger.exception(f"skill sync: scene sync failed: {e}")

    # 5. Cleanup temp directory (after scene sync is done)
    if repo_root:
        tmp_dir = os.path.dirname(repo_root)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return result
```

**Key changes**:

| Aspect | Before | After |
|--------|--------|-------|
| `finally` block | Cleans `tmp_dir` immediately | Removed — cleanup moved to step 5 |
| Scene sync | Not called | Called as step 4 with `repo_root` |
| Cleanup location | `finally` block (line 318–319) | Explicit call after scene sync (new step 5) |
| `tmp_dir` variable | `tmp_dir = repo_root` in `finally` | `repo_root` tracked directly; `tmp_dir = os.path.dirname(repo_root)` at cleanup |

### 3.2 Serial Call Position

The `sync_official_scenes` call is placed **after** steps 2 (DB sync) and 3 (OBS sync), ensuring the `skill` table is fully populated before scene sync checks skill references.

This ordering guarantee is the core fix for the dependency race condition described in §1.4.

### 3.3 Error Isolation

> **NOTE**: the try/finally shown here covers only steps 4–5; the AUTHORITATIVE form is the whole-body try/finally of §3.5. This section is retained for the error-isolation rationale only.

Scene sync failure does not affect skill sync or the next cycle. **Cleanup must be in a `finally` block** to guarantee temp directory cleanup even if scene sync or logging throws an unexpected exception:

```python
# 4. Serial scene sync + 5. Cleanup — wrapped in try/finally for exception safety
try:
    try:
        from src.storage.scene_sync import sync_official_scenes
        await sync_official_scenes(repo_root=repo_root)
    except Exception as e:
        logger.exception(f"skill sync: scene sync failed: {e}")
finally:
    # Cleanup happens regardless — even if scene sync or logging throws.
    # Without finally, an unexpected exception between step 4 and step 5
    # would leak the temp directory (same class of bug as P45).
    if repo_root:
        tmp_dir = os.path.dirname(repo_root)
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

> **Exception safety**: The original design had cleanup after the try/except block (not in finally). If `logger.exception` itself throws (rare but possible — e.g., logging handler disk full), or if an unexpected exception occurs between the except block and the cleanup line, `repo_root` would leak. The `try/finally` wrapper guarantees cleanup in all exit paths.
>
> **Scenario**: skill sync downloads ZIP (repo_root=/tmp/tmpXXXX) → DB sync → OBS sync → scene sync raises `RuntimeError` → `logger.exception` raises `IOError` (disk full) → cleanup line never reached → /tmp/tmpXXXX leaks. With try/finally: cleanup executes regardless of exception type.

If skill sync's remote download fails (`repo_root is None`), scene sync is still called with `repo_root=None`, which triggers scene sync's own download as a fallback. This preserves the "scene sync still works if skill download failed" behavior from the verification checklist (§9.5.1 of the requirements).

### 3.5 Whole-Body try/finally (audit fix)

The §3.3 try/finally only covers steps 4–5. If an except handler in steps 1–3 itself raises (e.g. `logger.exception` fails on a full disk), `repo_root` leaks — the same bug class as P45. Correct structure: ONE try/finally around the whole function body, with the per-step try/except kept inside:

```python
async def sync_official_skills() -> dict:
    result = {...}
    repo_root = None
    try:
        # steps 1–3 unchanged (each with its own try/except for error isolation)
        ...
        # step 4: serial scene sync (own try/except)
        ...
    finally:
        # ONE cleanup for every exit path, including failing except handlers
        if repo_root:
            shutil.rmtree(os.path.dirname(repo_root), ignore_errors=True)
    return result
```

The per-step `try/except` blocks provide *error isolation* (a later step still runs); the outer `try/finally` provides *resource safety* (cleanup always runs). The two compose; neither replaces the other.

`_skill_sync_worker` and `start_skill_sync_scheduler` remain unchanged. The worker already calls `sync_official_skills()`, and scene sync is now triggered inside that function.

---

## 4. Detailed Design: `scene_sync.py` Changes

### 4.1 `sync_official_scenes` Signature Change

**Current signature** (`scene_sync.py:303`):

```python
async def sync_official_scenes():
    """定时同步官方场景从 grape-skills 仓库到 scene_market 表。"""
```

**Proposed signature**:

```python
async def sync_official_scenes(repo_root: str | None = None):
    """Sync official scenes from the grape-skills repo to the scene_market table.

    Args:
        repo_root: Path to the already-extracted repository root directory.
                   If provided, skips the ZIP download step and reuses the
                   existing files. The caller is responsible for cleanup.
                   If None, downloads the ZIP independently and cleans up
                   the temp directory itself (standalone invocation mode).
    """
```

### 4.2 Download Logic with `repo_root` Parameter

**Current logic** (`scene_sync.py:307–319`):

```python
tmp_dir = None
try:
    bot_token = _get_sync_token()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            repo_root = await _download_repo_zip(client, bot_token)
        except Exception as e:
            log.warning(f"Scene sync: download repo zip failed: {e}")
            _init_featured_scenes_if_empty()
            return
finally:
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

**Proposed logic**:

```python
tmp_dir = None  # only set when this function downloads the ZIP
try:
    if repo_root is None:
        # Standalone mode: download ZIP ourselves
        bot_token = _get_sync_token()
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                repo_root = await _download_repo_zip(client, bot_token)
                tmp_dir = repo_root  # mark for cleanup
            except Exception as e:
                log.warning(f"Scene sync: download repo zip failed: {e}")
                _init_featured_scenes_if_empty()
                return

    # If repo_root was provided by caller, we skip download and go straight
    # to scene processing. No cleanup needed — caller owns the temp dir.

    # ... rest of the function unchanged (locate scenes/, validate, sync DB) ...

finally:
    # Only clean up if this function created the temp directory.
    # If repo_root was externally provided, the caller handles cleanup.
    if tmp_dir:
        # NOTE (audit fix): _download_repo_zip returns mkdtemp/<top_dir>;
        # rmtree(repo_root) alone would leave the empty scene_sync_zip_XXXX
        # parent behind (same residue as skill_sync L238). Delete the parent.
        shutil.rmtree(os.path.dirname(tmp_dir), ignore_errors=True)
```

> **P45 fix (bug fix)**: The current `sync_official_scenes` code (scene_sync.py:308-508) has a bug — `tmp_dir` is initialized to `None` (L308) but never assigned `repo_root` after download (L316 assigns `repo_root`, not `tmp_dir`). The `finally` block (L508) checks `if tmp_dir:` which is always `None`, so cleanup **never executes** — temp directories leak every cycle (~2MB each, ~96MB/day).
>
> The proposed logic above fixes this by adding `tmp_dir = repo_root` after successful download (L373). This is both a requirement for R31 (caller-provided `repo_root` mode needs correct cleanup tracking) and a bug fix for the existing standalone mode.
>
> **Scenario**: Before fix — each `sync_official_scenes()` call downloads ZIP → extracts to `/tmp/scene_sync_zip_XXXX` → processes scenes → returns. `finally: if tmp_dir (None)` → skip cleanup. Temp dir leaks. After 48 cycles/day × ~2MB (measured ZIP size) = ~96MB leaked daily. Server disk fills up over weeks → scene_sync download fails → scene market stops updating. Live evidence: 6 leaked `scene_sync_zip_*` dirs found on the deployed instance.
>
> After fix — `tmp_dir = repo_root` after download → `finally: if tmp_dir (path)` → `shutil.rmtree(os.path.dirname(tmp_dir))` → temp dir AND parent cleaned. Zero leakage.

**Key changes**:

| Aspect | Before | After |
|--------|--------|-------|
| Function signature | `sync_official_scenes()` | `sync_official_scenes(repo_root=None)` |
| Download | Always downloads | Downloads only when `repo_root is None` |
| `tmp_dir` tracking | Always set from download | Only set when this function downloads |
| Cleanup in `finally` | Always cleans | Only cleans when `tmp_dir` is set (i.e., this function downloaded) |
| Caller-provided `repo_root` | N/A | Used directly, not cleaned by this function |

### 4.3 Cleanup Responsibility Matrix

| Invocation mode | `repo_root` value | Who downloads? | Who cleans temp dir? |
|----------------|-------------------|----------------|----------------------|
| Via skill sync (merged) | Path string | `sync_official_skills` | `sync_official_skills` (step 5) |
| Standalone (worker/manual) | `None` | `sync_official_scenes` itself | `sync_official_scenes` `finally` block |

### 4.4 Worker and Scheduler — DELETED (Decision A, 2026-10-22)

> **Decision confirmed by the owner**: skill and scene scheduled tasks merge into ONE worker. `_scene_sync_worker`, `start_scene_sync_scheduler`, `stop_scene_sync_scheduler`, and the `SCENE_SYNC_INTERVAL` dead config are all removed. Rationale: a repo-wide audit found `start_scene_sync_scheduler` had NO caller besides the main.py lifespan — "admin manual trigger" never existed as an entry point, so keeping the worker would retain dead code. `sync_official_scenes(repo_root=None)` standalone-callability is PRESERVED (tests and any future admin trigger endpoint reuse it; adding an endpoint later is a ten-minute change). Removal acceptance: requirements UC5-1 (zero residual references) / UC5-2 (standalone call still works).

**DELETED (Decision A)**: `_scene_sync_worker` (`scene_sync.py:518`), `start_scene_sync_scheduler` (`scene_sync.py:537`), `stop_scene_sync_scheduler` (`scene_sync.py:545`), and the `SCENE_SYNC_INTERVAL` config read — all removed (see §4.4).

What IS preserved: `sync_official_scenes(repo_root=None)` remains directly callable (standalone semantics intact — self-download, self-cleanup), so unit/integration tests and any future admin trigger endpoint keep a working entry point. The merged `_skill_sync_worker` is the ONLY scheduled task (single distributed lock, single 30-min cycle).

### 4.5 Unified Cleanup Form (audit fix)

Both cleanup sites use `os.path.dirname` of the downloaded root:

| Site | Cleanup expression | Deletes |
|------|--------------------|---------|
| `skill_sync.sync_official_skills` (merged path) | `shutil.rmtree(os.path.dirname(repo_root))` | repo dir + `scene_sync_zip_XXXX` parent |
| `scene_sync.sync_official_scenes` (standalone) | `shutil.rmtree(os.path.dirname(tmp_dir))` | repo dir + `scene_sync_zip_XXXX` parent |

Rationale: `_download_repo_zip` returns `os.path.join(mkdtemp_result, top_dir)`. `rmtree(repo_root)` alone removes only the repo subdirectory and leaves the empty parent — the exact residue the current `skill_sync.py:238` leaves every cycle (§1.4 of the requirements doc). The two modes MUST NOT diverge; §4.2's earlier `rmtree(tmp_dir)` form was the divergent one and is superseded by this section.

---

## 5. Detailed Design: `main.py` Changes

### 5.1 Remove Independent Scene Sync Scheduler

**Current code** (`main.py:212–217`):

```python
# 官方场景定时同步：每 30 分钟从 grape-skills 仓库同步场景到 DB
try:
    from src.storage.scene_sync import start_scene_sync_scheduler
    start_scene_sync_scheduler()
except Exception as e:
    log.warning(f"Scene sync scheduler start failed: {e}")
```

**Proposed code**:

```python
# Scene sync is now triggered serially inside sync_official_skills()
# (see R31 design: src/storage/skill_sync.py calls sync_official_scenes
#  with repo_root after skill DB sync completes).
# The independent start_scene_sync_scheduler() call is removed.
```

The entire `try/except` block for `start_scene_sync_scheduler` is **removed**. Scene sync is handled by the skill sync worker's internal serial call.

### 5.2 Shutdown Cleanup

**Current shutdown code** (`main.py:713–718`):

```python
# 官方场景同步：优雅关闭定时任务
try:
    from src.storage.scene_sync import stop_scene_sync_scheduler
    await stop_scene_sync_scheduler()
except Exception:
    pass
```

**Proposed (Decision A)**: the shutdown `stop_scene_sync_scheduler()` block is **removed together with the worker deletion** — with no scene sync tasks ever started, the call is pure dead code. Shutdown of the merged flow is handled entirely by `stop_skill_sync_scheduler` (§5.3): the cancelled skill worker task includes the in-flight scene sync.

### 5.3 Skill Sync Scheduler — No Changes

The skill sync scheduler startup and shutdown remain unchanged:

```python
# Startup (main.py:205–210) — unchanged
try:
    from src.storage.skill_sync import start_skill_sync_scheduler
    start_skill_sync_scheduler()
except Exception as e:
    log.warning(f"Skill sync scheduler start failed: {e}")

# Shutdown (main.py:707–712) — unchanged
try:
    from src.storage.skill_sync import stop_skill_sync_scheduler
    await stop_skill_sync_scheduler()
except Exception:
    pass
```

When `stop_skill_sync_scheduler` cancels the skill sync worker, scene sync (running inside it) is also cancelled as part of the same asyncio task. No separate shutdown is needed for the merged scene sync path.

---

## 6. Distributed Lock Consolidation

### 6.1 Before

```
skill_sync:  run_with_distributed_lock("skill_sync", ttl=1800)   →  protects sync_official_skills()
scene_sync:  run_with_distributed_lock("scene_sync", ttl=1800)   →  protects sync_official_scenes()
```

Two independent locks. In a multi-worker deployment (uvicorn `--workers N`), each lock ensures only one worker executes its respective sync. But the two locks don't prevent the race condition between skill and scene sync — they only prevent duplicate execution of the same sync type.

### 6.2 After

```
skill_sync:  run_with_distributed_lock("skill_sync", ttl=1800)
              → protects sync_official_skills()
              → which internally calls sync_official_scenes(repo_root=...)
              → scene sync is within the same lock scope — no separate lock needed
```

Only one distributed lock (`skill_sync`) is needed for the merged flow. The `scene_sync` lock is retained in `_scene_sync_worker` for backward compatibility (standalone invocation).

### 6.3 Lock TTL, Renewal, and Expiry (revised 2026-10-22: owner-approved)

The `skill_sync` lock TTL is 1800s (30 minutes). The merged flow adds scene sync time to the skill sync time; with the §8.7 network retries the worst-case cycle is ~6 minutes today — but the design lifetime of this lock is not today's 2MB repo: skills/scenes grow with official operations, and the cycle must stay safe at any scale. The earlier "typical 45s, no renewal needed" argument is hereby REPLACED by an explicit renewal + threshold design:

**1. Auto-renewal (in-loop, reusing the existing pattern)**:
- The repo already has `renew_lock` (src/lock.py L81) with 4 mature call sites — `skill_sync.py:280` renews `skill_remote_sync` (TTL 300s) inside its directory-sync loop with a "lock lost → break" guard. The same pattern extends to the outer `skill_sync` lock:
  - renew at the scene-step entry (before `sync_official_scenes`),
  - renew inside long loops (skill directory sync already does; scene traversal if it grows),
  - renewal failure (lock stolen) → log ERROR and abort the remaining steps this cycle (never continue writing under a stolen lock — iron-rule aligned).
- Implementation note (fifth review, 2026-10-22): the scene-step renewal is dispatched via `asyncio.to_thread`, consistent with the adjacent acquire/release calls in the same step — all three lock ops in step 4 are off the event loop. (The pre-existing L280 in-loop renewal remains synchronous; it is sub-millisecond and documented under §8.9's disclosed blocking model.)
- The §8.8 `scene_sync` probe lock (TTL 300s) renews the same way if the scene step exceeds ~4 minutes.

**2. Overtaking detection (one-line cost, mandatory)**:
- If total cycle duration exceeds TTL/2 (900s), log an ERROR with a monitoring-friendly keyword (`skill sync: cycle exceeded 900s`) — the lock-expiry hazard becomes observable BEFORE it happens, prompting TTL adjustment or stuck-cycle investigation.

**3. Expiry on crash (already complete, unchanged)**:
- `distributed_locks.expires_at` + atomic conditional takeover (`WHERE expires_at < now`) + periodic stale-row cleanup: a crashed/killed holder's lock self-expires within TTL. `run_with_distributed_lock` also releases in a finally. No change needed — this half of the capability already covers all locks.

Rationale: not renewing costs "low probability × silent concurrent writes (the exact hazard §8.8 exists to prevent)"; renewing costs ~15 lines reusing a proven in-repo pattern. Threshold alerting makes the residual risk observable instead of silent.

---

## 7. Backward Compatibility Analysis

### 7.1 `sync_official_scenes` Standalone Invocation

| Scenario | `repo_root` | Behavior | Compatible? |
|----------|-------------|----------|-------------|
| Called from `sync_official_skills` (merged path) | Path string | Skips download, processes scenes, no cleanup | ✅ New behavior |
| Called from `_scene_sync_worker` (standalone) | `None` (default) | Downloads ZIP, processes scenes, cleans up | ❌ N/A — worker deleted (Decision A); the row documents the pre-merge behavior only |
| Called directly in tests | `None` or path | Works in both modes | ✅ Flexible |

### 7.2 `_scene_sync_worker` and `start_scene_sync_scheduler`

**DELETED** (Decision A, see §4.4): the worker, schedulers, and dead config are removed. The historical scenario — IF something invoked the standalone worker — was:

1. Acquires the `scene_sync` distributed lock.
2. Calls `sync_official_scenes(repo_root=None)` — downloads independently.
3. Cleans up its own temp directory.

This is fully self-contained and does not conflict with the skill sync worker (different lock key, different task name).

### 7.3 `stop_scene_sync_scheduler`

**Removed** (Decision A): the function is deleted with the worker (§4.4). Shutdown coverage comes solely from `stop_skill_sync_scheduler`, whose cancelled task includes any in-flight scene sync.

### 7.4 API and Database Schema

No API endpoints, database schema, or configuration files are affected. The change is purely internal to the sync scheduling logic.

### 7.5 Configuration

`configs/scene-sync.json` and environment variables (`GRAPESKILLS_REPO_URL`, `GRAPESKILLS_REPO_BRANCH`, `GRAPE_DEV_TOKEN`) continue to be used by `sync_official_scenes` when it downloads independently. In the merged path, `skill_sync.py` uses `_get_sync_token()` and `_download_repo_zip()` from `scene_sync.py` (already imported at `skill_sync.py:221`), which read the same config.

The `SCENE_SYNC_INTERVAL` env var is **removed** (Decision A): its only reader, `_scene_sync_worker`, is deleted. The single merged cycle is controlled by `SKILL_SYNC_INTERVAL` (default 1800s).

### 7.6 Multi-Platform Repo Source (added 2026-10-22: open-source readiness)

grape-skills is being open-sourced on GitHub, so `repo_url` must support both hosts:

- `_parse_repo_host_path(repo_url)` resolves `(download base, owner/repo)`:
  - `https://github.com/{owner}/{repo}` → `https://codeload.github.com` + `/zip/refs/heads/{branch}`
  - `https://gitcode.com/{owner}/{repo}` → `https://raw.gitcode.com` + `/archive/refs/heads/{branch}.zip`
  - unknown host → falls back to gitcode semantics (historical behavior)
- Both platforms produce the same ZIP top dir `{repo}-{branch}` with identical content (verified: 66 skills / 13 scenes / 768 entries / 1.97MB from both hosts), so extraction and everything downstream is host-agnostic.
- **Runtime default stays gitcode** (fallback in `_load_config`); switching the fleet to GitHub is a one-line default change + restart, to be done with the open-source announcement. Example config now points at the GitHub URL.
- Hard lesson encoded in the design: the pre-merge code derived `_OWNER_REPO` via a hardcoded `replace("https://gitcode.com/", "")` — pointing repo_url at GitHub silently produced `raw.gitcode.com/https://github.com/...` and a BadZipFile. Any future host addition must go through `_parse_repo_host_path`, not string surgery.
- **Token/host matching (sixth review, P0 fix)**: GitHub codeload returns **404 for ANY `Authorization: Bearer` header** — a GitCode PAT (production standard via GRAPE_DEV_TOKEN) alongside the GitHub repo URL broke downloads entirely. `_download_repo_zip` now sends headers per host: GitHub downloads are ALWAYS anonymous (`headers = {}` even when a token is present); the token is only attached to gitcode endpoints. Two regression tests pin this per host. Verified end-to-end with token + GitHub URL: 200 OK, 13 scenes / 66 skills intact.
- **Test-environment decoupling (seventh review, P1)**: the legacy `test_auth_header` asserted the Bearer header without pinning the download source — on a host configured with the GitHub repo URL it took the (correct) anonymous branch and failed as a false regression. Fixed by explicitly patching `_DL_BASE` to gitcode inside the test (token semantics are gitcode-only by design); the GitHub-anonymous behavior has its own dedicated test. Full suite green under an injected GitHub environment (84/84).
- End-to-end verification (scratch DB, real code): GitHub anonymous download → 13 scenes synced (stale detection intact, iron rule held) → 66-skill DB sync → temp dirs cleaned → 84/84 tests green.

---

## 8. Risk Analysis

### 8.1 Temp Directory Lifetime Extension

**Risk**: The temp directory lives longer (through scene sync). If scene sync hangs or takes excessive time, the temp directory occupies disk space for longer.

**Mitigation**: Scene sync makes no repo download when `repo_root` is provided — only DB operations and optional icon (OBS) upload. Typical scene sync takes <15s. The temp directory is cleaned in a `finally`-equivalent position (step 5 in `sync_official_skills`), which executes even if scene sync raises an exception.

Note: the existing `skill_sync.py` cleanup (`tmp_dir = repo_root` at L238) deletes only the repo subdirectory and leaves the empty `scene_sync_zip_XXXX` parent dir behind each cycle; the proposed `os.path.dirname(repo_root)` cleanup form fixes this residual leak as well.

**Residual risk**: Low. Worst case: ~2 MB temp directory persists for an additional ~15s per cycle.

### 8.2 Scene Sync Failure Impact

**Risk**: If `sync_official_scenes` raises an unhandled exception inside `sync_official_skills`, it could affect the return value or leave the temp directory uncleaned.

**Mitigation**: The scene sync call is wrapped in its own `try/except`:

```python
try:
    from src.storage.scene_sync import sync_official_scenes
    await sync_official_scenes(repo_root=repo_root)
except Exception as e:
    logger.exception(f"skill sync: scene sync failed: {e}")
```

Cleanup (step 5) is outside this `try/except`, so it executes regardless. The `result` dict from skill sync is not affected by scene sync failures.

**Residual risk**: None. Scene sync failure is fully isolated.

### 8.3 Skill Download Failure → Scene Sync Fallback

**Risk**: If skill sync's ZIP download fails (`repo_root is None`), scene sync is called with `repo_root=None`, triggering a second download attempt. This could also fail.

**Mitigation**: This is acceptable — it degrades gracefully to the current behavior (both attempt independent downloads). If both fail, scene sync calls `_init_featured_scenes_if_empty()` as a fallback, which is the existing behavior. The user sees featured scenes even without a successful download.

**Residual risk**: Low. In the worst case, the behavior is identical to the current architecture (both downloads fail, featured scenes initialized).

### 8.4 Distributed Lock Expiry During Merged Flow

**Risk**: The `skill_sync` lock has a 1800s TTL. If the merged flow (skill sync + scene sync) takes longer than 1800s, the lock expires and another worker may start a concurrent sync.

**Mitigation**: The merged flow typically takes ~45s (skill ~30s + scene ~15s + download ~10s), far below the 1800s TTL. The skill sync worker also calls `renew_lock` during directory sync (`skill_sync.py:280`). No lock renewal is needed for scene sync given its short duration.

**Residual risk**: Negligible. Only relevant if the repo is extremely large or the network is very slow.

### 8.5 Concurrent Standalone and Merged Scene Sync (SUPERSEDED by Decision A)

> **Historical**: this scenario assumed the standalone worker was retained. Decision A deleted it, so the "admin manually starts scene sync while merged worker runs" case no longer exists in code. Kept for design-history traceability; the surviving live variant of this hazard is the rolling-upgrade window, handled by the §8.8 lock probe.

**Risk (historical)**: If an admin manually starts `start_scene_sync_scheduler()` while the skill sync worker is also running, two scene syncs could execute concurrently — one via the merged path (inside `skill_sync` lock) and one via the standalone worker (`scene_sync` lock).

**Mitigation**: They use different lock keys (`skill_sync` vs `scene_sync`), so they can run concurrently. However, both write to the same `scene_market` table. The DB operations use `db_lock` (SQLite thread lock) or transaction isolation (MySQL), so concurrent writes are serialized at the DB level. The worst case is redundant processing (same scenes synced twice), not data corruption — `INSERT OR IGNORE` and `UPDATE` are idempotent.

**Residual risk**: Low. Redundant processing is wasteful but not harmful. This is an edge case (admin manually starting scene sync while skill sync is running).

### 8.6 `repo_root` Path Validity

**Risk**: If `sync_official_skills` passes an invalid or stale `repo_root` path to `sync_official_scenes` (e.g. the directory was partially cleaned by an error), scene sync could fail.

**Mitigation**: `repo_root` is only set from the return value of `_download_repo_zip`, which creates a valid temp directory. The cleanup (step 5) only runs after scene sync completes. If `_download_repo_zip` fails, `repo_root` remains `None`, and scene sync falls back to its own download.

**Residual risk**: None. The path is either valid (from successful download) or `None` (download failed).

### 8.7 Network Failures and Retry Design (added 2026-10-22 review)

**Risk**: Transient network faults (DNS blips, connection resets, 5xx from the archive host, OBS outages) currently abort the whole cycle with no in-cycle retry — the system heals only at the next 30-min cycle. That is too brittle for a sync whose steps are network-bound.

**Retry design (in-cycle, exponential backoff, idempotency-gated)**:

1. **Download step** (`_download_repo_zip`, the most fault-prone): retry up to 3 attempts with exponential backoff (5s / 15s / 45s). Only network-class exceptions retry (httpx.TransportError, httpx.HTTPStatusError with 5xx, socket errors, timeouts); 4xx (auth/config) fail fast — retrying a bad token is pointless noise.
2. **OBS upload step**: same policy (3 attempts, 5/15/45s, 5xx/transport only). Storage writes are idempotent (fixed object keys, atomic overwrite), so a retry can never duplicate.
3. **DB steps: NO in-cycle retry.** DB operations run against the local MySQL with per-row try/except already; a DB fault is rarely transient in the same sense, and retrying DB writes risks amplifying load during an outage. Cycle-level retry (next period) covers them.
4. **Cap and telemetry**: max 3 retries keeps worst-case added time ~65s (well under the 1800s lock TTL and the 60s httpx timeout × attempts budget). Every retry logs a warning with attempt number and backoff delay; final failure logs an error and falls through to the existing per-step degradation (scene fallback → featured-scenes init).
5. **Idempotency precondition**: retries are safe because every retried operation is idempotent (ZIP download = read; OBS put = fixed-key overwrite; nothing before the retry point has committed partial state).

**Cycle-level retry remains the outer safety net**: any failure still not healed in-cycle is fully retried by the next 30-min cycle with content_hash incremental sync (only the failed portion is redone).

**Failure visibility**: consecutive-failure counting with escalation — after 3 consecutive failed cycles, emit an ERROR-level line (a monitoring-friendly keyword like `skill sync: 3 consecutive failed cycles`) so log-based alerting can page on persistent outages instead of transient ones. Single-cycle failures stay at WARNING.

**Residual risk**: Low. Worst case with retries: cycle takes ~2 extra minutes and still fails → next cycle retries; featured scenes remain visible throughout.

### 8.8 Rolling-Upgrade Double-Run Window (added 2026-10-22 review; owner decision 2026-10-22: lock-probe avoidance)

**Risk**: During a rolling deploy (new merged worker + old scene worker briefly coexisting), the old scene worker (`scene_sync` lock) and the new merged worker (`skill_sync` lock) may sync scenes concurrently — different lock keys do not exclude each other. Concurrent writes are harmless in practice (see Defense-in-depth below), but the owner chose to avoid them entirely.

**Mitigation (primary): non-blocking `scene_sync` lock probe before the scene-sync step.**

The merged worker acquires `skill_sync` as before, then — immediately before calling `sync_official_scenes(repo_root=...)` — tries to acquire the legacy `scene_sync` lock NON-blocking:

```python
# inside sync_official_skills, before the serial scene-sync step
from src.lock import acquire_lock, release_lock
scene_lock_acquired = await asyncio.to_thread(acquire_lock, "scene_sync", ttl=300)
if not scene_lock_acquired:
    # A pre-upgrade instance's standalone scene worker is running right now.
    # Skip this cycle's scene step (skill step already done); next cycle it
    # will win the probe once the old instances are gone.
    logger.warning("skill sync: legacy scene_sync lock held, skipping scene step this cycle")
    return result  # temp dir still cleaned by the whole-body finally
# ... await sync_official_scenes(repo_root=repo_root) ...
# release the probe lock right after the scene step (in a finally around it)
await asyncio.to_thread(release_lock, "scene_sync")
```

Behavior:
- **Steady state (all instances on new code)**: no old scene worker exists anywhere, so the probe always succeeds instantly — behavior is identical to the single-lock design, zero overhead (one no-contention lock acquire/release per cycle).
- **Rolling window**: old instance's scene worker holds `scene_sync` → probe fails → the new merged worker SKIPS only the scene step this cycle (skill sync still completes; temp dir still cleaned). Next cycle, the old instance is gone, the probe succeeds, scene sync resumes. No concurrent scene writes at any moment.
- **No deadlock**: `skill_sync` is acquired first, `scene_sync` probe is non-blocking (never waits), and the old worker never takes `skill_sync`. No lock-ordering cycle exists.
- **Probe TTL 300s**: only needs to cover the scene-sync step (~15s typical); a crashed holder self-expires long before the next cycle.

**Defense-in-depth (unchanged, still true)**: `scene_market` has a UNIQUE constraint (`uk_name_scope_user_repo`, verified on the live MySQL schema); concurrent duplicate INSERTs would be rejected and swallowed by the per-scene try/except; UPDATEs are content_hash-idempotent. So even if the probe were somehow bypassed, concurrent writes remain harmless — the probe removes the waste, the constraint removes the risk.

**Residual risk**: Negligible. The only cost is at most one skipped scene step per instance during a deploy window (retried next cycle).

### 8.9 Async Model and Storage Convergence (added 2026-10-22 review)

**Async model — layered honesty**:
- Scheduling layer IS fully async: workers spawn via `asyncio.create_task` (lifespan is not blocked, unlike the D32 migration which deliberately awaits pre-yield); the worker loop awaits/sleeps; downloads use `httpx.AsyncClient`; lock ops go through `asyncio.to_thread`.
- Execution layer has pre-existing synchronous blocking calls inside async bodies: `shutil.copytree/rmtree`, `open()/read`, `os.listdir` — each cycle accumulates ~1-2s of event-loop stall at the current 2MB repo scale. This is the CURRENT behavior of both existing workers, unchanged and slightly improved by the merge (same total, half the frequency). The OBS hash computation already uses `run_in_executor` (correct existing practice). Migration of file IO to `to_thread` is recorded as an observation item for when the repo grows — not a blocker.

**Storage convergence — no super-linear growth (measured)**:
| Storage site | Growth mechanism | Convergence |
|---|---|---|
| /tmp temp dirs | one per cycle | deleted same-cycle (P45 fix incl. parent); crash orphans swept >24h — no accumulation |
| local skills/ dir | incremental copy | hash-gated skip (unchanged content never copied); changed dirs rmtree-then-copytree — converges to content, no version stacking |
| OBS objects | skill packages | fixed-key atomic overwrite (v2.4 de-versioned) + hash gating — one object per skill, count never grows with cycles |
| DB skill/scene rows | fixed-row UPDATE | row count == official repo entry count |
| The only growth source | the official repo itself | measured: ZIP 1.97MB / uncompressed 4.2MB / largest skill 1.25MB — any sync scheme follows proportionally; hash increment only transfers changes |

Shrink path (repo removes a skill): detection reports stale (P46.4); the ADMIN manually deletes the DB row and local dir (iron rule: the sync chain never deletes). Verified: the deletion chain is manual by design, and stale detection stops reporting once the admin acts.

---

## 9. Impact Summary

| Dimension | Before | After |
|-----------|--------|-------|
| Scheduled tasks | 2 independent workers | 1 worker |
| ZIP downloads per cycle | 2 | 1 |
| Dependency ordering | Not guaranteed (concurrent race) | Guaranteed (serial execution) |
| Scene skip on first startup | ~100% (race condition) | 0% (skill table ready before scene check) |
| Distributed locks | 2 (`skill_sync` + `scene_sync`) | 1 (`skill_sync`) in normal operation |
| Bandwidth per cycle | 2× ZIP (~4 MB) | 1× ZIP (~2 MB, measured 1.97 MB) |
| Temp disk usage | 2 ZIP extractions (short-lived) | 1 ZIP extraction (slightly longer-lived) |
| Total sync time | Concurrent ~max(T1, T2) | Serial T1+T2 (~45s typical) |
| Backward compatibility | — | `sync_official_scenes(repo_root=None)` works standalone |
| `_scene_sync_worker` | Active (started in lifespan) | DELETED (Decision A: no caller besides main.py lifespan ever existed) |

---

## 10. Verification Checklist

### 10.1 Functional Verification

- [ ] Service first startup → skill sync completes → scene sync auto-triggers → scene market has data
- [ ] New skill + new scene pushed → within the same cycle, skill enters DB first → scene sync finds skill → scene sync succeeds
- [ ] Skill sync failure → scene sync still executes (falls back to independent download with `repo_root=None`)
- [ ] Skill sync success but scene sync failure → does not affect next cycle's skill sync
- [ ] ZIP downloaded only once per cycle (only one "download repo zip" log entry)
- [ ] Temp directory cleaned after scene sync completes

### 10.2 Dependency Order Verification

- [ ] Log shows `skill sync: DB sync done` before `Scene sync: _check_skills_exist`
- [ ] Scene sync's `_check_skills_exist` finds the latest skill table data
- [ ] `scene_skill_ref.skill_id` matches `skill.id` primary key

### 10.3 Compatibility Verification

- [ ] `sync_official_scenes(repo_root=None)` standalone call works (downloads independently)
- [ ] `sync_official_scenes(repo_root=path)` reuse mode works (skips download)
- [ ] No residual references: grep confirms `_scene_sync_worker` / `start_scene_sync_scheduler` / `stop_scene_sync_scheduler` / `SCENE_SYNC_INTERVAL` have zero code hits in src/ (Decision A; enforced by tests/test_r31_p45_p46.py::TestStandaloneWorkerRemoval)
- [ ] `skill_sync` distributed lock works correctly (only one worker executes in multi-worker deployment)
- [ ] `stop_skill_sync_scheduler` stops both skill and scene sync

### 10.4 Performance Verification

- [ ] Single sync cycle downloads ZIP exactly once (not twice)
- [ ] Only one temp directory exists (not two)
- [ ] Total sync time < 60s (skill ~30s + scene ~15s + download ~10s)

---

## 11. Implementation Order

1. **`scene_sync.py`**: Add `repo_root` parameter to `sync_official_scenes`, implement download-skip and conditional cleanup logic. **Unify the standalone cleanup form to `os.path.dirname(repo_root)`** (see §4.5) so both modes clean the full `scene_sync_zip_XXXX` parent dir.
2. **`skill_sync.py`**: Restructure `sync_official_skills` to defer temp cleanup, add serial `sync_official_scenes` call, add post-scene-sync cleanup. **Wrap the whole function body (steps 1–5) in a single try/finally** (see §3.5) so any exception path — including a failing `logger.exception` in steps 1–3 — still cleans the temp dir.
3. **`main.py`**: Remove `start_scene_sync_scheduler()` from lifespan startup AND the `stop_scene_sync_scheduler()` block from shutdown (Decision A — the functions themselves are deleted from `scene_sync.py`, together with `_scene_sync_worker` and the `SCENE_SYNC_INTERVAL` dead config).
4. **`scene_sync.py` (P46)**: Implement read-only stale detection with completion guard (§12.1), unify pre-check scope (§12.2), fix early-return fallback (§12.3); skill-side detection per §12.6. **IRON RULE: no delete/deactivate writes anywhere in the sync chain (requirements §1.0).**
5. **`upload_cleanup.py` (P45 companion)**: Add the `/tmp` leak-pattern sweep (§12.4).
6. **Tests**: Add unit tests for both invocation modes of `sync_official_scenes` (with and without `repo_root`); mock or short-circuit the serial scene call in skill-side tests; add integration test verifying serial execution order; add P46 tests per §12.5.
7. **Manual verification**: Deploy to test environment, verify first-startup scene market population, single ZIP download, and stale-detection reporting (zero DB writes).

---

## 12. P46 Design: Stale Detection (read-only), Pre-check Unification, Early-Return Fallback, /tmp Sweep

> This section implements the requirements doc §六 (P46.1/P46.2/P46.3/P46.4) and the P45 companion sweep. **IRON RULE (requirements §1.0): the sync chain MUST NOT delete, deactivate, or overwrite production data.** An earlier revision of this section proposed auto reconcile-delete with a completion guard; it was rejected in review (2026-10-22) because it gave the sync task a destructive path whose worst case is a market-wide outage. The design below is detect-and-report only; disposal is a manual admin action.

### 12.1 P46.1 — Stale-Scene Detection (read-only)

**Placement**: a new `_detect_stale_official_scenes(repo_scene_names: set, traversal_complete: bool)` function in `scene_sync.py`, called at the END of `sync_official_scenes` (both merged and standalone modes), before `_init_featured_scenes_if_empty()`.

**Algorithm**:

```python
def _detect_stale_official_scenes(repo_scene_names: set, traversal_complete: bool):
    # Integrity precondition: only protect REPORT accuracy — no DB writes exist,
    # so a wrong report cannot damage anything. Worst case: this cycle has no report.
    if not traversal_complete:
        logger.warning("Scene sync: traversal incomplete, skipping stale detection this cycle")
        return
    with db_lock:
        c = db.cursor()
        stale = [name for (_id, name) in
                 c.execute("SELECT id, name FROM scene_market WHERE source='official'").fetchall()
                 if name not in repo_scene_names]
    if stale:
        logger.warning(
            f"Scene sync: stale official scenes (removed from repo, "
            f"admin should deactivate manually): {', '.join(stale)}")
    return len(stale)  # surfaced as stats["stale_official"] in the sync summary
```

**Completion guard wiring**: `sync_official_scenes` tracks `traversal_complete = True` and flips it to `False` in the per-directory `except` handler. `repo_scene_names` is collected from the same directory listing the traversal iterates.

**Design choices**:
- ZERO DB writes: no is_active flip, no DELETE, no ref cleanup. Disposal (deactivate + ref cleanup) is a manual admin action (admin scene list or SQL); after the admin acts, the next cycle's detection stops reporting.
- The guard now protects only report accuracy (no false "stale" names from an incomplete traversal); its failure mode degrades to "no report this cycle", not data damage.
- Residual risk accepted: stale rows remain visible in the market until an admin acts — visible + logged, never silent.

**Verification**: requirements UC9-1 (detection report + zero DB writes) and UC9-4 (guard skips report on mid-traversal failure).

### 12.2 P46.2 — Pre-check / Rebuild Scope Unification

`_check_skills_exist` (L154) currently queries `skill` with NO scope/is_active filter; `_rebuild_scene_skill_ref` (L248) requires `is_active=1 AND scope IN ('official','repo')`. A scene referencing a user-scope or deactivated skill passes the pre-check but writes `skill_id=0`.

**Fix**: one shared helper, both callers use it:

```python
def _query_active_skill_names(skill_names: set) -> set:
    with db_lock:
        c = db.cursor()
        placeholders = ",".join("?" * len(skill_names))
        c.execute(
            f"SELECT DISTINCT skill_name FROM skill "
            f"WHERE skill_name IN ({placeholders}) AND is_active=1 "
            f"AND scope IN ('official','repo')",
            list(skill_names))
        return set(r[0] for r in c.fetchall())
```

`_check_skills_exist` uses it for the missing-list; `_rebuild_scene_skill_ref` uses it for the id resolution (its official-first/repo-second priority query stays, only the existence semantics align). Verification: requirements UC9-3.

### 12.3 P46.3 — Early-Return Fallback

Current: `scenes/` missing → `log.warning` + `return` (L324), skipping `_init_featured_scenes_if_empty()`, while the download-failure path (L318) DOES call it — inconsistent.

**Fix**: the early return calls the fallback too:

```python
scenes_dir = os.path.join(repo_root, "scenes")
if not os.path.isdir(scenes_dir):
    log.warning("Scene sync: scenes/ directory not found in zip")
    _init_featured_scenes_if_empty()
    return
```

Note this is the same fallback the download-failure path uses — fresh installs never see a completely empty market due to a repo layout change. Verification: requirements UC9-2.

### 12.4 P45 Companion — upload_cleanup /tmp Leak Sweep

`upload_cleanup.py` currently cleans only `uploaded_files` table rows and OBS attachments; it has no /tmp filesystem scanning. Add one function to its existing hourly cycle:

```python
def _sweep_leaked_tmp_dirs():
    """P45 companion: remove crash-orphaned sync temp dirs (>24h old)."""
    import glob, time, shutil
    patterns = [os.path.join(tempfile.gettempdir(), "scene_sync_zip_*")]
    for pattern in patterns:
        for d in glob.glob(pattern):
            try:
                if time.time() - os.path.getmtime(d) > _MAX_TEMP_AGE_HOURS * 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
```

Scope discipline: ONLY `scene_sync_zip_*` (the sync module's own prefix). Other modules' temp prefixes (e.g. executor's `hermes-iso-*`) are their owners' cleanup responsibility — do not sweep them here. The >24h age guard prevents deleting a dir belonging to a sync cycle that is CURRENTLY running.

### 12.6 P46.4 — Skill-Side Stale Detection (read-only, same-family fix)

`_sync_skills_to_db(data)` iterates skill-description.json entries with INSERT/UPDATE only — a skill removed from the repo json persists as an is_active=1 official row, and keeps satisfying `_check_skills_exist` and the official-first lookup in `_rebuild_scene_skill_ref`.

**Fix (IRON RULE compliant — detection only, no deactivation)**: mirror the scene-side design (§12.1):

```python
def _detect_stale_official_skills(repo_skill_names: set, traversal_complete: bool):
    if not traversal_complete:  # json parse failed or DB write aborted mid-way
        logger.warning("skill sync: DB sync incomplete, skipping stale detection this cycle")
        return 0
    with db_lock:
        c = db.cursor()
        stale = [name for (_id, name) in
                 c.execute("SELECT id, skill_name FROM skill WHERE scope='official'").fetchall()
                 if name not in repo_skill_names]
    if stale:
        logger.warning(
            f"skill sync: stale official skills (removed from repo json, "
            f"admin should deactivate manually): {', '.join(stale)}")
    return len(stale)  # surfaced as stats["stale_skill"]
```

Wiring: `repo_skill_names = set(data.keys())` collected where the merged json is loaded; `traversal_complete` flips to False when the json fails to parse or `_sync_skills_to_db` aborts mid-way. Called at the end of the DB-sync step in `sync_official_skills`. ZERO DB writes; deactivation is a manual admin action.

Downstream effect (only after the ADMIN deactivates): the skill no longer passes the (unified, §12.2) pre-check or the official-first ref resolution; scenes referencing it report missing / fall to the repo-scope lookup / skill_id=0 with warning. Verification: requirements UC11-1/11-2/11-3.

### 12.7 Updated P46 Test Plan

| Test | Verifies |
|------|----------|
| test_reconcile_deletes_stale_scene | UC9-1: repo-removed scene → warning + stats reported, **DB unchanged (zero writes)** |
| test_reconcile_skips_non_official | user/featured scenes untouched |
| test_reconcile_guard_partial_traversal | UC9-4: traversal_complete=False → no report, warning logged, DB unchanged |
| test_precheck_rebuild_same_scope | UC9-3: user-scope skill → pre-check reports missing (aligned) |
| test_scenes_dir_missing_fallback | UC9-2: early return still initializes featured scenes |
| test_tmp_sweep_old_dirs_only | >24h dirs removed, fresh dirs kept |
| test_skill_reconcile_deactivates_stale | UC11-1: json-removed skill → warning + stats, **DB unchanged (zero writes)** |
| test_skill_reconcile_guard | UC11-2: json parse fail / aborted write → no report, DB unchanged |
| test_deactivated_skill_ref_chain | UC11-3: scene ref no longer resolves to deactivated official skill |
| existing 38 scene tests + skill tests with mocked scene call | UC10-1/UC10-2 regression |

---

## References

- Requirements document: `docs/weekly-08/skill-scene-sync-merge-requirements.md` (R31 + P45 + P46, including the scenario/use-case catalog S1–S15 in its §三)
- `src/storage/skill_sync.py` — `sync_official_skills` (line 214), `_skill_sync_worker` (line 342), `start_skill_sync_scheduler` (line 364)
- `src/storage/scene_sync.py` — `sync_official_scenes` (line 303), `_download_repo_zip` (line 202), `_scene_sync_worker` (line 518), `start_scene_sync_scheduler` (line 537)
- `src/api/main.py` — lifespan startup (line 205–217), lifespan shutdown (line 707–718)
- `CONTRIBUTING.md` — design document conventions (§17.3: design docs in `docs/`, named `xxx-design.md`)
