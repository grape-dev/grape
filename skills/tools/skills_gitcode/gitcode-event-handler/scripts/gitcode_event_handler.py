#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitCode 事件处理器 —— 处理 GitCode webhook 推送的事件

支持的事件(按业务逻辑):
  1. Issue 创建事件    -> 识别 skill 开发任务, 创建 Multica issue 分配给 skills开发小队, 入库
  2. Issue 评论事件    -> 转发评论到对应的 Multica issue
  3. PR 创建事件(匹配到 issue) -> 在 Multica issue 下 @skills测试小队 处理;
                               共建仓库节点(node_repo)由 PR 合入事件点亮;
                               测试小队完成后(轮询检测) @skills验证小队 处理
  4. PR 创建事件(未匹配)      -> 新建 Multica issue 分配给 skills测试小队;
                               测试完成后 @skills验证小队; 入库
  5. PR 评论事件       -> 转发评论到 PR 对应的 Multica issue
  6. 每次处理进展通过 GitCode 评论通知提交者; 程序自身评论带 [skills_bot] 前缀,
     不会被当作输入转发给 Multica issue
  7. 合入 distribute_for_github 分支的 PR(通常为 cherry-pick) ->
     创建 Multica issue 分配给 skills分发小队 处理, 入库

辅助模式:
  --check-handoff   轮询检测"测试小队已完成"的 Multica issue, 自动 @验证小队并转移分配
                    (建议配合 cron 每 5-10 分钟执行)

用法:
  python3 gitcode_event_handler.py <payload.json>            # 处理 webhook payload 文件
  cat payload.json | python3 gitcode_event_handler.py -      # 从 stdin 读取
  python3 gitcode_event_handler.py --check-handoff           # 测试→验证 交接轮询
  python3 gitcode_event_handler.py --test                    # 自检

依赖: pymysql, multica CLI (已安装)
"""

import datetime
import glob
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl  # 本地仓库镜像 flock 排队等锁(Linux)
except ImportError:
    fcntl = None

logger = logging.getLogger("gitcode-event-handler")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

try:
    import pymysql
except ImportError:
    print("缺少依赖 pymysql, 请执行: pip install pymysql", file=sys.stderr)
    sys.exit(2)

# ==================== 配置区(全部可用环境变量覆盖) ====================


def _load_env_file():
    """从 ~/.agents/skills/.env 加载本机工具链凭据(webhook 会话无注入环境变量时的兜底)。

    仅填充 os.environ 中未显式设置的键(显式环境变量优先);
    文件缺失/解析失败时静默跳过(fail-open), 不影响纯环境变量部署。
    """
    for _path in (
        os.path.expanduser("~/.agents/skills/.env"),
        "/root/.agents/skills/.env",
    ):
        try:
            with open(_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    if _line.startswith("export "):
                        _line = _line[len("export "):]
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip()
                    if _v[:1] in ("'", '"') and _v[-1:] == _v[:1]:
                        _v = _v[1:-1]
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
            return
        except Exception:
            continue


_load_env_file()
GITCODE_API_BASE = os.environ.get("GITCODE_API_BASE", "https://gitcode.com/api/v5")
GITCODE_TOKEN = os.environ.get("GITCODE_TOKEN", "REPLACE_WITH_GITCODE_TOKEN")

# ==================== 本地仓库镜像(2026-08-18 用户要求) ====================
# 审查/判重/分流读**仓库内容**改用本地 bare 镜像(替代 contents/commits API
# 逐文件拉取), 消除 429 限流误报(#15/#16 整批误报根因: 限流期 contents
# 批量失败 -> SKILL.md 补入失败 -> 检测点全误报缺失)。并发 fetch 用 flock
# 排队等锁(用户确认方案); 任何失败 fail-open 返回 None/空, 调用方回退原
# API 逻辑。镜像与 gitsync 分发目录(/tmp/gitsync-* 临时)完全隔离。
MIRROR_ROOT = os.environ.get("GITCODE_MIRROR_ROOT", "/root/.cache/gitcode_repos")
MIRROR_SSH = os.environ.get(
    "GITCODE_MIRROR_SSH", "ssh -i /root/.ssh/id_rsa -o StrictHostKeyChecking=no")
MIRROR_MAX_WAIT = int(os.environ.get("GITCODE_MIRROR_MAX_WAIT", "30"))
MIRROR_FETCH_TTL = int(os.environ.get("GITCODE_MIRROR_FETCH_TTL", "60"))
# 进程内 ensure 节流: 同一进程(一次审查/判重流程多次读)只 fetch 一次,
# 避免每次 _mirror_read 都 git fetch(实测 97 次读 = 97 次 fetch = 74s)
_MIRROR_ENSURE_CACHE = {}


def _mirror_path(repo):
    """仓库 -> 镜像目录路径(bare, /root/.cache/gitcode_repos/<owner>__<repo>.git)"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", repo or "")
    return os.path.join(MIRROR_ROOT, safe + ".git")


def _mirror_git(args, cwd=None, timeout=120):
    """执行 git 命令(注入 SSH 凭据), 返回 (exit_code, stdout, stderr)"""
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = MIRROR_SSH
    try:
        proc = subprocess.run(["git"] + args, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=cwd)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        logger.warning(f"[mirror] git 执行失败: {e}")
        return -1, "", str(e)[:150]


def _ensure_mirror(repo, max_wait=None):
    """确保本地镜像存在且最新。并发安全: flock 排队等锁(0.2s 轮询阻塞,
    用户要求"排队等锁"而非冲突放弃), 锁内 clone(不存在)或 fetch -f(存在,
    force 处理远端 force push)。等待超时返回 False(极端情况, 调用方回退 API)。

    返回 True=镜像可用 / False=不可用。
    """
    if not repo:
        return False
    if fcntl is None:
        return False  # 非 Linux 无 flock, 直接回退 API
    # 进程内节流: TTL 内已 ensure 成功则直接复用(同进程多次读只 fetch 一次)
    _now = time.time()
    _last = _MIRROR_ENSURE_CACHE.get(repo)
    if _last and _now - _last < MIRROR_FETCH_TTL:
        return True
    path = _mirror_path(repo)
    max_wait = MIRROR_MAX_WAIT if max_wait is None else max_wait
    try:
        os.makedirs(MIRROR_ROOT, exist_ok=True)
        lock_path = path + ".lock"
        with open(lock_path, "w") as lf:
            start = time.time()
            while True:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() - start > max_wait:
                        logger.warning(f"[mirror] {repo} 等待锁超时 {max_wait}s, 回退 API")
                        return False
                    time.sleep(0.2)
            try:
                if os.path.isdir(path):
                    code, out, err = _mirror_git(
                        ["fetch", "-f", "-q", "origin", "+refs/heads/*:refs/heads/*"],
                        cwd=path)
                    if code != 0:
                        # 镜像损坏/ref 缺失 -> 重建
                        logger.warning(f"[mirror] {repo} fetch 失败, 尝试重建: {err[:120]}")
                        shutil.rmtree(path, ignore_errors=True)
                        code, out, err = _mirror_git(
                            ["clone", "--bare", "-q", f"git@gitcode.com:{repo}.git", path])
                        if code != 0:
                            logger.warning(f"[mirror] {repo} clone 失败: {err[:120]}")
                            return False
                else:
                    code, out, err = _mirror_git(
                        ["clone", "--bare", "-q", f"git@gitcode.com:{repo}.git", path])
                    if code != 0:
                        logger.warning(f"[mirror] {repo} clone 失败: {err[:120]}")
                        return False
                _MIRROR_ENSURE_CACHE[repo] = time.time()
                return True
            finally:
                try:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[mirror] {repo} 镜像操作异常: {e}")
        return False


def _mirror_read(repo, path):
    """读镜像内文件内容(git show HEAD:<path>, 原子)。失败/不存在返回 None。"""
    if not (repo and path) or not _ensure_mirror(repo):
        return None
    code, out, err = _mirror_git(["show", f"HEAD:{path}"], cwd=_mirror_path(repo))
    return out if code == 0 else None


def _mirror_dir_exists(repo, path):
    """镜像内目录是否存在(ls-tree 检查 type=tree)。返回 True/False/None(失败)。"""
    if not (repo and path) or not _ensure_mirror(repo):
        return None
    code, out, err = _mirror_git(["ls-tree", "HEAD", path], cwd=_mirror_path(repo))
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "tree":
            return True
    return False


def _mirror_dir_files(repo, path):
    """镜像内目录下所有 blob 文件路径(ls-tree -r 递归)。返回 set 或 None(失败)。

    注意与 _repo_dir_files 语义对齐: 空目录/路径不存在返回空 set,
    命令失败(镜像不可用)返回 None(fail-open 由调用方回退)。
    """
    if not (repo and path) or not _ensure_mirror(repo):
        return None
    code, out, err = _mirror_git(
        ["ls-tree", "-r", "-z", "--name-only", "HEAD", path], cwd=_mirror_path(repo))
    if code != 0:
        return None
    return set(f for f in out.split("\0") if f)


def _mirror_products(repo):
    """本地镜像构建产品映射 {skill名小写: 路径}(2026-08-18 本地化)。

    替代 _get_repo_products 的 API 类目遍历(每次 15-25 次 contents 调用,
    限流期返回不完整清单是 #101 误判"仓库无此 skill"的根因)。一次
    ls-tree -r -d 拿全量目录树, 按"skill 根"规则构建:
    - skills/<cat>/<name>(name 是 huawei-cloud-*/xxx-skill) -> 直接是 skill 根
    - skills/<cat>/<subcat>/<name> -> 二级子类目下的 skill 根
    返回 dict 或 None(镜像不可用, 调用方回退 API)。
    """
    if not _ensure_mirror(repo):
        return None
    code, out, err = _mirror_git(
        ["ls-tree", "-r", "-d", "-z", "--name-only", "HEAD", "skills"],
        cwd=_mirror_path(repo))
    if code != 0:
        return None
    products = {}
    for p in (f for f in out.split("\0") if f):
        parts = p.split("/")
        if len(parts) < 3:  # skills/<cat>/<name> 至少 3 段
            continue
        name = parts[-1]
        if re.search(r"(huawei[-_]?cloud-|[\w-]+-skill)", name, re.IGNORECASE):
            products[name.lower().replace("_", "-")] = p
    return products or {}


def _mirror_commit_authors(repo, path, limit=100):
    """镜像内某路径的提交作者列表(git log, 2026-08-18 本地化替代 commits API)。

    返回 [(author_name, author_email), ...] 或 None(镜像不可用, 调用方回退
    commits API)。GitCode commits API 的 author.login 是账号登录名, 本地
    git log 只有 author name/email —— bot 判定用 _mirror_author_is_bot
    (name/email 匹配 BOT_AUTHOR_LOGINS, 提交者 git config user.name/email
    通常就是账号名, 如 skills_bot)。
    """
    if not _ensure_mirror(repo):
        return None
    code, out, err = _mirror_git(
        ["log", f"--format=%an|%ae", f"-{int(limit)}", "--", path],
        cwd=_mirror_path(repo))
    if code != 0:
        return None
    authors = []
    for ln in out.splitlines():
        if "|" in ln:
            n, e = ln.split("|", 1)
            authors.append((n.strip(), e.strip()))
    return authors


def _mirror_author_is_bot(name, email):
    """本地提交作者是否机器人(name/email 命中 BOT_AUTHOR_LOGINS 或含 bot)"""
    low_n = (name or "").lower()
    low_e = (email or "").lower()
    for b in BOT_AUTHOR_LOGINS:
        if b in low_n or b in low_e:
            return True
    return "bot" in low_n or "bot" in low_e

MULTICA_SERVER = os.environ.get("MULTICA_SERVER_URL", "https://multica.clouddeveloper.club")
MULTICA_TOKEN = os.environ.get("MULTICA_TOKEN", "REPLACE_WITH_MULTICA_TOKEN")
MULTICA_WORKSPACE_ID = os.environ.get(
    "MULTICA_WORKSPACE_ID", "270d4faf-4e8b-4d04-b8d3-ed88569abf4c"
)  # SKILLS开发验证(测试)

DB_HOST = os.environ.get("DB_HOST", "192.168.1.18")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASS = os.environ.get("DB_PASS", "REPLACE_WITH_DB_PASS")
DB_NAME = os.environ.get("DB_NAME", "rds-hd-skillsopr-db")

# ---- Multica 小队/智能体配置(ID + 显示名称, 按处理角色划分) ----
# 逻辑1: Issue 创建 -> 开发小队
SQUAD_DEV = os.environ.get("SQUAD_DEV", "c5287391-b8c3-4843-ae77-5610eecd6b04")
SQUAD_DEV_NAME = os.environ.get("SQUAD_DEV_NAME", "skills_dev")
# 逻辑3/4: PR 处理 -> 测试小队
SQUAD_TEST = os.environ.get("SQUAD_TEST", "c05558fa-8c50-43af-b80a-b9b0ea0c9650")
SQUAD_TEST_NAME = os.environ.get("SQUAD_TEST_NAME", "skills_test")
# 逻辑3/4: 测试完成后 -> 验证小队
SQUAD_VERIFY = os.environ.get("SQUAD_VERIFY", "9a6d8a29-b2db-4329-b6e4-60b6870af834")
SQUAD_VERIFY_NAME = os.environ.get("SQUAD_VERIFY_NAME", "skills_verify")
# 逻辑7: distribute_for_github 分支 PR -> 分发小队
SQUAD_DISTRIBUTE = os.environ.get("SQUAD_DISTRIBUTE", "77137221-0956-4d9c-b67c-71a210fb7816")
SQUAD_DISTRIBUTE_NAME = os.environ.get("SQUAD_DISTRIBUTE_NAME", "skills_distribute")
# 监听的分发目标分支(cherry-pick 合入该分支的 PR)
DISTRIBUTE_BRANCH = os.environ.get("DISTRIBUTE_BRANCH", "distribute_for_github")

# ---- devops-bot 迁移能力(2026-08-04, 对齐 devops-bot/src/event_processor.py) ----
# 全局忽略标签: 命中这些标签的 issue 事件整体忽略(不处理/不通知/不分发)
IGNORE_ISSUE_LABELS = [
    l.strip() for l in os.environ.get("IGNORE_ISSUE_LABELS", "codeowner,评测参考").split(",") if l.strip()
]
# PR 变更文件数上限: 超过则发评论提示并跳过处理(对齐 _check_pr_file_limit)
PR_FILE_LIMIT = int(os.environ.get("PR_FILE_LIMIT", "50"))
# 合并目标分支: 合入该分支(main/master)的 PR 点亮 node_repo 的同时分发到分发小队
# (对齐 devops-bot 的 merge_target_branch + _dispatch_merged_pr; distribute_for_github 仍走逻辑7)
MERGE_TARGET_BRANCH = os.environ.get("MERGE_TARGET_BRANCH", "main")
# push-sync: push 事件按仓库配置分发到 Multica, 格式 JSON: {"owner/repo": {"branch": "github", "squad_id": "...", "squad_name": "..."}}
PUSH_SYNC_CONFIG = {}
_push_sync_raw = os.environ.get("PUSH_SYNC_CONFIG", "")
if _push_sync_raw:
    try:
        PUSH_SYNC_CONFIG = json.loads(_push_sync_raw)
    except Exception:
        logger.warning("PUSH_SYNC_CONFIG 环境变量 JSON 解析失败, 忽略 push-sync 配置")


def _load_routing_sidecar():
    """可选: 从 EVENT_ROUTING_JSON 指向的 event-routing.json(devops-bot 配置)加载迁移配置。

    环境变量(IGNORE_ISSUE_LABELS/MERGE_TARGET_BRANCH/PUSH_SYNC_CONFIG)优先,
    未设置时用 sidecar 的 ignore_issue_labels / merge_target_branch / push_sync。
    便于与 devops-bot configs/event-routing.json 零配置对齐。
    """
    global IGNORE_ISSUE_LABELS, MERGE_TARGET_BRANCH, PUSH_SYNC_CONFIG
    path = os.environ.get("EVENT_ROUTING_JSON", "")
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"EVENT_ROUTING_JSON 加载失败: {e}")
        return
    if not os.environ.get("IGNORE_ISSUE_LABELS"):
        labels = data.get("ignore_issue_labels")
        if labels:
            IGNORE_ISSUE_LABELS = [str(l).strip() for l in labels if str(l).strip()]
    if not os.environ.get("MERGE_TARGET_BRANCH"):
        m = data.get("merge_target_branch")
        if m:
            MERGE_TARGET_BRANCH = str(m)
    if not os.environ.get("PUSH_SYNC_CONFIG"):
        ps = data.get("push_sync")
        if isinstance(ps, dict):
            PUSH_SYNC_CONFIG = ps


_load_routing_sidecar()

# 程序自身评论前缀: 收到带此前缀的评论事件直接跳过, 防止死循环
BOT_PREFIX = "[skills_bot]"
# ==================== 幂等 marker 常量表(S3 重构 2026-08-09) ====================
# 所有 _issue_has_bot_comment / _pr_has_bot_comment 的幂等 marker 集中管理。
# ⚠️ 硬性约定(历史教训 #163/#170/1.4): **marker 必须是评论正文的真实子串**,
# 否则幂等失效, 每个事件都发一条重复评论。改评论文案时同步改这里。
MARKERS = {
    # —— issue 判重/已存在 ——
    "dup_followup":        "已在原 issue 中跟进",      # 🔁 该需求与 #N 重复...
    "dup_closed_similar":  "与已关闭的",              # ℹ️ 与已关闭 #N 相似(2026-08-18 改共性子串: 需求/bug 文案都命中)
    "dup_pr_covered":      "该功能点已有 PR 实现",      # 📦 该功能点已有 PR 实现
    "dup_skill_covered":   "该功能点已有 skill 实现",   # 📦 该功能点已有 skill 实现
    "task_create_failed":  "开发任务创建失败",          # ⚠️ 开发任务创建失败告知(2026-08-18, 不涉及任务系统内部)
    "issue_linked_pr":     "已关联 PR",                # 📦 该 issue 已关联 PR #N, 不重复建任务(2026-08-18)
    "repo_has_skill":      "已在仓库中开发过",          # 📦 该需求对应的 skill 已在仓库中开发过
    "open_pr_has_skill":   "已有 PR 提交",              # 📦 该 skill 已有 PR 提交
    # —— 竞品/无关 ——
    "competitor_cloud":    "面向其他云厂商",            # ☁️ 该需求面向其他云厂商
    "unrelated_repo":      "与仓库项目范围无关",        # 该 Issue 与仓库项目范围无关
    # —— codeowner ——
    "codeowner_assigned":  "已按 codeowner 映射分配",   # 👤 已按 codeowner 映射分配
    # —— 质量/提示 ——
    "need_info":           "需求信息不完整",            # ⚠️ 需求信息不完整
    "dev_in_progress_lock":"开发仍按原需求进行",     # ⚠️ 该需求已在开发中...对 issue 的修改**不生效**, 开发仍按原需求进行
    "comment_change_rej":  "评论中的变更要求",        # ⚠️ 该需求已在开发中...评论中的变更要求(**改成**)**不生效**
    # —— 2.2 bug 分流 / 2.1.3 优化类提示 ——
    "bug_need_skill_name": "未提供具体的 skill 名称",   # ⚠️ 检测到该 issue 为问题/缺陷类, 但未提供具体...
    "bug_bad_skill_fmt":   "不是完整的 skill 名称",     # ⚠️ `xxx` 不是完整的 skill 名称
    "bug_skill_not_found": "未找到与",                  # ⚠️ 未找到与 `xxx` 完全匹配的 skill
    "bug_repo_no_skill":   "仓库中不存在 skill",        # ⚠️ 仓库中不存在 skill `xxx`
    "bug_pr_in_progress":  "已有 PR 在处理中",          # 📦 该 skill 已有 PR #N 在处理中(2.2.2 ③)
    "opt_need_skill_name": "未明确指定要优化的 skill",  # ⚠️ 检测到该需求为优化/修改类需求...
    "bot_fix_requested":   "已转为机器人修复",            # 🤖 已按您的要求转为机器人修复
    "pr_rescan_requested": "已按您的要求重新扫描",        # 🔄 已按您的要求重新扫描
    # —— PR 审查/合入 ——
    "pr_review_notified":  "已通知开发小队",            # PR 审查未通过, 已通知开发小队进行处理
    "pr_review_fix_req":   "审查报告修改后重新提交",    # @开发者 请根据上方 PR 审查报告修改后重新提交
    "pr_merged_dev_notice":"请关注后续处理",            # ✅ PR #N 已合入, @作者 请关注后续处理。
    "pr_merged_done":      "该需求已开发完成并合入主仓",# ✅ 该需求已开发完成并合入主仓 (PR #N 已合并)
    "pr_merged_issue_open":   "还有其他 PR 在处理中",  # 📌 关联的 PR #N 已合入, 但该 issue 还有其他 PR 在处理中...
    "pr_review_report":    "## PR 审查报告",            # 审查报告标题(pr_review_and_notify 幂等)
    "pr_file_limit":       "超过单次审查上限",          # ⚠️ 本 PR 包含 N 个变更文件, 超过单次审查上限(PR 文件数超限提示幂等)
    "pr_dup_skill":        "该 Skill 已有其他 open PR",  # 🚫 该 Skill 已有其他 open PR 在处理中(同 skill 多 PR 入口硬门槛幂等)
    # —— feature 类 LLM 判重(2026-08-18 Jaccard + LLM) ——
    "feature_dup":         "覆盖, 如有疑问请回复",   # 🔁 feature_dup 评论尾部: ...功能已被 #N 覆盖, 如有疑问请回复。
        "feature_related":     "请确认是否为重复创建",    # ⚠️ feature_related 评论: ...功能高度相似, 请确认是否为重复创建。
        "dev_identified":      "已识别SKILL开发任务",      # ✅ handle_issue 建任务成功时发出的 marker, 用于 retry 幂等防重复 webhook
    }
    # 机器人账号提交的 issue 整体忽略(2026-08-05 用户要求): skills_bot 等
# 自动化账号创建的 issue(如评测系统自动建单)不做任何处理 —— 不评论/不
# 打标签/不分配/不建 Multica 任务。逗号分隔, 大小写不敏感, 可环境变量覆盖。
BOT_AUTHOR_LOGINS = tuple(
    x.strip().lower() for x in
    os.environ.get("BOT_AUTHOR_LOGINS", "skills_bot,check_bot").split(",") if x.strip()
)
# Multica issue 页面 URL 前缀 (identifier 为 SKI-xx); 默认按工作区 slug 生成, 可显式覆盖
MULTICA_ISSUE_URL_PREFIX = os.environ.get(
    "MULTICA_ISSUE_URL_PREFIX", f"{MULTICA_SERVER}/skill-test/issues"
)
# DevFlow 前端页面域名(issue/PR 回复中展示给用户的流程跟踪链接); 默认可环境变量覆盖
DEVFLOW_BASE_URL = os.environ.get("DEVFLOW_BASE_URL", "http://skillsop.topxtopx.com")


def _devflow_url(requirement_id):
    """DevFlow 流程跟踪页 URL(issue/PR 回复中替代 multica 内部链接)。

    按 requirement 主键 id 定位: {DEVFLOW_BASE_URL}/devflow?requirementid={id}
    """
    return f"{DEVFLOW_BASE_URL}/devflow?requirementid={requirement_id}"

# ---- skill-issue 集成能力开关(默认全开, 环境变量可关) ----
ENABLE_LABELS = os.environ.get("ENABLE_LABELS", "1") == "1"          # 自动打标签
ENABLE_QUALITY_CHECK = os.environ.get("ENABLE_QUALITY_CHECK", "1") == "1"  # 需求质量检查(need-info 追问)
ENABLE_DUP_CHECK = os.environ.get("ENABLE_DUP_CHECK", "1") == "1"    # 重复 issue 检查
ENABLE_LLM_TEMPLATE_CHECK = os.environ.get("ENABLE_LLM_TEMPLATE_CHECK", "1") == "1"  # LLM 模板字段语义检查(2026-08-18 #98/#99)
# 已存在检查(2026-08-04 #112): 需求对应 skill 是否已在仓库中开发过 /
# DB 是否已有类似需求(进行中或已完成) -> 评论提示 + 关闭, 不重复建任务
ENABLE_EXISTING_CHECK = os.environ.get("ENABLE_EXISTING_CHECK", "1") == "1"
ENABLE_COMMENT_INTENT = os.environ.get("ENABLE_COMMENT_INTENT", "1") == "1"  # 评论意图处理
# 无关评论过滤(2026-08-05 用户要求): 灌水短文本/纯 emoji/与需求无关的闲聊
# 评论静默过滤, 不转发 Multica、不触发 need-info 追问; 判定异常 fail-open
ENABLE_COMMENT_FILTER = os.environ.get("ENABLE_COMMENT_FILTER", "1") == "1"
# LLM 兜底判断开关(预留位, 本次不实现调用; 后续开启时层3 未命中可走 LLM)
ENABLE_COMMENT_LLM_FILTER = os.environ.get("ENABLE_COMMENT_LLM_FILTER", "0") == "1"
# ---- LLM 兜底分类(2026-08-05 新增): 规则无结论/线索冲突时交给大模型判
# issue 是 需求(feature) 还是 问题(bug)。规则显式命中直接判(零成本);
# 仅当规则拿不准时调 LLM, 调用失败自动回退规则结论(fail-open, 不阻塞流程)。
ENABLE_LLM_CLASSIFY = os.environ.get("ENABLE_LLM_CLASSIFY", "1") == "1"
# 2026-08-06 用户要求: 规则判定后 LLM 复核, 冲突以 LLM 为准(默认开)。
# 关闭后回到纯规则快判 + 规则无结论时 LLM 兜底。
ENABLE_LLM_REVIEW = os.environ.get("ENABLE_LLM_REVIEW", "1") == "1"
# 2026-08-07 需求判重 LLM 语义确认(默认开): 规则弱信号(同产品+同功能词)
# 命中候选后, 调 LLM 确认是否语义重复 —— 防"同产品不同需求"被误关(#175
# 类问题)与"换说法"漏判。关闭后回到纯规则判重。
ENABLE_LLM_DUP_CHECK = os.environ.get("ENABLE_LLM_DUP_CHECK", "1") == "1"
LLM_API_BASE = os.environ.get("LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_API_KEY = os.environ.get(
    "LLM_API_KEY",
    os.environ.get("HERMES_CUSTOM_API_DEEPSEEK_COM_API_KEY", ""),
)
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "30"))
# 灌水词集: 命中且评论短(≤8 字)或无实质信息 -> 无关评论
IRRELEVANT_COMMENT_WORDS = [w.strip() for w in os.environ.get(
    "IRRELEVANT_COMMENT_WORDS",
    "好的,收到,顶,辛苦了,👍,+1,明白,了解,ok,OK,可以,行,谢谢,感谢,666,牛,支持,赞,不错,没问题,收到啦,好的呢,嗯嗯,哦哦,哈哈,了解啦,知道,知晓,确认,收到收到,棒,厉害,牛批,给力,真棒,优秀").split(",") if w.strip()]
# 功能词集: 评论需求相关性判定(层3); 命中任一即视为有实质内容
COMMENT_FUNC_WORDS = os.environ.get(
    "COMMENT_FUNC_WORDS",
    "查询,列表,创建,删除,修改,修复,问题,bug,报错,错误,希望,需要,补充,信息,需求,skill,技能,华为云,产品,接口,api,参数,返回,结果,数据,测试,验证,发布,上线,优化,改进,建议,方案,增加,添加,实现,开发").split(",")
# codeowner 分配: 非 Multica 开发 skill 的 issue 按 codeowner 映射分配给开发者
# (对齐 skill-issue assignee-guide; 仓库需有带 codeowner 标签的映射 issue)
ENABLE_CODEOWNER_ASSIGN = os.environ.get("ENABLE_CODEOWNER_ASSIGN", "1") == "1"
# PR 预校验: PR 创建事件先做配置检查(标题/描述/关联Issue/标签/Assignee),
# 对齐 skill-pr 阶段0; 发现问题评论提醒(幂等), 不阻塞后续流程
ENABLE_PR_PRECHECK = os.environ.get("ENABLE_PR_PRECHECK", "1") == "1"
# PR 审查报告: 预校验通过后输出完整审查报告(变更摘要/Skill 审查 30 项/
# 安全扫描/合并检查), 对齐 skill-pr 阶段1-5; 报告 fail 时阻断进测试
ENABLE_PR_REVIEW = os.environ.get("ENABLE_PR_REVIEW", "1") == "1"
# PR 评审人自动设置(2026-08-12 用户要求): 审查报告生成后按 PR 作者类型 +
# 关联 issue 标签设置评审人 —— 开发者 PR(非 bot)固定 DEFAULT_REVIEWER(胡盖);
# 机器人 PR 按 issue 标签: bug=skill 贡献者(提交历史推断) / feature=issue
# 创建人; 无 feature/bug 标签静默跳过; 找不到评审人回退 DEFAULT_REVIEWER。
# 约束(2026-08-12 API 实测): 评审人必须是仓库 collaborator(仓库外用户 400);
# PR 作者不能设自己为评审人。
ENABLE_PR_REVIEWER_ASSIGN = os.environ.get("ENABLE_PR_REVIEWER_ASSIGN", "1") == "1"
DEFAULT_REVIEWER = os.environ.get("DEFAULT_REVIEWER", "HugLovesWine")  # 胡盖
# 华为云产品相关性检查: 需求须命中华为云特征词或产品词库, 否则 need-info
# 追问(温和模式, 不直接关闭); 追问后补充仍不相关 -> invalid 关闭
# (2026-08-04 新增, 防"查天气"类无关 skill 需求直接进开发管线)
ENABLE_HUAWEI_RELATED_CHECK = os.environ.get("ENABLE_HUAWEI_RELATED_CHECK", "1") == "1"
# 华为云产品词库(逗号分隔, 环境变量可覆盖/追加): 命中即视为华为云产品相关
HUAWEI_CLOUD_PRODUCTS = [p.strip() for p in os.environ.get(
    "HUAWEI_CLOUD_PRODUCTS",
    "ecs,rds,obs,ges,eip,dns,ces,iam,cce,cci,swr,ucs,dws,mrs,css,modelarts,"
    "maas,billing,functiongraph,flexus,cbr,dcs,csm,sac,vpc,elb,nat,gaussdb,"
    "drs,dms,kafka,dis,roma,apig,cdn,waf,dbss,dew,kms,sfs,evs,ims,aom,lts,"
    "apm,cli,terraform,openjiuwen,弹性云服务器,云数据库,对象存储,图引擎,"
    "弹性公网IP,云监控,统一身份认证,云容器引擎,云容器实例,云搜索,数据仓库,"
    "数据湖,模型训练,盘古,函数工作流,云备份,分布式缓存,弹性负载均衡,"
    "虚拟私有云,云硬盘,镜像服务,云日志,应用性能管理,云审计,代码检查,"
    "编译构建,流水线"
).split(",") if p.strip()]

# 资源对象词(产品下的具体资源/功能对象): 用于 DB 相似需求判定的资源交集
# 校验。同一产品下「查询镜像列表」与「查询组织名称列表」资源对象不同,
# 是两个不同的 skill, 不能仅凭功能词交集(查询+列表)判为相似(#147 事故:
# swr 组织名称列表被误判与 #27 swr 镜像列表重复关闭)。两需求资源对象词
# 必须有交集才算相似(无资源词的按标题归一化互相包含兜底)。
# 注意: 词必须足够长/特异性, 避免子串误命中 —— 「组织名称列表」不应命中
# 「表」(名称表 的子串)、"SWR" 不应命中 "sk"(大写 SWR 的 S/K)。
RESOURCE_OBJECT_WORDS = (
    "镜像", "组织", "仓库", "命名空间", "实例", "集群", "节点", "容器", "工作负载",
    "服务", "函数", "配置", "密钥", "证书", "域名", "存储", "桶", "对象", "数据库",
    "备份", "快照", "任务", "作业", "流水线", "构建", "主机", "磁盘", "网络",
    "子网", "安全组", "弹性ip", "负载均衡", "网关", "日志", "监控", "告警", "指标",
    "告警规则", "用户", "角色", "权限", "策略", "分组", "项目", "空间", "队列",
    "事件", "订阅", "消费者组", "记录", "分区", "连接器", "数据源",
    "作业流", "工作流", "模板", "版本", "环境", "应用", "设备", "产品", "消息",
    "图表", "报表", "数据集", "模型", "训练作业", "推理", "终端节点", "资源",
    "套餐", "订单", "账单", "费用", "用量", "配额", "标签", "管理组", "账号",
    "访问密钥", "同步", "迁移", "转换", "校验", "扫描",
)

# 标签定义 (名称 -> 颜色, 带#前缀; GitCode API 要求), 与 skill-issue/labeling-guide.md 一致
LABEL_FEATURE = ("feature", "#428BCA")
LABEL_BUG = ("bug", "#d73a4a")
LABEL_NEED_INFO = ("need-info", "#d4c5f9")
LABEL_TRIAGED = ("triaged", "#bfdadc")
LABEL_INVALID = ("invalid", "#e4e669")
ALL_LABELS = [LABEL_FEATURE, LABEL_BUG, LABEL_NEED_INFO, LABEL_TRIAGED, LABEL_INVALID]

# 需求质量检查阈值(与 skill-issue/issue-validity-check.md 一致)
MIN_BODY_LEN = 30   # 正文少于 30 字视为信息不足
BUG_DETAIL_RE = re.compile(r"复现|步骤|日志|截图|堆栈|stack|traceback|报错信息|版本|环境", re.IGNORECASE)

# 2026-08 数据库表已改名(去中划线): t_skills_dev_requirement / t_skills_dev_flow
REQUIREMENT_TABLE = "t_skills_dev_requirement"
FLOW_TABLE = "t_skills_dev_flow"
VERSION_TABLE = "t_skill_pr_version_history"


# ==================== 通用 HTTP 工具 ====================
def http_json(method, url, body=None, headers=None, timeout=30):
    """发 HTTP 请求并解析 JSON 响应, 失败抛异常"""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {url}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败 {url}: {e.reason}")


def gitcode_get(path):
    return http_json("GET", f"{GITCODE_API_BASE}{path}", headers={"PRIVATE-TOKEN": GITCODE_TOKEN})


def gitcode_post(path, body):
    return http_json("POST", f"{GITCODE_API_BASE}{path}", body=body, headers={"PRIVATE-TOKEN": GITCODE_TOKEN})


def gitcode_patch(path, body):
    return http_json("PATCH", f"{GITCODE_API_BASE}{path}", body=body, headers={"PRIVATE-TOKEN": GITCODE_TOKEN})


def gitcode_delete(path):
    return http_json("DELETE", f"{GITCODE_API_BASE}{path}", headers={"PRIVATE-TOKEN": GITCODE_TOKEN})


def gitcode_post_form(path, fields):
    """form-encoded POST(创建标签等 API 要求), 失败抛异常"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(f"{GITCODE_API_BASE}{path}", data=data, method="POST")
    req.add_header("PRIVATE-TOKEN", GITCODE_TOKEN)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {path}: {detail}")


# ==================== GitCode 标签工具(skill-issue 集成) ====================
def ensure_labels(repo):
    """确保运营标签存在(不存在则创建); 幂等, 失败不阻断主流程"""
    if not repo:
        return
    try:
        existing = gitcode_get(f"/repos/{repo}/labels")
        existing = existing if isinstance(existing, list) else []
        names = {l.get("name") for l in existing if isinstance(l, dict)}
        for name, color in ALL_LABELS:
            if name not in names:
                try:
                    gitcode_post_form(f"/repos/{repo}/labels", {"name": name, "color": color})
                except Exception as e:
                    # 并发创建可能 409/已存在, 忽略
                    logger.warning(f"[labels] 创建标签 {name} 失败: {e}")
    except Exception as e:
        logger.warning(f"[labels] 获取标签列表失败: {e}")


def set_issue_labels(repo, number, labels):
    """设置 issue 标签(labels 为名称列表); 失败抛异常

    ⚠️ 2026-08-06 实测(#168): GitCode 的 PATCH /issues/{n} {"labels": "a,b"}
    **不是替换而是追加**语义 —— #168 三次打标(open need-info / update need-info /
    升级 feature,triaged)后标签残留为 [feature, need-info, need-info, triaged],
    need-info 重复且升级后从未被清掉(用户曾反馈"标签太多")。旧 pitfall 21 记载
    的"整体替换"行为已不成立(或 GitCode API 已变更)。对策: 先删除本脚本管理的
    全部标签(ALL_LABELS, DELETE /issues/{n}/labels/{name} 实测 204 幂等, 404
    容忍), 再 PATCH 追加目标标签, 等效整体替换; 非本脚本管理的标签(codeowner/
    评测参考/pr-merged 等)不受影响。

    2026-08-11 幂等优化(用户要求): 打标前先读当前标签, **目标标签已全部存在
    且无多余管理标签时直接跳过** —— 否则每次调用都"删 5 个 + 加 2 个",
    页面标签反复闪烁(issue #101 多次 update 事件实测: 每次重评估都触发
    完整删/加)。读标签失败/不确定时保守执行(不跳过, 维持原行为)。
    """
    if not repo or not number:
        return None
    labels = [l for l in labels if l]
    # 幂等: 当前标签 == 目标标签(且无多余管理标签) -> 跳过
    try:
        current = _api_issue_labels(repo, number)
        if current is not None:
            _target = set(labels)
            _current_mgmt = {l for l in current if l in dict(ALL_LABELS)}
            if _target == _current_mgmt:
                return None  # 已是最新, 不删不加
    except Exception:
        pass  # 读取失败保守执行
    for name, _color in ALL_LABELS:
        try:
            gitcode_delete(f"/repos/{repo}/issues/{number}/labels/{name}")
        except Exception:
            pass  # 404/网络抖动均容忍, 删除是尽力而为
    return gitcode_patch(f"/repos/{repo}/issues/{number}", {"labels": ",".join(labels)})


def close_gitcode_issue(repo, number):
    """关闭 GitCode issue(state 传 close 而非 closed); 失败抛异常"""
    if not repo or not number:
        return None
    return gitcode_patch(f"/repos/{repo}/issues/{number}", {"state": "close"})


# ==================== issue 分类/质量/判重(skill-issue 集成) ====================
def _llm_classify(title, body, as_review=False):
    """LLM 兜底分类: 判定 issue 是 feature(需求) 还是 bug(问题)。

    仅当规则 classify_issue 无结论/线索冲突时调用(见 classify_issue)。
    返回 (label, reason): label in ('feature', 'bug') 或 None(调用失败/解析失败)。
    失败不抛异常 —— 调用方回退规则结论(fail-open, 不阻塞流程)。

    2026-08-09 #246 修复: as_review=True(规则已判 bug 后的复核)时**禁止
    "倾向 feature"默认** —— 否则 LLM 把"huawei-cloud-nat-list有问题"这类
    明确 bug 表述(规则已命中"有问题")按"描述模糊默认需求"改判 feature,
    绕过 bug 分流被 LLM 判重关闭(#244/#246 实测三次同类误判)。
    """
    if not (ENABLE_LLM_CLASSIFY and LLM_API_KEY):
        return None, f"LLM 未启用或缺少 API key (ENABLE_LLM_CLASSIFY={ENABLE_LLM_CLASSIFY})"
    fallback_note = (
        "无法判断时倾向 feature(该仓库是 skill 开发仓库, 默认新需求)。"
        if not as_review else
        "注意: 这是对已判定为 bug 的复核, 除非有**明确证据**证明是需求"
        "(如 新增/实现/支持 某功能), 否则保持 bug 判定。"
    )
    prompt = (
        "你是 GitCode 仓库 issue 分类器。判断下面 issue 是「需求(feature)」还是「问题(bug)」。\n"
        "判定准则:\n"
        "- feature(需求): 要求新增/实现/查询/管理某资源或技能, 或希望支持某种能力。\n"
        "- bug(问题): 报告现有功能异常 —— 报错/失败/超时/结果不符预期/数据不对/页面异常等, "
        "通常带复现步骤或预期 vs 实际。\n"
        f"- {fallback_note}\n"
        "只输出 JSON: {\"label\": \"feature\"|\"bug\", \"reason\": \"一句话依据\"}\n"
        f"\n标题: {title or ''}\n正文: {(body or '')[:2000]}"
    )
    try:
        resp = http_json(
            "POST", f"{LLM_API_BASE}/chat/completions",
            body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 300,  # 推理模型: 120 会被 reasoning_content 占满致 content 为空
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=LLM_TIMEOUT,
        )
        content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        import re as _re
        m = _re.search(r"\{\s*\"label\"\s*:\s*\"(feature|bug)\"", content)
        if not m:
            return None, f"LLM 输出无法解析: {content[:120]}"
        label = m.group(1)
        reason = _re.search(r"\"reason\"\s*:\s*\"([^\"]*)\"", content)
        return label, (reason.group(1) if reason else content[:80])
    except Exception as e:
        logger.warning(f"[llm-classify] 调用失败, 回退规则: {e}")
        return None, f"LLM 调用失败: {e}"


def _llm_is_duplicate(cur_title, cur_body, cand_title, cand_status=""):
    """LLM 语义判重: 判断当前需求是否与候选需求语义重复(2026-08-07 新增)。

    仅在规则弱信号(同产品词+同功能词)命中候选后调用 —— 规则只能确认
    "词面相关", 无法区分"同产品不同需求"(如 查询vpc vs 创建vpc)与真重复。
    返回 (is_dup, reason); 调用失败返回 (False, reason) —— fail-open,
    宁可漏判不误关(关闭 issue 代价高, 可重新开; 误关则需求被拒)。
    """
    if not (ENABLE_LLM_DUP_CHECK and LLM_API_KEY):
        return False, f"LLM 判重未启用 (ENABLE_LLM_DUP_CHECK={ENABLE_LLM_DUP_CHECK})"
    prompt = (
        "你是 GitCode 仓库需求判重器。判断新需求是否与已有需求重复(要开发的 skill 本质相同)。\n"
        "判定准则:\n"
        "- 重复: 同一产品 + 同一操作类型(如 都查询同一产品的列表), 或本质同一功能\n"
        "- 不重复: 同产品但操作不同(查询 vs 创建/删除/修改); 不同产品即使词相似\n"
        "  (vpc 虚拟私有云 vs vpcep 终端节点); 迭代修改已有 skill(提到现有 skill\n"
        "  名称 + 修改意图词, 如改成只查数量)\n"
        f"新需求标题: {cur_title or ''}\n"
        f"新需求正文: {(cur_body or '')[:800]}\n"
        f"已有需求标题: {cand_title or ''}(状态: {cand_status or '未知'})\n"
        "只输出 JSON: {\"duplicate\": true|false, \"reason\": \"一句话依据\"}\n"
    )
    try:
        resp = http_json(
            "POST", f"{LLM_API_BASE}/chat/completions",
            body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 300,  # 推理模型: 120 会被 reasoning_content 占满致 content 为空
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=LLM_TIMEOUT,
        )
        content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        import re as _re5
        m = _re5.search(r"\"duplicate\"\s*:\s*(true|false)", content, _re5.IGNORECASE)
        if not m:
            return False, f"LLM 判重输出无法解析: {content[:120]}"
        is_dup = m.group(1).lower() == "true"
        reason = _re5.search(r"\"reason\"\s*:\s*\"([^\"]*)\"", content)
        return is_dup, (reason.group(1) if reason else content[:80])
    except Exception as e:
        logger.warning(f"[llm-dup] 调用失败, 按不重复放行: {e}")
        return False, f"LLM 调用失败: {e}"


def _llm_bug_is_same_issue(cur_title, cur_body, cand_title, cand_body, cand_status=""):
    """LLM 语义判重(问题类专用): 判断当前 bug 是否与候选 issue 是**同一问题**。

    2026-08-09 用户确认(判重 2.0 问题类语义): bug 类判重不走"PR/skills
    覆盖功能点"(PR 已合入恰说明 skill 存在且有 bug, 应修复), 而是 LLM
    判断"是否同一问题已在其他 issue 报告/跟进"。规则只按标题归一化会把
    "标题相似但现象不同"的 bug 误关 —— 如两个 issue 都写"xxx有问题"但
    一个是查询超时、一个是数据错误。
    判定准则:
      - 同一问题: 同一 skill + 同一异常现象/故障表现
      - 不同问题: 同一 skill 但现象不同(超时 vs 数据错); 不同 skill
    返回 (is_same, reason); 失败返回 (False, reason) fail-open(宁漏判不误关)。
    """
    if not (ENABLE_LLM_DUP_CHECK and LLM_API_KEY):
        return False, f"LLM 判重未启用 (ENABLE_LLM_DUP_CHECK={ENABLE_LLM_DUP_CHECK})"
    prompt = (
        "你是 GitCode 仓库 bug 判重器。判断新的问题报告(bug)是否与已有 issue 是**同一个问题**。\n"
        "判定准则:\n"
        "- 同一问题: 同一 skill + 同一异常现象/故障表现(如都报告 xxx 查询超时)\n"
        "- 不同问题: 同一 skill 但异常现象不同(查询超时 vs 返回数据错误); "
        "不同 skill 的问题; 一个是需求/优化、一个是 bug\n"
        f"新 bug 标题: {cur_title or ''}\n"
        f"新 bug 正文: {(cur_body or '')[:800]}\n"
        f"已有 issue 标题: {cand_title or ''}(状态: {cand_status or '未知'})\n"
        f"已有 issue 正文: {(cand_body or '')[:800]}\n"
        "只输出 JSON: {\"same\": true|false, \"reason\": \"一句话依据\"}\n"
    )
    try:
        resp = http_json(
            "POST", f"{LLM_API_BASE}/chat/completions",
            body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 300,
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=LLM_TIMEOUT,
        )
        content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        import re as _re7
        m = _re7.search(r"\"same\"\s*:\s*(true|false)", content, _re7.IGNORECASE)
        if not m:
            return False, f"LLM 判重输出无法解析: {content[:120]}"
        is_same = m.group(1).lower() == "true"
        reason = _re7.search(r"\"reason\"\s*:\s*\"([^\"]*)\"", content)
        return is_same, (reason.group(1) if reason else content[:80])
    except Exception as e:
        logger.warning(f"[llm-bug-dup] 调用失败, 按不同问题放行: {e}")
        return False, f"LLM 调用失败: {e}"


def _llm_find_bug_duplicate(repo, title, body, exclude_number=None):
    """问题类 LLM 语义判重(2026-08-09 #248 用户确认): 在已有 issue 中找
    **同一问题**(同一 skill + 同一异常现象)的候选。

    候选粗筛(减少 LLM 调用): 标题归一化完全一致 或 技能名交集命中;
    再对每个候选调 _llm_bug_is_same_issue 判断是否同一问题。
    返回 (issue dict 或 None, kind, reason); kind in ('open', 'closed_dup')。
    全部 LLM 失败/未命中返回 (None, None, reason) fail-open(宁漏判不误关)。
    """
    if not repo:
        return None, None, "无 repo"
    try:
        issues = gitcode_get(
            f"/repos/{repo}/issues?state=all&per_page=100"
            f"&sort=created&direction=desc")
        issues = issues if isinstance(issues, list) else []
    except Exception as e:
        logger.warning(f"[llm-bug-dup] 获取 issues 失败: {e}")
        return None, None, f"获取 issues 失败: {e}"

    def _norm(s):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())

    def _skill_names(s):
        found = set()
        for m in re.finditer(r"(huawei[-_]?cloud-[a-z0-9-]+|[\w-]+-skill|skill[:：]\s*[\w-]+)", s, re.IGNORECASE):
            found.add(re.sub(r"skill[:：]\s*", "", m.group(1)).lower())
        return found

    my_title_n = _norm(title)
    my_skills = _skill_names(f"{title} {body}")
    for iss in issues:
        num = iss.get("number") or iss.get("iid")
        if exclude_number and str(num) == str(exclude_number):
            continue
        state = (iss.get("state") or "").lower()
        if state not in ("open", "opened", "closed"):
            continue
        t_n = _norm(iss.get("title"))
        other_skills = _skill_names(f"{iss.get('title')} {iss.get('body') or iss.get('description') or ''}")
        # 粗筛: 标题归一化一致 或 技能名交集
        if not (my_title_n and t_n == my_title_n) and not (my_skills and my_skills & other_skills):
            continue
        # closed 候选: 跳过指向当前 issue 自身的判重(循环引用, #247)
        if state == "closed":
            try:
                comments = gitcode_get(f"/repos/{repo}/issues/{num}/comments?per_page=20")
                comments = comments if isinstance(comments, list) else []
                dup_link = None
                for c in comments:
                    cb = c.get("body") or ""
                    if cb.startswith(BOT_PREFIX) and any(k in cb for k in ("重复", "已在原 issue 中跟进", "避免重复开发")):
                        m = re.search(r"issues/(\d+)", cb)
                        if m and exclude_number and str(m.group(1)) == str(exclude_number):
                            dup_link = True  # 指向当前 issue 自身 -> 跳过
                        elif not m:
                            pass
                if dup_link:
                    continue
            except Exception:
                pass
        # LLM 判断是否同一问题
        is_same, reason = _llm_bug_is_same_issue(
            title, body, iss.get("title") or "", iss.get("body") or iss.get("description") or "",
            cand_status=state)
        if is_same:
            kind = "open" if state in ("open", "opened") else "closed_dup"
            return iss, kind, reason
    return None, None, "未发现同一问题"


def _llm_find_feature_duplicate(repo, title, body, number):
    """feature 类 issue 关键词粗筛 + LLM 批量判重。

    2026-08-19 用户要求(替代原 Jaccard): Jaccard 对中文短标题效果差,
    "代金券创建"vs"代金券激活"字符重叠不足 0.5 但语义高度相关。
    改为从标题+正文提取业务关键词, 对 open issues 做关键词匹配召回候选,
    再送 LLM 做语义判定。

    三道防线(2026-08-18 用户要求):
      ① 关键词筛出候选后送 LLM, LLM 低置信度不触发任何动作
      ② 评论包含人工确认引导, 不直接静默关闭
      ③ 仅由新 issue 判旧 issue, 不反向操作

    返回: (issue_dict, kind, reason) 其中 kind in
      ("feature_dup", "feature_related", None)
    """
    e = None  # 保存异常, 最后统一 fail-open

    # ---- 获取 open issues ----
    try:
        issues = gitcode_get(
            f"/repos/{repo}/issues?state=open&per_page=100"
            f"&sort=created&direction=desc")
        issues = issues if isinstance(issues, list) else []
    except Exception as ex:
        logger.warning(f"[feature-dup] 获取 issues 失败: {ex}")
        return None, None, None

    # ---- 阶段 1: 关键词粗筛(替代 Jaccard, 2026-08-19) ----
    # 从标题+正文提取业务关键词, 去重+去通用停用词
    # 中文用滑动窗口切 2-4 字子串(因连续汉字无空格, 整段匹配会漏)
    KEYWORD_STOPWORDS = {"帮我", "一个", "的", "请", "你", "我",
        "是", "在", "有", "和", "与", "了", "就", "都", "而", "及",
        "以及", "或者", "这个", "那个", "什么", "怎么", "如何", "为什么",
        "然后", "可以", "能够", "想要", "需要", "用于", "进行", "帮助",
        "please", "this", "that", "with", "from", "help", "want", "need"}
    _combined = f"{title or ''} {body or ''}"[:300]
    _keywords = set()
    for m in re.findall(r'[\u4e00-\u9fff]{2,}', _combined):
        # 长中文段切 2-4 字滑动窗口: "创建一个代金券创建的"
        # → "创建", "建一", "一个", "个代", "代金", "金券", "券创", ...
        for wlen in (4, 3, 2):
            for i in range(len(m) - wlen + 1):
                sub = m[i:i + wlen]
                if sub not in KEYWORD_STOPWORDS:
                    _keywords.add(sub)
    for m in re.findall(r'[a-zA-Z]{3,}', _combined):
        ml = m.lower()
        if ml not in KEYWORD_STOPWORDS:
            _keywords.add(ml)

    # 过滤掉全数字等无意义关键词
    _keywords = {k for k in _keywords
                 if not k.isdigit() and not all(c in '0123456789' for c in k)}

    candidates = []
    for iss in issues:
        if len(candidates) >= 20:  # 上限 20 候选, 防 LLM token 超限
            break
        n = iss.get("number") or iss.get("iid")
        if n and str(n) == str(number):
            continue  # 防线 3: 不自查
        iss_text = f"{iss.get('title') or ''} {iss.get('body') or iss.get('description') or ''}"[:300]
        if any(kw in iss_text for kw in _keywords):
            candidates.append(iss)

    if not candidates:
        return None, None, None

    # ---- 阶段 2: LLM 批量判重 ----
    cand_lines = []
    for idx, iss in enumerate(candidates, 1):
        n = iss.get("number") or iss.get("iid")
        t = iss.get("title") or ""
        b = (iss.get("body") or iss.get("description") or "")[:200]
        cand_lines.append(f"  #{idx}. #{n}: 标题={t}, 正文摘要={b}")
    cand_text = "\n".join(cand_lines)

    prompt = (
            "你是一个 issue 判重助手。请判断新提交的 feature issue 与仓库中已存在的 issue 是否功能重叠。\n\n"
            "---\n\n"
            "**判定准则（宁漏不误判）**：\n"
            "- duplicate: 功能点完全相同（同一产品 + 同一操作）\n"
            "- subset: 新 issue 的功能范围完全被某个候选 issue 覆盖（如候选做了 A+B+C，新 issue 只做 A）\n"
            "- superset: 新 issue 包含了某个候选 issue 的全部功能，且更大\n"
            "- partial: 有部分功能重叠，但并不包含彼此\n"
            "- different: 无明确功能重叠（同一产品但不同操作，如\"查询\"vs\"创建\"，不算重叠）\n\n"
            "**注意事项**：\n"
            "- 同一产品但**不同操作**（如\"ECS安全组创建\"vs\"ECS安全组查询\"）不视为重叠\n"
            "- 标题相似但具体功能点不同的，不视为重叠\n"
            "- 不确定时优先判为 different（宁漏不误报）\n\n"
            "请输出 JSON：\n"
            '```json\n'
            '{"matches": [{"num": 候选编号, "relation": "duplicate|subset|superset|partial|different", '
            '"confidence": "high|medium|low", "reason": "判断依据"}]}\n'
            '```\n'
        )

    # 防线 1: fail-open 默认无匹配
    default_result = {"matches": []}
    parsed = default_result

    try:
        full_prompt = prompt + f"\n**新 issue** #{number}:\n标题={title}\n正文摘要={(body or '')[:200]}\n\n**候选 issue 列表**：\n{cand_text}\n\n请输出 JSON。"
        resp = http_json(
            "POST", f"{LLM_API_BASE}/chat/completions",
            body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": full_prompt}],
                "temperature": 0,
                "max_tokens": 1000,
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=LLM_TIMEOUT,
        )
        if resp and isinstance(resp, dict):
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if content:
                m = re.search(r"\{.*\}", content, re.DOTALL)
                if m:
                    import json as _json
                    parsed = _json.loads(m.group(0))
    except Exception as ex:
        logger.warning(f"[feature-dup] LLM 调用异常: {ex}, fail-open")
        e = ex

    matches = parsed.get("matches", []) if isinstance(parsed, dict) else []
    best_match = None
    best_kind = None
    best_reason = None
    best_confidence = "low"

    for m_entry in matches:
        idx = m_entry.get("num", 0)
        relation = (m_entry.get("relation") or "").strip().lower()
        confidence = (m_entry.get("confidence") or "low").strip().lower()
        reason = m_entry.get("reason", "")

        if relation in ("different", "none", ""):
            continue
        if idx < 1 or idx > len(candidates):
            continue
        target = candidates[idx - 1]
        # 防线 3: 只针对旧 issue (当前是候选)
        target_num = target.get("number") or target.get("iid")

        # 置信度决定处理方式
        is_high = confidence == "high"
        is_at_least_medium = confidence in ("high", "medium")

        if relation in ("duplicate", "subset", "superset"):
            if is_high:
                # 防线 1: high 才 auto-close
                best_match = target
                best_kind = "feature_dup"
                best_reason = reason
                best_confidence = "high"
                break  # 第一个匹配就够
            elif is_at_least_medium:
                # 防线 2: medium 只评论不关闭
                best_match = target
                best_kind = "feature_related"
                best_reason = reason
                best_confidence = "medium"
                # 继续搜, 可能找到 high
            else:
                # low: 忽略
                if e is None:
                    e = f"low confidence {relation}"
        elif relation == "partial":
            # partial 无论置信度都只评论
            if confidence in ("high", "medium", "low"):
                best_match = target
                best_kind = "feature_related"
                best_reason = reason
                best_confidence = "medium"
                # 继续找, high 优先

    if best_match:
        dup_url = best_match.get("html_url") or f"https://gitcode.com/{repo}/issues/{best_match.get('number')}"
        return best_match, best_kind, best_reason

    return None, None, None


def _llm_pr_covers_issue(repo, title, body, pr_numbers=None):
    """判重 2.0 第 1 步: LLM 判断当前 PR 列表(open + merged)是否已覆盖 issue 功能点。

    2026-08-07 用户要求: 判重从规则匹配升级为 LLM 语义判断 —— 判断当前代码仓
    中 PR 列表能否包含 issue 功能点, 若能则提示已有该功能(贴 PR 链接)并关闭。
    PR 范围: state=all 最近 30 条, 过滤掉 closed 未合并(功能未进仓库, 不算
    已实现), 仅 open(开发中)+ merged(已合入)进入 LLM 判断。
    返回 (covered, pr_number, reason); 失败返回 (False, None, reason) fail-open。
    """
    if not (ENABLE_LLM_DUP_CHECK and LLM_API_KEY):
        return False, None, "LLM 判重未启用"
    if not repo:
        return False, None, "无 repo"
    try:
        prs = gitcode_get(f"/repos/{repo}/pulls?state=all&per_page=30&sort=updated&direction=desc")
        prs = prs if isinstance(prs, list) else []
    except Exception as e:
        logger.warning(f"[llm-pr-cover] 获取 PR 列表失败: {e}")
        return False, None, f"获取 PR 列表失败: {e}"
    if not prs:
        return False, None, "无 PR"

    lines = []
    for p in prs:
        p_state = str(p.get("state") or "").lower()
        if p_state == "closed" and not p.get("merged"):
            continue  # closed 未合并: 功能未进仓库, 排除
        num = p.get("number")
        title_p = (p.get("title") or "")[:120]
        body_p = (p.get("body") or "").replace("\n", " ")[:200]
        mark = "merged" if p.get("merged") else ("open" if p_state in ("open", "opened") else p_state)
        lines.append(f"#{num} [{mark}] {title_p} | {body_p}")
    if not lines:
        return False, None, "无有效 PR(open+merged)"

    prompt = (
        "你是 GitCode 代码仓库需求判重器。判断当前仓库的 PR 列表中, 是否有 PR "
        "已经实现了该 issue 的功能点(语义等价, 不要求字面相同)。\n"
        "PR 状态说明: open=开发/审查中, merged=已合入主仓; 两者都算已覆盖; "
        "closed 未合并的 PR 已排除(功能未进仓库, 不算已实现)。\n"
        f"issue 标题: {title or ''}\n"
        f"issue 正文: {(body or '')[:800]}\n"
        "当前 PR 列表:\n"
        f"{chr(10).join(lines)}\n"
        "只输出 JSON: {\"covered\": true|false, \"pr_number\": 数字, \"reason\": \"一句话依据\"}\n"
    )
    # 空响应/无 covered 字段时重试一次(推理模型偶发 content 为空, 2026-08-07 实测)
    for _attempt in range(2):
        try:
            resp = http_json(
                "POST", f"{LLM_API_BASE}/chat/completions",
                body={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 300,  # 推理模型: 120 会被 reasoning_content 占满致 content 为空
                },
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                timeout=LLM_TIMEOUT,
            )
            content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            import re as _re6
            m = _re6.search(r"\"covered\"\s*:\s*(true|false)", content, _re6.IGNORECASE)
            if m:
                covered = m.group(1).lower() == "true"
                if not covered:
                    return False, None, "LLM 判 PR 未覆盖"
                m_num = _re6.search(r"\"pr_number\"\s*:\s*(\d+)", content)
                pr_number = int(m_num.group(1)) if m_num else None
                m_rs = _re6.search(r"\"reason\"\s*:\s*\"([^\"]*)\"", content)
                reason = m_rs.group(1) if m_rs else content[:80]
                if pr_number is None:
                    return False, None, "LLM 判 covered 但无 pr_number"
                return True, pr_number, reason
            # 无 covered 字段 -> 重试(可能是 content 空)
            if _attempt == 0:
                continue
            return False, None, f"LLM 输出无法解析: {content[:120]}"
        except Exception as e:
            logger.warning(f"[llm-pr-cover] 调用失败, 按未覆盖放行: {e}")
            return False, None, f"LLM 调用失败: {e}"
    return False, None, "LLM 判重重试耗尽"


def _llm_select_skill_candidates(repo, title, body, max_candidates=15, batch_size=20):
    """判重 2.0 阶段1: LLM 从全量 skill 清单选候选(语义筛选, 分批并发)。

    背景(2026-08-18 用户拍板): 词表粗筛有盲区 —— issue #59 "创建skill的
    skill" 与已有 huawei-cloud-skill-creator 字面零交集(中文需求 vs 英文
    skill 名), 粗筛分数=0 被筛掉, LLM 未见到候选 -> 漏判。阶段1 让 LLM
    自己选候选, 不受词表限制; 分批(20/批)并发: 全量单批喂注意力分散
    (8-07 实测 73 个仅 1/3 命中), 分批后每批注意力集中, 并发把耗时压到
    ≈ 单批。
    返回 [(name, path, summary)] 或 [] (LLM 判无相关) 或 None (失败,
    调用方回退词表粗筛)。
    """
    if not (ENABLE_LLM_DUP_CHECK and LLM_API_KEY):
        return None
    if not repo:
        return None
    try:
        products = _get_repo_products(repo)
        if not products:
            return None
        idx = _skill_capability_index(repo)
    except Exception as e:
        logger.warning(f"[llm-dup-select] 清单获取失败: {e}")
        return None
    all_items = []
    for name, path in sorted(products.items()):
        info = idx.get(name.lower().replace("_", "-"))
        all_items.append((name, path, ((info or {}).get("summary") or "")))
    if not all_items:
        return None
    batches = [all_items[i:i + batch_size]
               for i in range(0, len(all_items), batch_size)]
    issue_text = f"{title or ''} {(body or '')[:400]}"

    def _one_batch(batch, bi):
        lines = []
        for name, path, summary in batch:
            if summary:
                lines.append(f"- {name} ({path}) | {summary[:120]}")
            else:
                lines.append(f"- {name} ({path})")
        prompt = (
            "你是 GitCode 仓库需求判重器的候选筛选器。给定一个 issue 的功能"
            "描述, 从下面的 skill 清单中选出**可能实现该功能**的 skill"
            "(语义相关即可, 宁可多选不可漏选)。\n"
            f"issue 描述: {issue_text}\n"
            f"skill 清单(第 {bi + 1} 批):\n{chr(10).join(lines)}\n"
            "只输出 JSON: {\"candidates\": [\"skill名1\", \"skill名2\"]}\n"
            "无相关 skill 输出 {\"candidates\": []}"
        )
        for _attempt in range(2):
            try:
                resp = http_json(
                    "POST", f"{LLM_API_BASE}/chat/completions",
                    body={"model": LLM_MODEL,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0, "max_tokens": 500},
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    timeout=LLM_TIMEOUT,
                )
                content = (((resp.get("choices") or [{}])[0].get("message")
                            or {}).get("content") or "").strip()
                m = re.search(r'"candidates"\s*:\s*\[([^\]]*)\]', content,
                              re.IGNORECASE)
                if m:
                    return re.findall(r'"([^"]+)"', m.group(1))
                if _attempt == 0:
                    continue
                return []
            except Exception as e:
                logger.warning(f"[llm-dup-select] 批次 {bi + 1} 失败: {e}")
                return None
        return None

    results = []
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(5, len(batches))) as _ex:
            _futs = {_ex.submit(_one_batch, b, i)
                     for i, b in enumerate(batches)}
            for _f in _cf.as_completed(_futs):
                try:
                    results.append(_f.result())
                except Exception:
                    results.append(None)
    except Exception as e:
        logger.warning(f"[llm-dup-select] 并发执行失败: {e}")
        return None
    # 失败批次 >= 半数 -> 候选不完整会误导精判, 回退粗筛
    if not results or sum(1 for r in results if r is None) > len(results) // 2:
        return None
    # 合并去重 + 映射回 (name, path, summary)
    seen = set()
    out = []
    for r in results:
        if not r:
            continue
        for n in r:
            nk = n.strip().lower().replace("_", "-")
            if not nk or nk in seen:
                continue
            seen.add(nk)
            for name, path in products.items():
                if name.lower() == nk or nk in name.lower() \
                        or name.lower() in nk:
                    info = idx.get(name.lower().replace("_", "-"))
                    out.append((name, path, ((info or {}).get("summary") or "")))
                    break
    return out[:max_candidates] if out else []


def _llm_skills_covers_issue(repo, title, body):
    """判重 2.0 第 2 步: LLM 判断仓库已有 skills 是否已覆盖 issue 功能点。

    2026-08-07 用户要求: 第 1 步(PR 覆盖)不满足时, 判断仓库中已有 skills
    能否包含 issue 功能点, 若能则提示已有该功能(贴 skill 名称)并关闭。
    skills 清单来源: 遍历仓库 skills/<cat>/<name> 目录(复用 _get_repo_products
    缓存), 附带 SKILL.md frontmatter description 辅助语义判断。
    返回 (covered, skill_name, reason); 失败返回 (False, None, reason) fail-open。
    """
    if not (ENABLE_LLM_DUP_CHECK and LLM_API_KEY):
        return False, None, "LLM 判重未启用"
    if not repo:
        return False, None, "无 repo"
    try:
        products = _get_repo_products(repo)  # {product_lower: path}, 缓存 10 分钟
        if not products:
            return False, None, "仓库无 skills 目录"
    except Exception as e:
        logger.warning(f"[llm-skills-cover] 获取 skills 目录失败: {e}")
        return False, None, f"获取 skills 目录失败: {e}"

    # 组装 skill 清单(名称 + 目录路径); 不读每个 SKILL.md 的 description ——
    # 71+ skill × contents API = 71+ 次调用, 高频触发 GitCode 429 限流(实测),
    # 且读取失败时描述为空反让 LLM 犹豫。skill 名 huawei-cloud-xxx 本身已
    # 表达功能, 路径补充归类信息。
    # 2026-08-07 稳定性修复: 73 个 skill 全量喂 LLM, 推理模型注意力分散,
    # 漏看目标 skill(实测 3 次仅 1 次命中)。先按产品词粗筛缩小候选集
    # (标题+正文命中 HUAWEI_CLOUD_PRODUCTS 或与 issue 文本有子串交集的
    # skill 名), 候选 ≤15 个再喂 LLM 精判; 无候选时仍全量(兜底)。
    # 2026-08-17 粗筛缺陷修复(实测): 旧子串规则 `[a-z0-9]{4,}` 匹配文本中
    # 的 huawei/cloud/list/skill 等高频词, 98/98 skill 全部命中 -> 候选
    # 变全量, 再按字母序 [:15] 截断, 真正相关的 skill(vpc-list/dds-list
    # 等字母序靠后)被截掉, LLM 看不到目标 -> 判重形同虚设(实测 6 案例中
    # 2 个确定重复全漏报)。改为**相关度打分排序**: 产品词段精确命中(+10) /
    # skill 名完整出现在文本(+20) / 功能词段子串(+5), 按分数降序取前 15。
    text_all = f"{title or ''} {body or ''}".lower()
    import re as _re8
    # 2026-08-17 方案A: 候选清单用能力索引的 summary(description+核心命令),
    # 替代纯"名字+路径" —— LLM 判重基于真实能力而非猜名字。索引构建失败
    # (限流/异常) fail-open 回退纯名字(旧行为)。
    idx = _skill_capability_index(repo)
    # 粗筛: 相关度打分, 分数>0 才进候选, 按分数降序(不再字母序截断)
    scored = []
    for name, path in sorted(products.items()):
        name_l = name.lower()
        score = 0
        # 1. 产品词段精确命中(huawei-cloud-<prod>-xxx): +10
        for prod in HUAWEI_CLOUD_PRODUCTS:
            p = prod.lower()
            if p and len(p) >= 3 and p in text_all:
                if prod.isascii():
                    if _re8.search(r"(?<![a-z0-9])" + _re8.escape(p) + r"(?![a-z0-9])", name_l):
                        score += 10
                        break
                elif p in name_l:
                    score += 10
                    break
        # 2. skill 名完整出现在文本(名称级强信号): +20
        if name_l in text_all:
            score += 20
        # 3. 功能词段子串(huawei-cloud-xxx 的 xxx 段, 排除 huawei/cloud 高频): +5
        core = re.sub(r"^huawei[-_]?cloud-", "", name_l)
        if len(core) >= 4 and core in text_all:
            score += 5
        if score > 0:
            scored.append((score, name, path))
    scored.sort(key=lambda x: (-x[0], x[1]))  # 相关度降序
    # 组装候选: 2026-08-18 两阶段改造 —— 阶段1(LLM 语义选候选)优先,
    # 失败回退词表粗筛(旧行为)。阶段2 精判逻辑不变。
    cand_llm = _llm_select_skill_candidates(repo, title, body)
    if cand_llm is not None:
        if not cand_llm:
            return False, None, "LLM 语义候选为空(未发现相关 skill)"
        lines = []
        for _n, _p, _s in cand_llm:
            if _s:
                lines.append(f"{_n} ({_p}) | {_s}")
            else:
                lines.append(f"{_n} ({_p})")
        _note = f"(LLM 语义候选 {len(lines)} 个)"
    else:
        # 回退: 词表粗筛打分(旧行为, 阶段1 失败/未启用时保持原逻辑)
        candidates = []
        for _, name, path in scored[:15]:
            info = idx.get(name.lower().replace("_", "-"))
            if info and (info.get("summary") or "").strip():
                candidates.append(f"{name} ({path}) | {info['summary']}")
            else:
                candidates.append(f"{name} ({path})")
        lines = candidates if candidates else [f"{n} ({p})" for n, p in sorted(products.items())[:80]]
        _note = f"(粗筛 {len(candidates)} 个)" if candidates else "(全量)"
    if not lines:
        return False, None, "无 skill 清单"

    prompt = (
        "你是 GitCode 代码仓库需求判重器。判断仓库已有的 skills 中, 是否有 skill "
        "已经实现了该 issue 的功能点(语义等价, 不要求字面相同)。\n"
        f"issue 标题: {title or ''}\n"
        f"issue 正文: {(body or '')[:800]}\n"
        f"仓库已有 skills 候选清单{_note}:\n"
        f"{chr(10).join(lines)}\n"
        "只输出 JSON: {\"covered\": true|false, \"skill_name\": \"技能名或空\", \"reason\": \"一句话依据\"}\n"
    )
    # 空响应/无 covered 字段时重试一次(推理模型偶发 content 为空, 2026-08-07 实测)
    for _attempt in range(2):
        try:
            resp = http_json(
                "POST", f"{LLM_API_BASE}/chat/completions",
                body={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 300,  # 推理模型: 120 会被 reasoning_content 占满致 content 为空
                },
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                timeout=LLM_TIMEOUT,
            )
            content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            import re as _re7
            m = _re7.search(r"\"covered\"\s*:\s*(true|false)", content, _re7.IGNORECASE)
            if m:
                covered = m.group(1).lower() == "true"
                m_sk = _re7.search(r"\"skill_name\"\s*:\s*\"([^\"]*)\"", content)
                skill_name = m_sk.group(1) if m_sk else ""
                m_rs = _re7.search(r"\"reason\"\s*:\s*\"([^\"]*)\"", content)
                reason = m_rs.group(1) if m_rs else content[:80]
                if not covered:
                    return False, None, "LLM 判 skills 未覆盖"
                if not skill_name:
                    return False, None, "LLM 判 covered 但无 skill_name"
                return True, skill_name, reason
            # 无 covered 字段 -> 重试(可能是 content 空)
            if _attempt == 0:
                continue
            return False, None, f"LLM 输出无法解析: {content[:120]}"
        except Exception as e:
            logger.warning(f"[llm-skills-cover] 调用失败, 按未覆盖放行: {e}")
            return False, None, f"LLM 调用失败: {e}"
    return False, None, "LLM 判重重试耗尽"


def classify_issue(title, body):
    """按 skill-issue labeling-guide 判定 issue 主标签: feature / bug / invalid / None

    返回 (主标签名, 判定依据列表)。无法判定返回 (None, reasons)。
    """
    text = f"{title or ''} {body or ''}"
    reasons = []
    # 2026-08-13 #120 实测修复: **强 bug 信号直接判 bug, 跳过 LLM 复核**。
    # 评测问题单标题("基本功评测发现N条问题")与正文强 bug 词(报错/错误/异常/
    # 失败/卡死/崩溃等)是确凿缺陷信号 —— LLM 复核有 ~7% 概率改判 feature
    # (15 次实测 1 次改判, #120 事故), 强信号下复核改判弊大于利。
    strong_bug = re.search(
        r"评测(报告|问题|发现)|发现\d+\s*条问题|问题单",
        title or "", re.IGNORECASE
    )
    if not strong_bug:
        strong_bug = re.search(
            r"报错|错误|异常|失败|卡死|崩溃|闪退|挂了|乱码|不生效|白屏|500|堆栈|traceback",
            text, re.IGNORECASE
        )
    if strong_bug:
        reasons.append(f"命中强 bug 信号: {strong_bug.group(0)}")
        return "bug", reasons
    # bug 线索: 缺陷报告常见表述(标题/正文)
    # 覆盖: 显式 bug 词 + 结果不符预期(不对/不正确/有误) + 查询/获取类失败 +
    #       无响应/卡死 + 无法使用 + 数据问题(缺失/重复/数量) + 崩溃/乱码
    # 注意: 裸「不能/失败/超时/重复/为空/空白」易与 feature 词冲突
    #       (如「能不能」「支持失败重试」「支持去重」), 必须用组合模式
    bug_hint = re.search(
        r"bug|故障(?!排查|定位|诊断|处理|恢复|检测|演练|发现|分析|预防|自愈|监控|告警|预警|隔离|转移|切换|注入|解决)|报错|错误|不生效|白屏|异常|缺陷|漏洞|500|堆栈|traceback"
        r"|不对|不正确|有误|有问题|出问题|错了"
        r"|查询不到|查不到|获取不到|拿不到|读不到|找不到|检索不到"
        r"|没反应|无响应|不响应|卡住|卡死|超时(了|导致|报错)|请求超时|连接超时|执行超时|无结果"
        r"|一直等待|总是等待|老是等待|等待中|等不到|等很久|等半天|一直转|转圈|卡在|卡着|一直卡"
        r"|无法|不能用|用不了|不可用|执行失败|调用失败|请求失败|操作失败|查询失败"
        r"|创建失败|删除失败|更新失败|导入失败|导出失败|启动失败|运行失败|获取失败"
        r"|失效|缺失|缺少|丢失|不见了|结果为空|返回为空|数据为空|列表为空"
        r"|重复(数据|记录|条目|出现)|出现重复"
        r"|崩溃|闪退|挂了|乱码|显示空白|页面空白|内容空白|错乱|数量不对|结果不对",
        text, re.IGNORECASE
    )
    # feature 线索: 标题含 建议/需求/新增/支持/实现/希望/能不能
    feat_hint = re.search(
        r"建议|需求|新增|支持|实现|希望|能不能|是否|可以(加|做|增加)|开发(一个|一款|一套|一)", text, re.IGNORECASE
    )
    if bug_hint:
        reasons.append(f"命中 bug 线索: {bug_hint.group(0)}")
        # 2026-08-06 用户要求: 规则判定后 LLM 复核, 冲突以 LLM 为准。
        # LLM 失败时回退规则结论(fail-open, 不阻塞)。
        # 2026-08-09 #246: 复核传 as_review=True(禁止"倾向 feature"默认)——已修。
        # 2026-08-13 #120 实测修复(方案1): LLM 改判 feature 必须有需求证据词
        # (新增/实现/支持XX功能等), 否则保持 bug —— LLM 对"文档缺口"类
        # 评测问题单有 ~7% 概率误判 feature, 无条件采纳会覆盖规则 bug 结论。
        if ENABLE_LLM_REVIEW and ENABLE_LLM_CLASSIFY and LLM_API_KEY:
            llm_label, llm_reason = _llm_classify(title, body, as_review=True)
            if llm_label:
                if llm_label != "bug":
                    feat_evidence = re.search(
                        r"新增|新建|实现|支持.{0,8}(功能|能力|查询|管理)|开发(一个|一款|一套|新)",
                        f"{title or ''} {(body or '')[:500]}", re.IGNORECASE
                    )
                    if feat_evidence:
                        reasons.append(f"规则判 bug, LLM 复核改判(有需求证据 {feat_evidence.group(0)!r}): {llm_reason}")
                        return llm_label, reasons
                    reasons.append(f"规则判 bug, LLM 复核改判但无需求证据, 保持 bug: {llm_reason}")
                    return "bug", reasons
                reasons.append(f"规则判 bug, LLM 复核一致: {llm_reason}")
            else:
                reasons.append(f"LLM 复核失败({llm_reason}), 保持规则 bug")
        return "bug", reasons
    if feat_hint:
        reasons.append(f"命中 feature 线索: {feat_hint.group(0)}")
        # 规则判 feature 后 LLM 复核: 冲突(LLM 判 bug)以 LLM 为准。
        if ENABLE_LLM_REVIEW and ENABLE_LLM_CLASSIFY and LLM_API_KEY:
            llm_label, llm_reason = _llm_classify(title, body)
            if llm_label:
                if llm_label != "feature":
                    reasons.append(f"规则判 feature, LLM 复核改判: {llm_reason}")
                    return llm_label, reasons
                reasons.append(f"规则判 feature, LLM 复核一致: {llm_reason}")
            else:
                reasons.append(f"LLM 复核失败({llm_reason}), 保持规则 feature")
        return "feature", reasons
    # 兜底: 有描述但无明确线索 -> 交给 LLM 判(语义理解口语化/边界表述),
    # LLM 失败/未启用再按 feature 兜底(仓库是 skill 开发仓库, 默认新需求)。
    if (body or "").strip():
        if ENABLE_LLM_CLASSIFY and LLM_API_KEY:
            llm_label, llm_reason = _llm_classify(title, body)
            if llm_label:
                reasons.append(f"规则无明确线索, LLM 判定: {llm_reason}")
                return llm_label, reasons
            reasons.append(f"规则无明确线索, LLM 兜底失败({llm_reason}), 按 feature 兜底")
            return "feature", reasons
        reasons.append("无明确 bug/feature 线索, 按 feature 兜底")
        return "feature", reasons
    return None, reasons


def _created_label_override(payload, repo, number, title, body):
    """2026-08-13 用户要求(需求3): 创建时已带 feature/bug 标签则以标签为准。

    优先读 payload 自带标签(open 事件通常能带), 读不到再 API 兜底;
    命中 feature/bug 直接返回该标签, 跳过 classify_issue 规则+LLM。
    返回 (label_or_None, reasons)。失败 fail-open 走原 classify_issue。
    """
    reasons = []
    labels = _get_issue_labels_from_payload(payload)
    if not labels and repo and number:
        labels = _api_issue_labels(repo, number) or []
    if labels:
        for lb in labels:
            lb_l = str(lb).strip().lower()
            if lb_l in ("feature", "bug"):
                reasons.append(f"创建时自带标签 {lb} 优先, 跳过重新分类")
                return lb_l, reasons
    return None, reasons


def _parse_template_sections(body):
    """将 issue body 按 ### 标题切分成 {header: content} 字典。
    未检测到模板结构返回空 dict。
    """
    if not body or not body.strip():
        return {}
    sections = re.split(r'^###\s+', body.strip(), flags=re.MULTILINE)
    result = {}
    for sec in sections:
        if not sec.strip():
            continue
        lines = sec.split('\n', 1)
        header = lines[0].strip()
        content = lines[1].strip() if len(lines) > 1 else ''
        result[header] = content
    return result


def _detect_template_type(sections):
    """根据 section headers 判断使用了哪个模板。
    返回 'feature' / 'bug' / None。
    """
    if not sections:
        return None
    feat_keys = ["华为云服务", "解决哪些问题", "哪些操作"]
    bug_keys = ["问题描述", "触发场景", "问题影响"]
    feat_score = sum(1 for k in feat_keys if any(k in h for h in sections))
    bug_score = sum(1 for k in bug_keys if any(k in h for h in sections))
    if feat_score >= 2:
        return "feature"
    if bug_score >= 2:
        return "bug"
    return None


def _rule_check_issue_quality(title, body, label):
    """规则层质量检查(零LLM成本)。

    返回 (ok: bool|None, reason: str)
    - False: 确定不合格(直接 need_info 追问)
    - True: 确定合格(不需要 LLM)
    - None: 规则无法确定(需要 LLM 进一步语义判断)
    """
    # 1. 正文为空 -> 不合格
    if not body or not body.strip():
        return False, "正文为空"

    # 2. 需求类标题未指定 skill 名称 -> 不合格
    if label != "bug":
        PREFIX = "[帮我创建一个skill]"
        if title.startswith(PREFIX):
            name = title[len(PREFIX):].strip()
            if not name:
                return False, "标题未指定 skill 名称"
            if name.isdigit() or (len(set(name)) <= 2 and len(name) >= 3):
                return False, f"标题中 skill 名无效: '{name[:20]}'"

    # 3. 无模板结构 -> 由通用质量检查兜底
    sections = _parse_template_sections(body)
    if not sections:
        return None, ""

    template_type = _detect_template_type(sections)
    if not template_type:
        return None, ""

    # 4. 模板字段空内容检查
    tmpl_checks = {
        "feature": [
            ("华为云服务", "涉及的华为云服务未填写"),
            ("解决哪些问题", "Skill 功能描述未填写"),
            ("哪些操作", "涉及的操作未填写"),
        ],
        "bug": [
            ("问题描述", "问题描述未填写"),
            ("触发场景", "触发场景未填写"),
            ("问题影响", "问题影响未填写"),
        ],
    }
    checks = tmpl_checks.get(template_type, [])
    for keyword, err_msg in checks:
        matched = False
        for h, c in sections.items():
            if keyword in h:
                matched = True
                c_stripped = (c or "").strip()
                if not c_stripped:
                    return False, err_msg
                if c_stripped.isdigit():
                    return False, err_msg
                if len(set(c_stripped)) <= 2 and len(c_stripped) >= 3:
                    return False, err_msg
                break
        if not matched:
            return False, err_msg

    return True, ""


def _llm_check_template_quality(title, body, label):
    """LLM 层质量检查: 对规则无法确定的场景做语义判断。

    检查必填字段是否具有实质内容(非空但废话/无意义填充等)。
    返回 (ok: bool, reason: str)
    失败时 fail-open: 返回 (True, '') 不阻塞流程。
    """
    if not LLM_API_KEY:
        return True, "LLM 检查未启用"

    sections = _parse_template_sections(body)
    template_type = _detect_template_type(sections) if sections else "feature"

    if template_type == "feature":
        required_str = (
            "1. 涉及的华为云服务(target_service)\n"
            "2. Skill主要解决哪些问题(target_do)\n"
            "3. 涉及哪些操作(target_operation)"
        )
    else:
        required_str = (
            "1. 问题描述\n"
            "2. 问题触发场景\n"
            "3. 问题影响"
        )

    section_text = "\n".join(f"[{h}]\n{c or ''}" for h, c in sections.items())

    prompt = (
        "你是 GitCode 仓库 issue 质量检查员。检查下面 issue 是否按要求填写了模板字段。\n"
        f"该 issue 使用了「{template_type}」模板，以下字段为必填：\n{required_str}\n\n"
        "判断每个必填字段的内容是否具有实质意义。有实质意义的判定标准：\n"
        "- 描述了具体的技术内容（如：ECS 实例列表查询、弹性IP创建等）\n"
        "- 包含合理的业务场景描述\n"
        "- 内容不是纯数字、纯标点、重复字符\n"
        "- 内容不是无意义的填充文字（如 '测试'、'123'、'aaa'、重复复制粘贴等）\n\n"
        "只输出 JSON: {\"ok\": true/false, \"missing_fields\": [字段名列表], \"reason\": \"一句话原因\"}\n\n"
        f"标题: {title or ''}\n"
        f"正文:\n{section_text[:2000]}"
    )

    try:
        resp = http_json(
            "POST", f"{LLM_API_BASE}/chat/completions",
            body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 300,
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=LLM_TIMEOUT,
        )
        content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        m = re.search(r'"ok"\s*:\s*(true|false)', content, re.IGNORECASE)
        if not m:
            logger.warning(f"[llm-quality] 输出无法解析: {content[:120]}")
            return True, ""
        is_ok = m.group(1).lower() == "true"
        reason_m = re.search(r'"reason"\s*:\s*"([^"]*)"', content)
        reason = reason_m.group(1) if reason_m else content[:80]
        return is_ok, reason
    except Exception as e:
        logger.warning(f"[llm-quality] 调用失败, fail-open: {e}")
        return True, ""


def issue_quality_ok(title, body, label=None):
    """需求质量检查(issue-validity-check.md): 返回 (是否通过, 原因)

    - bug 类型: 必须有复现步骤/日志/截图/堆栈等关键信息
    - feature/其他: 正文不少于 MIN_BODY_LEN 字
    2026-08-09 #244 修复: 正文长度剔除 skill 名(skill 名是固定格式占位,
    不算实质描述) —— "huawei-cloud-ges-graph有问题,请协助定位下" 去掉
    skill 名后仅 9 字, 应判信息不足; 否则英文 skill 名占字符数导致空话
    通过校验。
    """
    body = (body or "").strip()
    if not body:
        return False, "正文为空"
    # 剔除 skill 名后的实质描述(不把固定格式 skill 名算作描述内容)
    stripped = re.sub(
        # skill 名固定为 ASCII(huawei-cloud-xxx / xxx-skill / skill: name),
        # 不能用 \w(Unicode 下匹配中文)—— "skill：实现查询..." 会把整段
        # 中文描述当 skill 名吃掉导致实质描述虚减(#6 实测 15 字 <30 误拦)
        r"huawei[-_]?cloud-[a-z0-9-]+|[a-zA-Z0-9_-]+-skill|skill[:：]?\s*[a-zA-Z0-9_-]+",
        "", body, flags=re.IGNORECASE)
    stripped = re.sub(r"[，。！？、,.!?；;\s]+", "", stripped)
    if label == "bug":
        if len(body) < MIN_BODY_LEN:
            return False, f"正文仅 {len(body)} 字(<{MIN_BODY_LEN}), 缺少复现步骤/日志等信息"
        if not BUG_DETAIL_RE.search(body):
            return False, "缺少复现步骤/日志/截图/堆栈等关键信息"
        return True, "bug 信息完整"
    # feature 与默认
    if len(stripped) < MIN_BODY_LEN:
        return False, f"实质描述仅 {len(stripped)} 字(<{MIN_BODY_LEN}), 需求描述不够清晰"
    return True, "需求描述清晰"


def _need_info_requirement(repo, number, issue_id, issue_url, title, body,
                           author_login, author_name, created, updated, qreason,
                           extra_hint=""):
    """统一 need_info 处理(2026-08-19 方案A): 仅评论追问, 不入库, 幂等依赖评论 marker。

    收敛 3 处重复块(质量检查 / 2.2 bug 非 bot / 通用 codeowner):
    1. 评论"需求信息不完整"(幂等 marker "需求信息不完整")
    extra_hint: 追问补充建议(默认通用文案)。

    2026-08-19 方案A(用户要求): 竞品云/判重关闭等不入库场景对齐。
    不再写 DB, 幂等靠 _issue_has_bot_comment(marker="need_info")。
    后续 handle_issue_update / handle_issue_comment 也改用 marker 检测。
    """
    if repo and number:
        _hint = extra_hint or (
            "请补充以下信息后重新提交, 以便开发小队准确实施:\n"
            f"- 具体功能/修复目标\n- 期望行为与边界条件\n"
            f"- (bug) 复现步骤、日志、截图、环境版本"
        )
        _q_msg = f"⚠️ 需求信息不完整: {qreason}。\n{_hint}"
        if not _issue_has_bot_comment(repo, number, MARKERS["need_info"]):
            safe_notify(gitcode_issue_comment, repo, number, bot_comment(_q_msg))
    return {"action": "need_info", "reason": qreason, "requirement_id": None}


def _close_unrelated_issue(repo, number, req_id=None):
    """完全无关内容直接 invalid 关闭(不入库, 幂等靠评论 marker)。

    2026-08-13 #125 修复: 质量检查失败分支也先判无关 —— 「上海天气很好」
    灌水 issue 因描述短先收质量追问(且被 need-info 路径错误入库), 用户
    补充后才走 2.1.4 无关关闭。无关内容无论描述长短都应直接关闭,
    不追问不入库(与 2.1.4 语义一致)。req_id 非 None 时(update 路径已有
    need_info/pending 记录)一并删除 flow+requirement, 保证无关零残留。
    """
    if repo and number:
        if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
            try:
                safe_notify(set_issue_labels, repo, number, ["invalid"])
            except Exception as e:
                logger.warning(f"[unrelated-close] 打 invalid 标签失败: {e}")
        msg = (
            "⚠️ 该 issue 内容与华为云产品 skill 开发无关"
            "(未识别到对应华为云产品/场景), 本仓库仅开发华为云相关的 skill, "
            "已按无关内容关闭。如确属华为云需求, 请补充说明对应的"
            "华为云产品(如 ECS / RDS / OBS 等)后重新提交。"
        )
        if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
            safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
        safe_notify(close_gitcode_issue, repo, number)
    if req_id:
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM `{FLOW_TABLE}` WHERE requirement_id=%s",
                        (req_id,))
                    cur.execute(
                        f"DELETE FROM `{REQUIREMENT_TABLE}` WHERE id=%s",
                        (req_id,))
            logger.info(f"[unrelated-close] 已清理无关 issue 的 DB 记录: requirement_id={req_id}")
        except Exception as e:
            logger.warning(f"[unrelated-close] 清理 DB 记录失败: {e}")
    return {"action": "invalid_unrelated", "reason": "无华为云特征, 按无关内容关闭",
            "requirement_id": None}


def _jaccard_similarity(t1, t2):
    """字符级 Jaccard 相似度。
    预处理: 剥离模板前缀, 只保留中文字符, 低频 ASCII 词(skill 等)跳过。
    注意: #79 vs #81 ≈ 0.67; 同产品不同操作(代金券查询 vs 激活) ≈ 0.43。
    """
    def _clean(s):
        s = (s or "").lower()
        s = re.sub(r"\[帮我创建一个skill\]", "", s)
        # 保留中文字符 + 数字
        return re.sub(r"[^\u4e00-\u9fff0-9]", "", s)
    a = set(_clean(t1))
    b = set(_clean(t2))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def find_duplicate_issue(repo, title, body, exclude_number=None, skip_skill_match=False):
    """实时判重: 列出 issues(含 closed, 2026-08-07 扩展), 标题/正文相似度匹配;
    返回 (issue dict 或 None, kind); kind in ('open', 'closed_dup', 'closed_other')

    判重策略(issue-validity-check.md):
      1. 提取技能名(huawei-cloud-xxx / xxx-skill)精确匹配
      2. 标题归一化后完全一致
      3. 标题核心词(Jaccard 相似度) > 0.5

    2026-08-07 用户要求(#187): 查重范围从 open 扩到**全部状态**(含 closed)——同
    一需求反复提交(#179/#182/#186/#187 同标题)时, 原 issue 已关闭也能识别。
    2026-08-07 讨论(#193 后): closed 命中要区分关闭原因 —— 只有**因重复关闭**
    的(closed_dup, 该 issue 上有 bot 判重评论)才可判重关闭; 其他原因关闭的
    (closed_other: 已完成/need-info/invalid 等)只提示不关闭, 避免误伤"修正后
    重提/迭代增强"的合法新需求。
    """
    if not repo:
        return None, None
    try:
        issues = gitcode_get(
            f"/repos/{repo}/issues?state=all&per_page=100"
            f"&sort=created&direction=desc")
        issues = issues if isinstance(issues, list) else []
    except Exception as e:
        logger.warning(f"[dup-check] 获取 issues 失败: {e}")
        return None, None

    def _norm(s):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())

    def _skill_names(s):
        found = set()
        # 只匹配具体技能名(huawei-cloud-xxx / xxx-skill / skill: xxx), 不匹配裸词 skill
        for m in re.finditer(r"(huawei[-_]?cloud-[a-z0-9-]+|[\w-]+-skill|skill[:：]\s*[\w-]+)", s, re.IGNORECASE):
            found.add(re.sub(r"skill[:：]\s*", "", m.group(1)).lower())
        return found

    def _closed_kind(iss, cur_number=None):
        """判断 closed issue 的关闭原因: 因重复关闭 -> 'closed_dup', 否则 'closed_other'。

        判据: 该 issue 上是否有 [skills_bot] 判重评论(含 "重复" / "已在原 issue
        中跟进" / "避免重复开发" 等关键词)。查不到评论(API 失败)时保守返回
        'closed_other'(不误关)。
        2026-08-09 #247: 判重评论**指向当前 issue 自己**时返回 'self_dup'
        (循环引用 —— 当前 issue 是它的重复源, 不应反过来判重当前 issue)。
        #246/#247 完全同标题: #247 被"与 #246 重复"关闭, #246 恢复处理时
        #247 不能反过来把 #246 判重关闭。
        """
        try:
            comments = gitcode_get(f"/repos/{repo}/issues/{iss.get('number')}/comments?per_page=20")
            comments = comments if isinstance(comments, list) else []
            for c in comments:
                cb = c.get("body") or ""
                if cb.startswith(BOT_PREFIX) and any(k in cb for k in
                        ("重复", "已在原 issue 中跟进", "避免重复开发")):
                    if cur_number:
                        # 评论里指向当前 issue 的链接(如 [#246](.../issues/246))
                        m = re.search(r"issues/(\d+)", cb)
                        if m and str(m.group(1)) == str(cur_number):
                            return "self_dup"
                    return "closed_dup"
        except Exception as e:
            logger.warning(f"[dup-check] 查 closed issue #{iss.get('number')} 评论失败: {e}")
        return "closed_other"

    my_title_n = _norm(title)
    my_skills = _skill_names(f"{title} {body}")
    for iss in issues:
        num = iss.get("number") or iss.get("iid")
        if exclude_number and str(num) == str(exclude_number):
            continue
        state = (iss.get("state") or "").lower()
        t_n = _norm(iss.get("title"))
        # 1) 技能名精确匹配(本仓库技能名是唯一标识, 最强判重信号)
        # 2026-08-09 #241 修复: skip_skill_match=True(bug 类)时**跳过技能名
        # 匹配** —— 同一 skill 可以有开发需求/优化需求/bug 报告多个独立
        # 工作项(#241 bug"运行失败" vs #218 优化"高并发", 都提到
        # maas-tokens-usage 但性质不同, 不应互相判重)。
        other_skills = _skill_names(f"{iss.get('title')} {iss.get('body') or iss.get('description') or ''}")
        matched = False
        if not skip_skill_match:
            matched = bool(my_skills and my_skills & other_skills)
        # 2) 标题归一化后完全一致(去除标点/空格/大小写后逐字相同)
        if not matched:
            matched = bool(my_title_n and t_n == my_title_n)
        if not matched:
            continue
        if state in ("open", "opened"):
            return iss, "open"
        # closed: 区分关闭原因(#247: 指向当前 issue 自己的判重 -> 跳过)
        kind = _closed_kind(iss, cur_number=exclude_number)
        if kind == "self_dup":
            continue
        return iss, kind
    return None, None


# ==================== 已存在检查(2026-08-04 #112 新增) ====================
# 背景: 「支持查询eip列表」类需求已被开发过多次(仓库已有 eip 产品线,
# DB 已有 repoed/tested 的历史需求), 但旧判重只查 open issues + 标题完全
# 一致, 重复需求仍会新建 Multica 任务。新增两道检查:
#   1. find_skill_in_repo: 需求对应产品/技能是否已在仓库中开发过

_REPO_PRODUCTS_CACHE = {}  # repo -> (timestamp, {product_lower: path})
# 2026-08-17 方案A: skill 能力索引缓存 repo -> (timestamp, {product_lower: {path, summary, name}})
_SKILL_CAPABILITY_CACHE = {}
# 2026-08-11 限流防雪崩: webhook 每次事件都是**新进程**, 进程内缓存永远
# 不命中 -> 每个事件都全量扫描(15-25 次 contents API) -> skills_bot 账号
# 50次/分钟配额秒没, 429 后不缓存又反复重扫(雪崩, issue #101 实测)。
# 改为**文件持久化缓存**: 完整结果 TTL 600s; 限流期部分结果 TTL 60s 短缓存,
# 避免雪崩(60s 后重试完整扫描)。
_REPO_PRODUCTS_CACHE_FILE = "/tmp/gitcode_repo_products_cache.json"


def _repo_products_cache_load():
    try:
        with open(_REPO_PRODUCTS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_json_cache(path):
    """读 JSON 缓存文件, 失败返回 {}。2026-08-17 方案A 索引缓存复用。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _repo_products_cache_save(data):
    try:
        with open(_REPO_PRODUCTS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _get_repo_products(repo):
    """获取仓库已开发的产品目录映射 skills/<cat>/<product> -> 路径(缓存 10 分钟)。

    用 contents API 遍历类目(仓库 tree API 递归不完整, 实测只返回 20 条)。
    2026-08-07 判重 2.0 修复: 仓库结构是 skills/<cat>/<subcat>/<skill-name>
    (如 skills/network/ops/huawei-cloud-vpc-list), 旧实现只取一级子目录
    (network/ 下的 dns/eip/nat/ops), 把 subcat 当 product、漏掉真正的 skill
    名。改为递归两层: 一级子目录若是 huawei-cloud-* 即 skill 根; 否则进入
    二级子目录取 skill 名。
    2026-08-07 限流修复: GitCode API 高频调用会 429 限流(实测连续多次
    contents 调用触发)。a) 任一类目失败不整体失败, 但**空结果不缓存**
    (否则 10 分钟缓存空); b) 429 时短暂退避重试(最多 2 次)。
    2026-08-08 限流健壮性修复: 429 限流期类目失败被静默跳过, 产物清单
    不完整(实测 44/74/53 波动, vpc-list/ascend 随机缺失), 导致 2.2 误判
    "仓库无此 skill"。已修: a) 重试 2->4 次、退避 1.5s*attempt; b) 统计
    失败类目, 失败 >3 个(12 类目)时视为不完整**不缓存**(下次重拉);
    c) 类目失败时降级单类目重拉(重试窗口内可能恢复)。
    """
    now = time.time()
    # 文件缓存(跨进程): 完整结果 600s / 部分结果 60s
    _fc = _repo_products_cache_load()
    _frec = _fc.get(repo)
    if _frec:
        _fttl = 600 if _frec.get("complete") else 60
        if now - _frec.get("ts", 0) < _fttl:
            return _frec.get("products") or {}
    cached = _REPO_PRODUCTS_CACHE.get(repo)
    if cached and now - cached[0] < 600:
        return cached[1]
    # 2026-08-18 本地化: 优先本地镜像一次 ls-tree -r 拿全量目录树构建
    # 产品映射(无 429 风险, 不存在"限流返回不完整清单"问题 —— #101 事故
    # 根因就是 API 限流期 products 不完整导致误判"仓库无此 skill")。
    # 构建成功后同样走文件缓存; 失败返回 None 回退原 API 逻辑。
    _local_products = _mirror_products(repo)
    if _local_products is not None:
        _REPO_PRODUCTS_CACHE[repo] = (now, _local_products)
        try:
            _repo_products_cache_save({**_repo_products_cache_load(),
                                       repo: {"products": _local_products,
                                              "ts": now, "complete": True}})
        except Exception:
            pass
        return _local_products
    # 2026-08-11 修复: 类目名不能写死 —— skillspackage 实际用 "compute"(单数)
    # 而旧列表写死 "computing"(复数), GET /skills/computing 404/空, 导致
    # skills/compute/ 下的 skill 永远扫不到, bug 分流误判"仓库无此 skill"
    # (issue #101 实测: 已上传 skill 仍报不存在)。改为先探测 skills/ 根目录,
    # 用**真实类目名**拉取, 兼容单数/复数/自定义类目。
    cats = None
    try:
        _root = gitcode_get(f"/repos/{repo}/contents/skills?per_page=100")
        if isinstance(_root, list):
            cats = [it.get("name") for it in _root
                    if it.get("type") == "dir" and it.get("name")]
    except Exception as _e:
        logger.warning(f"[repo-products] 探测 skills/ 根目录失败: {_e}")
    if not cats:
        # 探测失败回退固定列表 —— 必须同时含 compute/computing(单复数兼容):
        # skillspackage 实际用 compute(单数), 旧列表只有 computing(复数)导致
        # 429 探测失败回退后仍扫不到(2026-08-11 issue #101 二次实测)。
        cats = ["ai", "bigdata", "bss", "cdi", "compute", "computing",
                "container", "devtools", "monitoring", "network", "security",
                "solution", "storage"]
    products = {}

    def _fetch(path, retries=4):
        """GET contents path, 429 退避重试; 返回 list 或 None"""
        for attempt in range(retries + 1):
            try:
                data = gitcode_get(f"/repos/{repo}/contents/{path}?per_page=100")
                return data if isinstance(data, list) else []
            except Exception as e:
                if "429" in str(e) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return None  # 失败(含 429 重试耗尽)

    failed_cats = []
    for cat in cats:
        try:
            data = _fetch(f"skills/{cat}")
            if data is None:
                failed_cats.append(cat)
                continue  # 该类目失败, 跳过(不缓存整体结果)
            for it in data:
                if it.get("type") != "dir":
                    continue
                name = it["name"]
                path = f"skills/{cat}/{name}"
                # 一级目录名是 huawei-cloud-* / xxx-skill -> 直接是 skill 根
                if re.search(r"(huawei[-_]?cloud-|[\w-]+-skill)", name, re.IGNORECASE):
                    products[name.lower().replace("_", "-")] = path
                    continue
                # 否则是子类目(如 ops/), 递归一级取 skill 名
                data2 = _fetch(path)
                if data2 is None:
                    continue
                for it2 in data2:
                    if it2.get("type") == "dir":
                        n2 = it2["name"]
                        products[n2.lower().replace("_", "-")] = f"{path}/{n2}"
        except Exception:
            continue
    # 空结果不缓存(防限流期缓存空清单); 失败类目 >3 个(12 类目)视为
    # 不完整也不缓存(2026-08-08 限流健壮性: 不完整清单会误判"仓库无 skill")
    if products and len(failed_cats) <= 3:
        _REPO_PRODUCTS_CACHE[repo] = (now, products)
        # 文件持久化(跨进程): 完整结果
        _repo_products_cache_save({**_repo_products_cache_load(),
                                   repo: {"ts": now, "products": products,
                                          "complete": True}})
    elif failed_cats:
        logger.warning(f"[repo-products] 限流不完整: 失败类目 {len(failed_cats)} 个 {failed_cats}, 不缓存")
        # 2026-08-11 限流防雪崩: 部分结果也短缓存 60s —— 下一个事件直接用
        # 旧清单, 不立即全量重扫(否则每个事件都打满配额, 雪崩死循环);
        # 60s 后缓存过期自然重试完整扫描。
        if products:
            _repo_products_cache_save({**_repo_products_cache_load(),
                                       repo: {"ts": now, "products": products,
                                              "complete": False}})
    return products


# 修改/迭代类需求意图词: 提到已有 skill 名称 + 命中这些词 -> 是"修改已有
# skill"而非"新开发同名 skill", 跳过已存在检查(#142 事故: 「xxx 这个skill
# 现在要改成只查询数量」被 find_skill_in_repo 按名称全称匹配误判重复关闭)
SKILL_MODIFY_WORDS = (
    "改成", "修改", "变更", "更新", "升级", "优化", "调整", "改为", "重写",
    "改造", "重构", "迭代", "改进", "增强", "扩充", "扩展", "改版", "变更为",
    "修复", "不对", "有问题", "报错", "错误", "异常", "故障", "失效", "卡住",
    "白屏", "查不到", "数量不对", "结果不对",
)

def is_skill_modification(title, body):
    """判断需求是否为"修改/迭代已有 skill"(而非新开发同名 skill)。

    2026-08-05 #142 事故: 「huawei-cloud-gaussdb-count 这个skill现在要改成
    只查询数量」是修改已有 skill 的迭代需求, 仓库里已有该 skill 目录是
    正常的(就是要改它), 但旧逻辑 find_skill_in_repo 按名称全称匹配判为
    "重复开发"直接关闭。识别规则: 文本提到 huawei-cloud-xxx skill 名称
    **且** 命中修改/修复意图词 -> 视为迭代需求。
    """
    text = f"{title or ''} {body or ''}"
    if not re.search(r"huawei[-_]?cloud-[\w-]+", text, re.IGNORECASE):
        return False
    return any(w in text for w in SKILL_MODIFY_WORDS)


# 优化类需求意图词(2026-08-07 判重 2.0): 对已有 skill 的优化/增强/迭代/修改
# 类需求不走判重逻辑 —— 仓库里已有同名/同产品 skill 是正常的(就是要优化它),
# 判重会误关。必须配合 skill 名全称出现才生效(防"帮我优化一下"无 skill 名时
# 误跳过判重)。
OPTIMIZE_WORDS = (
    "优化", "增强", "改进", "升级", "扩充", "扩展", "迭代", "完善", "提升",
    "调整", "变更", "改成", "改为", "重写", "重构", "改造",
    "修改", "改版", "变更为",
    # 2026-08-09 #231 修复: 移除"新增/增加" —— 它们是**开发需求**的核心词
    # (「新增华为云 MaaS 列表查询 Skill」是标准开发需求), 不是优化意图词。
    # 误含导致 2.1.3 把新增需求误判为"优化类缺 skill 名"而拦截提示修改。
    # 迭代/增强类需求由"增强/扩展/迭代/升级"等词覆盖。
    # 2026-08-17 修复: 移除"修复" —— 纯 bug 语义词, 评测问题单/缺陷报告
    # 标题正文常含"修复"(修复建议/待修复), 误判优化类会跳过 bug 判重
    # (#43/#44/#45 同份 P0 评测报告: #44/#45 标题"问题修改"命中"修改"跳过
    # 判重各自建任务, #45 撞 Multica duplicate 崩溃)。bug 修复意图由
    # classify_issue 强 bug 信号 + label=="bug" 分流处理, 不依赖优化词表。
)


def is_optimization_request(title, body):
    """判断需求是否为"优化/增强/迭代已有 skill"类(2026-08-07 判重 2.0)。

    判重 2.0 规则 3: 优化类需求不走判重逻辑。识别: 文本提到已有 skill 名
    (huawei-cloud-xxx 或 xxx-skill) **且** 命中优化/修改意图词。
    返回 bool。
    """
    text = f"{title or ''} {body or ''}"
    if not re.search(r"(huawei[-_]?cloud-[\w-]+|[\w-]+-skill)", text, re.IGNORECASE):
        return False
    return any(w in text for w in OPTIMIZE_WORDS)


def has_optimization_intent(title, body):
    """判断文本是否命中优化/修改意图词(不要求出现 skill 名)。

    2026-08-07 判重 2.1.3: 优化类需求必须给出唯一具体 skill 名称 ——
    「帮我优化一下查询速度」无 skill 名但确属优化意图, 应提示用户补充
    skill 名称而非判无关关闭。与 is_optimization_request 的区别: 后者
    要求提到 skill 名(用于判重跳过), 本函数仅看意图词(用于 2.1.3 拦截)。
    """
    text = f"{title or ''} {body or ''}"
    return any(w in text for w in OPTIMIZE_WORDS)


def _skill_capability_index(repo, build_on_miss=False):
    """读取仓库 skill 能力索引缓存: {product_lower: {"path", "summary", "name"}}。

    2026-08-17 方案A(用户确认): LLM 判重不能只看 skill 名 —— 候选侧也要
    有真实能力描述。遍历仓库全部 skill, 读每个 SKILL.md 的 frontmatter
    description(功能一句话) + 核心命令/工作流章节片段, 提炼成能力摘要。
    判重时把摘要喂 LLM, 替代"只喂名字"。

    **2026-08-17 性能改造**: 原实现缓存未命中时同步全量构建(65-157s,
    webhook 每次事件都是新进程, 进程内缓存不命中 → issue #35 处理 165s
    卡在索引构建)。现改为**只读缓存**: 命中返回索引, 未命中返回 {}
    (调用方降级为名字粗筛), 构建交给后台预热脚本
    (scripts/warm_skill_capability.py, cron 每 10 分钟)。
    build_on_miss=True 时仍同步构建(供预热脚本/手动调用)。

    缓存: 内存 600s + 文件 /tmp/gitcode_skill_capability_cache.json
    (完整结果 600s / 部分结果 60s)。
    """
    now = time.time()
    # 文件缓存(跨进程): 完整结果 600s / 部分结果 60s
    _fc = _load_json_cache("/tmp/gitcode_skill_capability_cache.json")
    _frec = _fc.get(repo) if isinstance(_fc, dict) else None
    if _frec:
        _fttl = 600 if _frec.get("complete") else 60
        if now - _frec.get("ts", 0) < _fttl:
            return _frec.get("index") or {}
    cached = _SKILL_CAPABILITY_CACHE.get(repo)
    if cached and now - cached[0] < 600:
        return cached[1]
    if not build_on_miss:
        # 2026-08-17: 缓存未命中不阻塞构建(判重降级为名字粗筛),
        # 索引由后台预热脚本构建
        return {}
    return _build_skill_capability_index(repo)


def _build_skill_capability_index(repo):
    """同步构建 skill 能力索引并写缓存(预热脚本/手动调用用)。

    2026-08-17 限流优化: 98 个 skill × 1 次 contents API 远超 skills_bot
    50次/分钟配额, 实测 41/98 失败(429), 索引不完整 -> complete=False
    短缓存 60s -> 判重频繁降级。改为**分片构建**: 每片 25 个 skill,
    片间 sleep 40s 等配额恢复, 失败项单次重试。构建时间长(3-4 分钟)
    但**只在后台预热脚本执行**, 请求链路不感知。
    """
    now = time.time()
    try:
        products = _get_repo_products(repo)
    except Exception as e:
        logger.warning(f"[skill-capability] 获取 skills 目录失败: {e}")
        return {}
    if not products:
        return {}
    idx = {}
    failed = 0
    total = len(products)
    items = sorted(products.items())
    # 2026-08-18 本地化: 镜像可用时全量本地读(一次 ls-tree + 逐个 git show,
    # 无配额限制, 不分片不 sleep, 构建从 3-4 分钟降到秒级); 镜像不可用才
    # 回退原分片 API 逻辑(每片 25 个 + sleep 40s 等配额恢复)。
    if _ensure_mirror(repo):
        for name, path in items:
            try:
                md = _fetch_skill_md_summary(repo, path)
                if md is None:
                    failed += 1
                    continue
                summary, desc = md
                idx[name.lower().replace("_", "-")] = {
                    "path": path, "summary": summary, "name": name,
                    "description": desc,
                }
            except Exception:
                failed += 1
    else:
        _SHARD = 25
        _SHARD_SLEEP = 40  # 秒: 等 50次/分钟 配额恢复
        for _s in range(0, len(items), _SHARD):
            _shard = items[_s:_s + _SHARD]
            for name, path in _shard:
                try:
                    md = _fetch_skill_md_summary(repo, path)
                    if md is None:
                        failed += 1
                        continue
                    summary, desc = md
                    idx[name.lower().replace("_", "-")] = {
                        "path": path, "summary": summary, "name": name,
                        "description": desc,
                    }
                except Exception:
                    failed += 1
            if _s + _SHARD < len(items) and failed < total:
                # 非最后一片且仍有失败空间 -> 等配额恢复再拉下一片
                logger.info(f"[skill-capability] 分片 {_s//_SHARD+1} 完成, 等 {_SHARD_SLEEP}s 恢复配额")
                time.sleep(_SHARD_SLEEP)
    if not idx:
        return {}
    _complete = failed <= max(3, total // 10)
    _SKILL_CAPABILITY_CACHE[repo] = (now, idx)
    try:
        _fc = _load_json_cache("/tmp/gitcode_skill_capability_cache.json")
        _fc[repo] = {"ts": now, "index": idx, "complete": _complete}
        with open("/tmp/gitcode_skill_capability_cache.json", "w",
                  encoding="utf-8") as f:
            json.dump(_fc, f, ensure_ascii=False)
    except Exception:
        pass
    if not _complete:
        logger.warning(f"[skill-capability] 索引不完整: 失败 {failed}/{total}, 短缓存 60s")
    return idx


def _fetch_skill_md_summary(repo, skill_path):
    """拉取单个 skill 的 SKILL.md, 提炼 (summary, description)。

    summary = frontmatter description + 核心命令/工作流章节片段(截断),
    供 LLM 判重时作为该 skill 的能力描述。SKILL.md 缺失/读取失败返回 None。

    2026-08-18 本地化: 优先本地镜像读(git show, 无 429), 失败回退 contents API。
    """
    md = _mirror_read(repo, f"{skill_path}/SKILL.md")
    if md is None:
        try:
            d = gitcode_get(f"/repos/{repo}/contents/{skill_path}/SKILL.md")
            if not isinstance(d, dict) or not d.get("content"):
                return None
            import base64
            md = base64.b64decode(d["content"]).decode("utf-8", errors="replace")
        except Exception as e:
            if "429" in str(e):
                time.sleep(1.0)
                try:
                    d = gitcode_get(f"/repos/{repo}/contents/{skill_path}/SKILL.md")
                    if not isinstance(d, dict) or not d.get("content"):
                        return None
                    import base64
                    md = base64.b64decode(d["content"]).decode("utf-8", errors="replace")
                except Exception:
                    return None
            else:
                return None
    # frontmatter description(支持块标量 `|`/`|-`/`>`/`>-` 多行,
    # 2026-08-17 方案A 实测: 仓库 description 常用 `>-`, 旧 regex 只取到
    # 符号本身导致摘要错误)
    desc = ""
    m = re.match(r"^---\s*\n(.*?)\n---", md, re.S)
    if m:
        _fm_lines = m.group(1).splitlines()
        _di = None
        for _i, _ln in enumerate(_fm_lines):
            if re.match(r"^description\s*:", _ln):
                _di = _i
                break
        if _di is not None:
            _dv = _fm_lines[_di].split(":", 1)[1].strip()
            if _dv in ("|", "|-", ">", ">-"):
                _block = []
                _j = _di + 1
                while _j < len(_fm_lines) and (_fm_lines[_j].startswith("  ")
                                               or _fm_lines[_j].startswith("\t")):
                    _block.append(_fm_lines[_j].strip())
                    _j += 1
                _sep = "\n" if _dv.startswith("|") else " "
                desc = _sep.join(_block)
            else:
                desc = _dv.strip().strip('"\'')
    # 核心命令/工作流片段(取命令相关行, 最多 3 行)
    cmd_lines = []
    for ln in md.splitlines():
        s = ln.strip()
        if re.search(r"hcloud\s+\S+", s) or s.lower().startswith(("hcloud", "cli")):
            cmd_lines.append(s)
        if len(cmd_lines) >= 3:
            break
    parts = [p for p in ([desc] + cmd_lines) if p]
    summary = " | ".join(parts)[:300]
    if not summary:
        # 无 desc 无命令: 用第一个非空标题行兜底
        for ln in md.splitlines():
            s = ln.strip()
            if s and not s.startswith(("#", "-", "*", "`")):
                summary = s[:100]
                break
    return (summary or "", desc or "")


# ==================== LLM 判重 ====================
def find_skill_in_repo(repo, title, body):
    """判断需求对应的 skill 是否已在仓库中开发过(2026-08-04 调整)。

    仅按 **skill 名称全称**匹配(huawei-cloud-xxx): 需求显式写的技能名在
    仓库中已有同名目录才算已开发。**不再按产品目录匹配** —— #119 教训:
    「支持华为云查询eip列表」因 eip 产品目录存在(eip-cost-optimizer)被
    误报"仓库已开发过", 而实际要开发的 huawei-cloud-eip-list 在仓库中
    不存在(仅在 PR #130 中), 产品目录存在 ≠ skill 名称全称已存在。
    返回 (found, detail)。
    """
    if not repo:
        return False, None
    text = f"{title or ''} {body or ''}"
    names = [m.group(0).lower().replace("_", "-")
             for m in re.finditer(r"huawei[-_]?cloud-[a-z0-9-]+", text)]
    if not names:
        return False, None
    products = _get_repo_products(repo)
    if not products:
        return False, None
    # 2026-08-18 本地化: products 已优先本地镜像完整构建(一次 ls-tree,
    # 无限流不完整问题), key 即 skill 目录名, 直接查映射
    for name in names:
        if name in products:
            return True, products[name]
    # 未命中(products 为 API 回退的不完整结果时兜底): 本地镜像探测候选
    # 路径(替代 contents 遍历), 本地不可用再回退 API 探测该路径
    prod_seg = None
    for name in names:
        parts = name.split("-")
        if len(parts) >= 4:
            prod_seg = parts[2]
            break
    candidates = ([products[prod_seg]] if prod_seg and prod_seg in products
                  else list(products.values()))
    for path in candidates:
        for name in names:
            _ok = _mirror_dir_exists(repo, f"{path}/{name}")
            if _ok is True:
                return True, f"{path}/{name}"
            if _ok is None:  # 本地不可用, 回退 API 探测该路径
                try:
                    data = gitcode_get(f"/repos/{repo}/contents/{path}?per_page=100")
                    items = data if isinstance(data, list) else []
                    for it in items:
                        if it.get("type") == "dir" and it["name"].lower().replace("_", "-") in names:
                            return True, f"{path}/{it['name']}"
                except Exception:
                    continue
    return False, None


def find_skill_in_open_prs(repo, title, body, strict_skill_only=False):
    """检查已提交 PR 中是否已包含该 skill(2026-08-04 新增, 名称重复检测)。

    用户要求: 开发类需求的 skill 名称不能与已提交 PR 重复。仓库检查
    (find_skill_in_repo)只覆盖已合入的 skill, 待合入的 PR 需单独查。
    匹配策略:
    1. 技能名精确匹配(huawei-cloud-xxx): 需求技能名出现在 PR 标题/body
       -> 直接命中(标题即证据, 不再拉文件)
    2. 产品名匹配: 需求产品词出现在 PR 标题 -> 拉该 PR 文件列表确认
       是否真的包含 skills/ 下的该产品文件, 防误报
    2026-08-09 #231 修复: strict_skill_only=True 时**仅走技能名精确匹配**
    (第 1 条), 跳过产品词匹配(第 2 条) —— LLM 判重已语义判定"PR 不覆盖
    需求"(如 maas-tokens-usage vs maas 列表查询)后, 产品词匹配会按粗糙
    词面把 issue 误关, 使 LLM 判重形同虚设。
    返回 (found, pr_number, pr_title)。
    """
    if not repo:
        return False, None, None
    text = f"{title or ''} {body or ''}"
    t = text.lower()
    names = [m.group(0).lower().replace("_", "-")
             for m in re.finditer(r"huawei[-_]?cloud-[a-z0-9-]+", text)]
    try:
        prs = gitcode_get(f"/repos/{repo}/pulls?state=all&per_page=30"
                          f"&sort=updated&direction=desc")
        prs = prs if isinstance(prs, list) else []
    except Exception as e:
        logger.warning(f"[pr-skill] 获取 PR 列表失败: {e}")
        return False, None, None
    for pr in prs:
        pr_num = pr.get("number")
        pr_title = pr.get("title") or ""
        pr_body = pr.get("body") or ""
        pr_text = f"{pr_title} {pr_body}"
        # 1) 技能名精确匹配(需求写了 huawei-cloud-xxx)
        if names:
            pr_names = [m.group(0).lower().replace("_", "-")
                        for m in re.finditer(r"huawei[-_]?cloud-[a-z0-9-]+", pr_text)]
            if names[0] in pr_names:
                return True, pr_num, pr_title
        # 2) 产品名匹配 PR 标题 -> 拉文件确认
        # 2026-08-05: 按需求移除 #147 的资源对象词交集限制 —— 产品词命中
        # PR 标题即拉文件, 文件路径含该产品(skills/ 下)即视为重复。
        # 2026-08-07 修复 #175 误关闭: 英文产品词必须是词边界匹配(子串会
        # 误伤前缀包含 —— vpc 匹配 vpcep、ecs 匹配 ecsx 等, vpcep 是 VPC
        # 终端节点, 与 vpc-list skill 绝不重复)。用正则 (?<![a-z0-9])prod(?![a-z0-9])。
        # 2026-08-09 #231: strict_skill_only=True 时跳过产品词匹配
        # (LLM 判重已语义判定不覆盖, 产品词匹配会误关)。
        if strict_skill_only:
            continue
        import re as _re2
        for prod in HUAWEI_CLOUD_PRODUCTS:
            if prod.isascii():
                _pat = _re2.compile(r"(?<![a-z0-9])" + _re2.escape(prod) + r"(?![a-z0-9])", _re2.IGNORECASE)
                hit = bool(_pat.search(t))
                if not hit:
                    continue
                if not _pat.search(pr_title.lower()):
                    continue
            else:
                if prod not in text:
                    continue
                if prod not in pr_title:
                    continue
            try:
                files = gitcode_get(f"/repos/{repo}/pulls/{pr_num}/files?per_page=100")
                files = files if isinstance(files, list) else []
                for f in files:
                    fn = (f.get("filename") or "").lower()
                    # 2026-08-07 #175: 文件名同样词边界匹配(防 vpc-list 文件
                    # 被 vpcep 需求命中; 文件名 huawei-cloud-<prod>- 段与产品
                    # 词整体边界一致)
                    if fn.startswith("skills/"):
                        if prod.isascii():
                            if _pat.search(fn):
                                return True, pr_num, pr_title
                        elif prod in fn:
                            return True, pr_num, pr_title
            except Exception:
                continue
    return False, None, None


def _bug_skill_open_pr(repo, skill_ref):
    """判断该 skill 是否已有 open 状态的 PR 在处理(2026-08-16 用户要求)。

    bug 类 issue 分流新增检查: 识别问题类 issue 后, 判断当前问题的 skill
    是否已有**开启状态**的 PR 在处理 —— 有则当前 bug issue 应跟进该 PR
    而非重复建修复任务(评论提示 + 关闭, 见 handle_issue 2.2.2 ③)。

    判定口径(用户确认方案A): 仅查 `state=open` 的 PR, 逐个拉文件列表,
    变更文件路径属于该 skill 目录(skills/.../{skill_ref}/)即命中。
    查询失败(限流/异常) fail-open 返回 None(不阻断, 由调用方放行)。

    返回 (pr_number, pr_title) 或 None。
    """
    if not repo or not skill_ref:
        return None
    try:
        prs = gitcode_get(f"/repos/{repo}/pulls?state=open&per_page=100")
        prs = prs if isinstance(prs, list) else []
    except Exception as e:
        logger.warning(f"[bug-skill-open-pr] 获取 open PR 列表失败: {e}")
        return None
    skill_key = f"/{skill_ref.lower().replace('_', '-')}/"
    for pr in prs:
        pr_num = pr.get("number")
        try:
            pfiles = gitcode_get(f"/repos/{repo}/pulls/{pr_num}/files?per_page=100")
            pfiles = pfiles if isinstance(pfiles, list) else []
        except Exception:
            continue  # 单个 PR 文件查询失败, 跳过该 PR
        for f in pfiles:
            fn = (f.get("filename") or "").lower().replace("_", "-")
            if skill_key in fn:
                return pr_num, pr.get("title") or ""
    return None


# issue 文本中"关联 PR"引用(2026-08-18 用户要求): 评测问题单/需求描述常带
# "对应 PR: https://.../pull/15" 或 "merge_requests/15" / "PR #15" —— 说明
# 该 issue 的修复已在 PR 中推进, 不应再建开发任务/入库(避免重复开发)。
ISSUE_LINKED_PR_RE = re.compile(
    r"(?:对应|关联|涉及|相关|所属|本次|该)?\s*"
    r"(?:PR|mr|merge\s*request|pull\s*request|pull)\s*[:：#]?\s*#?\s*(\d+)"
    r"|(?:pull|merge_requests)/(\d+)",
    re.IGNORECASE,
)


def _issue_linked_pr(title, body):
    """检测 issue 标题/正文是否已关联 PR(2026-08-18 用户要求)。

    命中 -> handle_issue 建任务前拦截: 不创建开发任务、不入库(评论告知
    已关联 PR, 请到该 PR 跟进)。评测问题单正文的"对应 PR: .../merge_requests/15"
    是典型形态(#43-#47 系列实测: 测试报告带对应 PR, 但 issue 仍建了重复
    开发任务)。

    返回 PR 编号(int) 或 None。空/异常 fail-open 返回 None(不误拦)。
    """
    try:
        text = f"{title or ''} {body or ''}"
        m = ISSUE_LINKED_PR_RE.search(text)
        if not m:
            return None
        return int(m.group(1) or m.group(2))
    except Exception:
        return None

# ==================== 统一判重入口(2026-08-09 K3 重构) ====================
def _dup_comment_msg(label, kind, num, url, state=""):
    """判重评论文案生成(2026-08-18 用户要求): bug/问题类 issue 用**问题语义**
    提示(该问题与 #N 重复/已在原 issue 中跟进处理), 需求/feature 类保持
    **需求语义**(该需求与 #N 重复/避免重复开发)。此前统一用需求文案,
    评测问题单(#47 实测)被判重关闭时收到"该需求与 #45 重复...避免重复开发"
    与问题类场景不符。

    kind: "dup"(重复关闭) / "remind"(与已关闭相似, 只提示不关闭)
    """
    if label == "bug":
        if kind == "remind":
            return (f"ℹ️ 该问题与已关闭的 [#{num}]({url}) 相似"
                    f"(状态: {state}), 如问题仍然存在, 请补充说明现象/复现步骤; "
                    f"本 issue 保持开放待确认。")
        return (f"🔁 该问题与 [#{num}]({url}) 重复, "
                f"已在原 issue 中跟进处理, 本 issue 将关闭。")
    if kind == "remind":
        return (f"ℹ️ 该需求与已关闭的 [#{num}]({url}) 相似"
                f"(状态: {state}), 如确需重新开发/迭代, "
                f"请补充说明与原需求的差异; 本 issue 保持开放待确认。")
    return (f"🔁 该需求与 [#{num}]({url}) 重复, "
            f"已在原 issue 中跟进, 为避免重复开发本 issue 将被关闭。")


def _dup_task_status_suffix(repo, dup_num):
    """查被判重的 issue 是否有关联开发任务。 #83 用户要求判重回复中
    不体现内部任务编号(SKI-xx)与进展链接, 统一返回空字符串。"""
    return ""


def _check_llm_api_connectivity():
    """快速测试 LLM API 连通性。返回 True(可用)/False(不可用, 回退产品词匹配)。
    
    2026-08-18 #70 新增: 区分"LLM 执行成功(语义判无重复)"与
    "LLM 不可用(需回退产品词匹配)"。"""
    try:
        http_json("POST", f"{LLM_API_BASE}/chat/completions",
                  body={"model": LLM_MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1},
                  headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                  timeout=LLM_TIMEOUT)
        return True
    except Exception as e:
        logger.warning(f"[dup-check-llm] LLM API 连通性检查失败(将回退产品词匹配): {e}")
        return False


def _dup_check_issue(repo, title, body, number, label, exclude_id=None):
    """统一判重入口: 收敛 5 链判重, 消除 handle_issue / handle_issue_update
    两处调用漂移(#231/#241/#248 三次修复均源于调用点语义不一致)。

    只做**判定**, 不做评论/关闭/入库 —— 动作由调用方保留各自的语义
    (handle_issue 判重关闭不入库; handle_issue_update 额外 update
    status=duplicate)。

    返回 (dict|None, llm_ok: bool):
      - dict: 命中判重, 格式同旧返回
        {"kind": ..., "dup": issue_dict|None, "num": ..., "url": ...,
         "reason": ...}
        kind 取值:
          - "pr_covered":   LLM 判 PR 已覆盖功能点(feature 类)
          - "skill_covered":LLM 判仓库 skill 已覆盖功能点(feature 类)
          - "dup_open":     open 候选重复
          - "dup_closed":   closed_dup(因重复关闭)候选重复
          - "dup_remind":   closed_other 候选相似(只提示不关闭)
      - None: 未命中, 此时 llm_ok 表示 LLM API 是否实际可用
      - llm_ok: 仅当返回 None 时有效; True=LLM 执行成功(语义判无重复),
                 False=LLM 未执行/失败(需回退产品词匹配)
        """
    # 优化类不走判重(2.0 规则 3) —— 但仅对**非 bug 类**生效(2026-08-17 修复):
    # bug 报告标题/正文常含"修改/修复/异常"等词(#43/#44/#45 评测问题单标题
    # "[huawei-cloud-dns-managemengt] 问题修改"命中"修改"被误判优化类跳过判重,
    # 重复 bug 报告未被拦截各自建任务), 判重优先于优化语义。
    if is_optimization_request(title, body) and label != "bug":
        return None, False
    # ---- LLM API 连通性检查 ----
    _llm_api_ok = False
    _llm_can_try = label != "bug" and ENABLE_DUP_CHECK and ENABLE_LLM_DUP_CHECK and LLM_API_KEY
    if _llm_can_try:
        _llm_api_ok = _check_llm_api_connectivity()
    # ---- 链 1/2: LLM PR/skills 覆盖(仅 feature 类; bug 跳过 —— PR 合入恰
    # 说明 skill 存在且有 bug, 应修复而非判重, #241) ----
    if _llm_api_ok:
        pr_covered, pr_num, pr_reason = _llm_pr_covers_issue(repo, title, body)
        if pr_covered and pr_num:
            return {"kind": "pr_covered", "dup": None, "num": pr_num,
                    "url": f"https://gitcode.com/{repo}/pull/{pr_num}", "reason": pr_reason}, True
        sk_covered, sk_name, sk_reason = _llm_skills_covers_issue(repo, title, body)
        if sk_covered and sk_name:
            return {"kind": "skill_covered", "dup": None, "num": None,
                    "url": None, "reason": sk_reason, "skill_name": sk_name}, True
    if not ENABLE_DUP_CHECK:
        return None, False
    # ---- 链 3: 实时 issue 判重(bug -> LLM 同一问题语义, #248; 退化规则) ----
    if label == "bug" and ENABLE_LLM_DUP_CHECK and LLM_API_KEY:
        dup, dup_kind, dup_reason = _llm_find_bug_duplicate(
            repo, title, body, exclude_number=str(number) if number else None)
    else:
        dup, dup_kind, dup_reason = None, None, None
        d, k = find_duplicate_issue(
            repo, title, body, exclude_number=str(number) if number else None,
            skip_skill_match=(label == "bug"))
        dup, dup_kind = d, k
    # ---- 链 3.5: feature 类 Jaccard + LLM 判重(非 bug) ----
    if not dup and label != "bug":
        _fd_dup, _fd_kind, _fd_reason = _llm_find_feature_duplicate(
            repo, title, body, number)
        if _fd_dup and _fd_kind:
            dup, dup_kind, dup_reason = _fd_dup, _fd_kind, _fd_reason or ""
    if dup:
        dup_num = dup.get("number") or dup.get("iid")
        dup_url = dup.get("html_url") or f"https://gitcode.com/{repo}/issues/{dup_num}"
        if dup_kind == "closed_other":
            return {"kind": "dup_remind", "dup": dup, "num": dup_num, "url": dup_url,
                    "reason": dup_reason or "closed_other"}, True
        if dup_kind == "feature_related":
            return {"kind": "feature_related", "dup": dup, "num": dup_num, "url": dup_url,
                    "reason": dup_reason or "LLM 判部分重复"}, True
        return {"kind": "feature_dup" if dup_kind == "feature_dup" else
                ("dup_open" if dup_kind == "open" else "dup_closed"),
                "dup": dup, "num": dup_num, "url": dup_url, "reason": dup_reason or ""}, True
    return None, _llm_api_ok


# ==================== Multica CLI 封装 ====================
def _multica_cli(args, timeout=60):
    """调用 multica CLI, 返回 (exit_code, stdout, stderr)"""
    env = os.environ.copy()
    env["MULTICA_TOKEN"] = MULTICA_TOKEN
    env["MULTICA_WORKSPACE_ID"] = MULTICA_WORKSPACE_ID
    proc = subprocess.run(
        ["multica"] + args, capture_output=True, text=True, timeout=timeout, env=env
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def multica_issue_create(title, description, assignee_id, force_new=False):
    """创建 Multica issue 并指定分配对象(小队或智能体), 返回 (issue_id, identifier)

    2026-08-08 #221 崩溃防护: 撞 "Active duplicate issue exists: SKI-xxx"
    校验时**不抛异常崩溃**, 而是用 _find_existing_multica_issue 复用已有
    同标题任务(返回其 id/identifier), 由调用方继续后续流程 —— 避免判重
    漏判时 webhook 处理整体崩溃导致 issue 无响应(#221 实测: 撞 SKI-172
    duplicate 后脚本 RuntimeError, issue 保持 open 无人处理)。

    2026-08-11 force_new 参数: 评论触发"转机器人修复"(is_bot_fix_request)
    传 force_new=True —— **不同 GitCode issue 即使标题相同也必须独立建任务**,
    否则 #102 复用 #101 的同标题任务(in_review 等已关闭的 PR #39 收尾),
    机器人不会为 #102 重新开发, 流程永久卡死(实测)。force_new 时给 CLI 加
    --allow-duplicate(Multica 侧 duplicate 保护也需显式放行, 否则仍抛
    "Active duplicate issue exists", 实测)。
    """
    _cmd = [
        "issue", "create",
        "--title", title,
        "--description", description,
        "--assignee-id", assignee_id,
        "--output", "json",
    ]
    if force_new:
        _cmd.append("--allow-duplicate")
    code, out, err = _multica_cli(_cmd)
    if code != 0:
        if not force_new and ("duplicate issue exists" in (out or "") or "duplicate issue exists" in (err or "")):
            logger.warning(
                f"[multica-dup] 创建撞 active duplicate, 复用已有同标题任务: "
                f"{out[:200]} {err[:200]}")
            existing_id, existing_identifier = _find_existing_multica_issue(title)
            if existing_id:
                return existing_id, existing_identifier or f"SKI-{existing_id[:8]}"
            logger.warning("[multica-dup] 复用失败(查不到同标题任务), 抛出原始错误")
        raise RuntimeError(f"multica issue create 失败: {out[:300]} {err[:300]}")
    data = json.loads(out)
    return data["id"], data.get("identifier") or f"SKI-{data.get('number')}"


def _create_dev_task_or_notify(repo, number, title, body):
    """创建开发任务, 失败时告知 issue 创建者(2026-08-18 用户要求)。

    此前 multica_issue_create 抛异常 -> handle_issue 整体崩溃 -> issue 无
    任何响应(#45 实测: 撞 409 后页面静默, 创建者不知情)。本函数捕获异常,
    评论告知创建者。**文案不涉及任务系统名称及内部细节**(用户明确要求:
    "回复内容中不要涉及 multica 以及其具体内容")——只说明任务创建失败、
    重试方式, 不出现任务系统名/任务编号/接口信息。

    返回 (m_issue_id, m_identifier, err_msg):
    - 成功: (id, identifier, None)
    - 失败: (None, None, 错误摘要) 且已发告知评论(幂等 marker 防重复刷屏)
    """
    try:
        m_id, m_ident = multica_issue_create(title, body, SQUAD_DEV)
        return m_id, m_ident, None
    except Exception as e:
        logger.warning(f"[task-create] issue #{number} 创建开发任务失败: {e}")
        if repo and number:
            msg = (
                "⚠️ 该 issue 的开发任务创建失败, 请稍后重试"
                "(可编辑保存该 issue 触发重新处理); 若持续失败请联系管理员。"
            )
            if not _issue_has_bot_comment(repo, number, MARKERS["task_create_failed"]):
                safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
        return None, None, str(e)[:200]


def multica_issue_comment(issue_id, content):
    """向 Multica issue 追加评论, 返回评论 id"""
    code, out, err = _multica_cli(
        ["issue", "comment", "add", issue_id, "--content", content, "--output", "json"]
    )
    if code != 0:
        raise RuntimeError(f"multica issue comment 失败: {out[:300]} {err[:300]}")
    return json.loads(out)["id"]


def multica_issue_comments(issue_id):
    """获取 Multica issue 的评论列表"""
    code, out, err = _multica_cli(["issue", "comment", "list", issue_id, "--output", "json"])
    if code != 0:
        raise RuntimeError(f"multica issue comment list 失败: {out[:300]} {err[:300]}")
    return json.loads(out)


def multica_issue_assign(issue_id, assignee_id):
    """转移 Multica issue 的分配对象(小队或智能体)"""
    code, out, err = _multica_cli(["issue", "assign", issue_id, "--to-id", assignee_id, "--output", "json"])
    if code != 0:
        raise RuntimeError(f"multica issue assign 失败: {out[:300]} {err[:300]}")
    return True


def multica_issue_get(issue_id):
    """查询 Multica issue 详情, 返回 (是否存在, 数据 dict)"""
    try:
        code, out, err = _multica_cli(["issue", "get", issue_id, "--output", "json"])
        if code != 0:
            return False, {}
        data = json.loads(out)
        return bool(data.get("id")), data
    except Exception:
        return False, {}


def _find_existing_multica_issue(title):
    """查 Multica 中同标题的已有 issue, 返回 (issue_id, identifier) 或 (None, None)。

    最后防线: 逻辑4 兜底新建任务前先查重 —— PR 重开/重复事件场景下 Multica
    已有同标题活跃 issue 时, 直接复用不新建, 避免撞 "Active duplicate issue
    exists: SKI-xxx" 校验崩溃(2026-08-05 PR #143 实测: #142 关闭后重开 #143,
    DB 记录已关联 SKI-152, 但若 skill 名称提取不到/记录丢失仍会走到新建)。
    """
    try:
        code, out, err = _multica_cli(["issue", "list", "--output", "json"])
        if code != 0:
            logger.warning(f"[multica-dup] issue list 失败: {out[:200]} {err[:200]}")
            return None, None
        data = json.loads(out)
        for i in (data.get("issues") or []):
            if str(i.get("title") or "") == title:
                return i.get("id"), i.get("identifier") or f"SKI-{i.get('number')}"
    except Exception as e:
        logger.warning(f"[multica-dup] 查询同标题 issue 失败: {e}")
    return None, None


# ==================== 数据库工具 ====================
def db_connect():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME,
        charset="utf8mb4", autocommit=True, connect_timeout=15,
    )


def db_find_requirement(where, params):
    """按条件查开发需求表, 返回字典行或 None; params 为参数元组"""
    sql = f"SELECT * FROM `{REQUIREMENT_TABLE}` WHERE {where} LIMIT 1"
    with db_connect() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params if isinstance(params, (list, tuple)) else (params,))
            return cur.fetchone()


def db_find_requirement_by_issue_id(issue_id):
    # issue id 为 GitCode 全局唯一, 无需仓库校验
    return db_find_requirement("gitcode_issue_id = %s", (issue_id,))


def db_find_requirement_by_pr_id(pr_id, repo=None):
    """按 PR 编号匹配; 传 repo 时校验仓库归属(不同仓库 PR 编号会冲突)"""
    if repo:
        return db_find_requirement(
            "gitcode_pr_id = %s AND gitcode_issue_url LIKE %s",
            (str(pr_id), f"%/{repo}/%"),
        )
    return db_find_requirement("gitcode_pr_id = %s", (str(pr_id),))


def db_find_requirement_by_issue_number(number, repo=None):
    """按 issue 编号匹配; 传 repo 时校验仓库归属(不同仓库 issue 编号会重复)"""
    if repo:
        return db_find_requirement(
            "gitcode_issue_number = %s AND gitcode_issue_url LIKE %s",
            (str(number), f"%/{repo}/%"),
        )
    return db_find_requirement("gitcode_issue_number = %s", (str(number),))


def db_find_requirement_by_multica_issue(multica_issue_id):
    return db_find_requirement("multica_issue_id = %s", (multica_issue_id,))


def db_insert_requirement(data):
    """插入开发需求, 返回自增 id; gitcode_issue_id 冲突时返回已有记录 id"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            sql = (
                f"INSERT INTO `{REQUIREMENT_TABLE}` "
                "(gitcode_issue_id, gitcode_issue_number, gitcode_issue_url, title, body, "
                " state, author, author_name, created_at_gitcode, updated_at_gitcode, "
                " multica_issue_id, multica_issue_url, multica_issue_number, status, "
                " requirement_type, gitcode_pr_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                # 2026-08-09 隐患修复: ON DUPLICATE 不含 gitcode_pr_id ——
                # insert 路径(issue 创建/need_info)的 gitcode_pr_id 恒为 None,
                # 撞唯一键会**清空**已有记录的 PR 关联(issue 已关联 PR 后被
                # need_info 重插入), 导致合入时 db_find_requirement_by_pr_id
                # 找不到。PR 关联更新由 handle_pr 的 db_update_requirement
                # 显式完成(L5254/5280/5314), 不需 ON DUPLICATE 覆盖。
                "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), "
                " title = VALUES(title), body = VALUES(body), state = VALUES(state), "
                " updated_at_gitcode = VALUES(updated_at_gitcode), status = VALUES(status)"
            )
            cur.execute(sql, tuple(data[k] for k in (
                "gitcode_issue_id", "gitcode_issue_number", "gitcode_issue_url", "title", "body",
                "state", "author", "author_name", "created_at_gitcode", "updated_at_gitcode",
                "multica_issue_id", "multica_issue_url", "multica_issue_number", "status",
                "requirement_type", "gitcode_pr_id",
            )))
            return cur.lastrowid


def db_insert_flow(requirement_id, gitcode_issue_id, multica_issue_id, node_updates):
    """插入开发流程记录; node_updates 为 {列名: (status, url)} 字典。

    幂等: requirement_id 已存在 flow 行时(uk_requirement_id 唯一键)改为更新
    节点状态, 避免重复事件(issue update 连推/重试/外部系统改 status 触发
    handle_issue 重入)直接撞唯一键崩溃(2026-08-04 issue #102 实测 1062)。
    """
    cols = ["requirement_id", "gitcode_issue_id", "multica_issue_id"]
    vals = [requirement_id, gitcode_issue_id, multica_issue_id]
    updates = [
        "`gitcode_issue_id` = VALUES(`gitcode_issue_id`)",
        "`multica_issue_id` = VALUES(`multica_issue_id`)",
    ]
    for col, (status, url) in node_updates.items():
        cols += [f"{col}_status", f"{col}_url", f"{col}_time"]
        vals += [status, url, datetime.datetime.now()]
        updates += [
            f"`{col}_status` = VALUES(`{col}_status`)",
            f"`{col}_url` = VALUES(`{col}_url`)",
            f"`{col}_time` = VALUES(`{col}_time`)",
        ]
    placeholders = ",".join(["%s"] * len(cols))
    sql = (
        f"INSERT INTO `{FLOW_TABLE}` ({','.join('`'+c+'`' for c in cols)}) "
        f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {', '.join(updates)}"
    )
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(vals))
            return cur.lastrowid


def db_update_requirement(req_id, fields):
    """按 id 更新需求表, fields 为 {列名: 值}"""
    if not fields:
        return
    sets = ",".join(f"`{k}` = %s" for k in fields)
    sql = f"UPDATE `{REQUIREMENT_TABLE}` SET {sets} WHERE id = %s"
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(list(fields.values()) + [req_id]))


def db_update_flow(requirement_id, col, status, url=None):
    """更新流程表某节点状态"""
    now = datetime.datetime.now()
    with db_connect() as conn:
        with conn.cursor() as cur:
            if url:
                cur.execute(
                    f"UPDATE `{FLOW_TABLE}` SET `{col}_status`=%s, `{col}_url`=%s, `{col}_time`=%s WHERE requirement_id=%s",
                    (status, url, now, requirement_id),
                )
            else:
                cur.execute(
                    f"UPDATE `{FLOW_TABLE}` SET `{col}_status`=%s, `{col}_time`=%s WHERE requirement_id=%s",
                    (status, now, requirement_id),
                )


# ==================== 版本表 t_skill_pr_version_history(2026-08-04 需求) ====================
# 主分支 PR 合入 -> 记录 skill 版本变化(add/update), 供版本追溯。
_VERSION_TITLE_RE = re.compile(r"\bv(\d+(?:\.\d+){1,3})\b", re.IGNORECASE)


def _extract_version_from_title(title):
    """从 PR 标题提取版本号: 标题规范 `{add|update} skill: path/name [v1.2.3]` -> '1.2.3'"""
    m = _VERSION_TITLE_RE.search(title or "")
    return m.group(1) if m else None


def _bump_patch_version(v):
    """版本号末位 +1: '1.0.0' -> '1.0.1'; 解析失败返回 None"""
    m = re.match(r"^(\d+\.\d+\.)(\d+)$", (v or "").strip())
    if not m:
        return None
    return f"{m.group(1)}{int(m.group(2)) + 1}"


def _skill_dir_from_pr_files(repo, pr_number, fallback_path=""):
    """从 PR 变更文件推导 skill 根目录(对齐 devops-bot _find_skill_dir_from_pr)。

    取所有 skills/ 开头文件的最长公共目录, 再向上找含 SKILL.md 的目录作为
    skill 根(避免公共目录落在 scripts/ 等子目录, 如 .../ges-graph/scripts);
    失败时回退标题路径(补 skills/ 前缀)。返回形如 'skills/network/ops/xxx'。
    """
    common = ""
    try:
        data = gitcode_get(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page=1")
        if isinstance(data, list):
            files = [f.get("filename") for f in data
                     if f.get("filename") and str(f.get("filename")).startswith("skills/")]
            if files:
                dirs = [os.path.dirname(p) for p in files if p]
                common = os.path.commonpath(dirs) if dirs else ""
    except Exception as e:
        logger.warning(f"[版本表] 从 PR 变更文件推导 skill 目录失败: {e}")
    # 向上找含 SKILL.md 的目录(最多 3 层), 失败则用公共目录本身
    # 2026-08-18 本地化: 优先本地镜像探测(HEAD=目标分支), 失败回退 contents API
    if common.startswith("skills/"):
        cur = common
        for _ in range(3):
            _ok = _mirror_read(repo, f"{cur}/SKILL.md")
            if _ok is not None:
                return cur
            try:
                d = gitcode_get(f"/repos/{repo}/contents/{cur}/SKILL.md?ref={MERGE_TARGET_BRANCH}")
                if isinstance(d, dict) and d.get("content"):
                    return cur
            except Exception:
                pass
            parent = os.path.dirname(cur)
            if not parent.startswith("skills/") or parent == cur:
                break
            cur = parent
        # 公共目录本身含 SKILL.md 或向上找不到 -> 去掉常见子目录后缀
        for suffix in ("/scripts", "/src", "/tests"):
            if common.endswith(suffix):
                return common[: -len(suffix)]
        return common
    if fallback_path:
        p = str(fallback_path).strip("/")
        if not p.startswith("skills/"):
            p = "skills/" + p
        return p
    return ""


def _fetch_skill_frontmatter_version(repo, skill_dir, branch):
    """读取技能 SKILL.md frontmatter 的 version 字段(可能不存在)

    2026-08-18 本地化: 优先本地镜像读(HEAD=目标分支, 与 MERGE_TARGET_BRANCH
    一致), 失败回退 contents API(?ref=branch)。"""
    if not (repo and skill_dir):
        return None
    _content = _mirror_read(repo, f"{skill_dir}/SKILL.md")
    if _content is None:
        try:
            d = gitcode_get(f"/repos/{repo}/contents/{skill_dir}/SKILL.md?ref={branch}")
            _content = (d or {}).get("content") if isinstance(d, dict) else None
            if _content:
                try:
                    import base64
                    _content = base64.b64decode(_content).decode("utf-8")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[版本表] 读取 SKILL.md frontmatter 版本失败: {e}")
            return None
    try:
        in_fm = False
        for ln in _content.splitlines():
            s = ln.strip()
            if s == "---":
                if in_fm:
                    break
                in_fm = True
                continue
            if in_fm:
                m = re.match(r"^version[:：]\s*([\w.\-]+)", s, re.IGNORECASE)
                if m:
                    return m.group(1)
    except Exception as e:
        logger.warning(f"[版本表] 读取 SKILL.md frontmatter 版本失败: {e}")
    return None


def db_last_version(skill_name, repo):
    """查版本表该技能最近一条 new_version(按 id 倒序取最新)"""
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT new_version FROM `{VERSION_TABLE}` "
                    "WHERE skill_name=%s AND repo=%s ORDER BY id DESC LIMIT 1",
                    (skill_name, repo),
                )
                r = cur.fetchone()
                return r[0] if r else None
    except Exception as e:
        logger.warning(f"[版本表] 查最近版本失败: {e}")
        return None


def db_write_version_history(skill_name, repo, old_version, new_version,
                             change_type, pr_number, merged_at):
    """写版本表(幂等: 同 skill_name+repo+pr_number 已存在则跳过)。

    返回 True=新写入 / False=已存在跳过。
    """
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM `{VERSION_TABLE}` "
                    "WHERE skill_name=%s AND repo=%s AND pr_number=%s LIMIT 1",
                    (skill_name, repo, int(pr_number)),
                )
                if cur.fetchone():
                    return False
                cur.execute(
                    f"INSERT INTO `{VERSION_TABLE}` "
                    "(skill_name, repo, old_version, new_version, change_type, pr_number, merged_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (skill_name, repo, old_version or "", new_version,
                     change_type, int(pr_number), merged_at),
                )
                return True
    except Exception as e:
        logger.warning(f"[版本表] 写入失败: {e}")
        return False


def record_version_history(payload, pr, repo, pr_number, req):
    """主分支 PR 合入 -> 写 t_skill_pr_version_history 版本表(逻辑8 补充)。

    - skill_name: PR 变更文件推导的 skill 根目录(形如 skills/network/ops/xxx),
      回退标题路径/需求 skill_name
    - change_type: 由 PR 标题动作决定(add skill:/update skill:), 非表内有无旧版本
    - new_version: 标题 vX.Y.Z > SKILL.md frontmatter version > 兜底
      (add=1.0.0, update=旧版本末位+1; 旧版本缺失时假定 1.0.0 -> 1.0.1)
    - old_version: 该技能上一条 new_version; add 恒为空, update 无旧记录时取 1.0.0
    - merged_at: payload > API > now
    """
    if not (repo and pr_number):
        return False
    title = pr.get("title") or ""
    # 0. 动作: 标题 add/update skill: 决定 change_type
    m_action = re.match(r"^\s*(add|update)\s+skill", title or "", re.IGNORECASE)
    action = (m_action.group(1).lower() if m_action else "") or \
        ("add" if not (req or {}).get("skill_name") else "update")
    # 1. skill 目录路径
    m = re.search(r"skill[:：]\s*([\w/.-]+)", title, re.IGNORECASE)
    title_path = m.group(1).strip("/") if m else ""
    skill_dir = _skill_dir_from_pr_files(repo, pr_number, title_path)
    if not skill_dir:
        skill_dir = str((req or {}).get("skill_name") or "").strip()
    if not skill_dir:
        logger.warning("[版本表] 无法确定 skill 目录, 跳过版本记录")
        return False
    # 2. 旧版本: 表内该技能最近一条 new_version(幂等键需与已入库的目录一致)
    old_version = db_last_version(skill_dir, repo)
    # 3. 新版本: 标题 > frontmatter > 兜底
    new_version = _extract_version_from_title(title)
    branch = pr.get("target_branch") or MERGE_TARGET_BRANCH
    if not new_version:
        new_version = _fetch_skill_frontmatter_version(repo, skill_dir, branch)
    if action == "update" and not old_version:
        # 表内无旧记录但 PR 是 update: 假定首版 1.0.0, 本次 1.0.1(对齐版本表示例)
        old_version = "1.0.0"
    if not new_version:
        new_version = _bump_patch_version(old_version) if old_version else "1.0.0"
    if not new_version:
        new_version = "1.0.0"
    change_type = "add" if action == "add" else "update"
    # 4. merged_at: payload > API(webhook payload 常无 merged_at) > now
    merged_dt = parse_datetime(pr.get("merged_at") or payload.get("merged_at"))
    if not merged_dt:
        try:
            pr_detail = gitcode_get(f"/repos/{repo}/pulls/{pr_number}")
            if isinstance(pr_detail, dict):
                merged_dt = parse_datetime(pr_detail.get("merged_at"))
        except Exception as e:
            logger.warning(f"[版本表] 拉取 PR 合并时间失败: {e}")
    merged_dt = merged_dt or datetime.datetime.now()
    ok = db_write_version_history(skill_dir, repo, old_version or "",
                                  new_version, change_type, pr_number, merged_dt)
    logger.info(
        f"[版本表] {'写入' if ok else '已存在跳过'}: skill={skill_dir} "
        f"{old_version or '∅'}->{new_version} ({change_type}) PR #{pr_number}")
    return ok


def db_comment_forwarded(gitcode_comment_id, multica_issue_id):
    """检查该 GitCode 评论是否已转发到 Multica issue(去重)"""
    marker = f"GitCode 评论 ID: {gitcode_comment_id}"
    try:
        for c in multica_issue_comments(multica_issue_id):
            if marker in (c.get("content") or ""):
                return True
    except Exception:
        pass
    return False


# ==================== 解析工具 ====================
def parse_datetime(s):
    """GitCode 时间字符串转 MySQL datetime 字符串, 失败返回 None"""
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def get_repo_full_name(payload):
    """从 payload 提取 owner/repo, 如 developer-skill/huaweicloud-skills-dev"""
    proj = payload.get("project") or payload.get("repository") or {}
    for key in ("path_with_namespace", "full_name"):
        if proj.get(key):
            return proj[key]
    for key in ("web_url", "homepage"):
        url = proj.get(key) or ""
        m = re.search(r"gitcode\.com/([^/]+/[^/]+)/?$", url)
        if m:
            return m.group(1)
    return None


# ==================== 忽略标签检查(devops-bot 迁移) ====================
def _flatten_labels(labels) -> list:
    """把标签(字符串/数组/对象数组)展平为名称列表"""
    if isinstance(labels, str):
        return [l.strip() for l in labels.split(",") if l.strip()]
    if isinstance(labels, list):
        out = []
        for l in labels:
            if isinstance(l, dict):
                name = l.get("name", "")
                if name:
                    out.append(str(name).strip())
            elif isinstance(l, str) and l.strip():
                out.append(l.strip())
        return out
    return []


def _get_issue_labels_from_payload(payload):
    """从 payload 提取 issue 当前标签集合(GitHub/GitLab 风格兼容)。

    - GitHub 风格: changes.labels.to(变更后完整串) / issue.labels
    - GitLab 风格: object_attributes.labels / 顶层 labels
    返回 None 表示 payload 中读不到标签信息(需 API 兜底)。
    """
    changes = payload.get("changes")
    if isinstance(changes, dict):
        lc = changes.get("labels")
        if isinstance(lc, dict) and lc.get("to"):
            return [l.strip() for l in str(lc["to"]).split(",") if l.strip()]
    issue = payload.get("issue")
    if isinstance(issue, dict):
        labels = issue.get("labels")
        if labels:
            return _flatten_labels(labels)
    oa = payload.get("object_attributes")
    if isinstance(oa, dict):
        labels = oa.get("labels")
        if labels:
            return _flatten_labels(labels)
    labels = payload.get("labels")
    if labels:
        return _flatten_labels(labels)
    return None


def _extract_issue_number(payload):
    """从 payload 提取 issue 编号(iid/number), 用于忽略标签的 API 兜底查询"""
    for key in ("issue", "pull_request", "merge_request"):
        obj = payload.get(key)
        if isinstance(obj, dict):
            for k in ("number", "iid"):
                if obj.get(k) is not None:
                    return obj[k]
    oa = payload.get("object_attributes")
    if isinstance(oa, dict):
        for k in ("noteable_iid", "iid"):
            if oa.get(k) is not None:
                return oa[k]
        if oa.get("id") is not None:
            return oa["id"]
    return None


def _get_issue_title(payload):
    """从 payload 提取 issue 标题(GitHub/GitLab 风格兼容)"""
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("title"):
        return issue["title"]
    oa = payload.get("object_attributes")
    if isinstance(oa, dict) and oa.get("title"):
        return oa["title"]
    pr = payload.get("pull_request") or payload.get("merge_request")
    if isinstance(pr, dict) and pr.get("title"):
        return pr["title"]
    return ""


def _get_issue_body(payload):
    """从 payload 提取 issue 正文(GitHub 风格 body / GitLab 风格 description)"""
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("body"):
        return issue["body"]
    oa = payload.get("object_attributes")
    if isinstance(oa, dict):
        if oa.get("description"):
            return oa["description"]
        if oa.get("body"):
            return oa["body"]
    return ""


def _api_issue_labels(repo, number):
    """API 兜底查询 issue 当前标签集合(修复 2026-08-04 #102 事故)。

    背景: GET /repos/{repo}/issues/{number}/labels 端点实测返回 feign 错误 JSON
    (200 状态码但 body 是 {\"error_code\": 200, \"error_message\": \"Error while
    extracting response...\"}), 不是标签列表 —— 旧实现对 dict 响应静默取空,
    导致 ignore 标签检查在 \"payload 无标签\" 场景形同虚设(issue #102 评测报告
    被当成普通 issue 处理, bug/need-info 标签覆盖了评测参考)。

    正确端点: GET /repos/{repo}/issues/{number}(详情, 含 labels 字段, 实测可用)。
    策略: 先查详情端点; 响应异常/结构无效时再试 /labels 端点;
    两处都失败 -> 返回 None(由调用方决定 fail-open)。
    """
    if not (repo and number):
        return None
    # 端点1: issue 详情(含 labels 字段, 实测返回 ['bug', 'need-info'] 风格)
    for path in (f"/repos/{repo}/issues/{number}",
                 f"/repos/{repo}/issues/{number}/labels"):
        try:
            data = gitcode_get(path)
            if isinstance(data, list):
                # /labels 端点正常返回时的列表形式
                return _flatten_labels(data)
            if isinstance(data, dict):
                # 详情端点: labels 字段
                labels = data.get("labels")
                if labels is not None:
                    return _flatten_labels(labels)
                # feign 错误 JSON(error_code/error_message) -> 端点不可用, 试下一个
                if data.get("error_code") or data.get("error_message"):
                    continue
                # 无 labels 字段的普通 dict -> 视为查不到
                return []
        except Exception:
            continue
    return None


# 仓库 collaborator 缓存(2026-08-12): PR 评审人必须是有仓库操作权限的
# collaborator, 每事件查 collaborators API 会 429(50次/分钟), 文件持久化缓存
# TTL 600s(对齐 _get_repo_products 的防雪崩模式)。
_COLLABORATORS_CACHE_FILE = "/tmp/gitcode_repo_collaborators_cache.json"


def _repo_collaborators(repo):
    """返回仓库 collaborator login 小写集合; 查询失败回退旧缓存/空集。"""
    if not repo:
        return set()
    _c = {}
    try:
        if os.path.exists(_COLLABORATORS_CACHE_FILE):
            with open(_COLLABORATORS_CACHE_FILE, encoding="utf-8") as _f:
                _c = json.load(_f) or {}
    except Exception:
        _c = {}
    _entry = _c.get(repo) or {}
    if _entry.get("ts") and time.time() - _entry["ts"] < 600:
        return set(_entry.get("logins") or [])
    logins = set()
    try:
        d = gitcode_get(f"/repos/{repo}/collaborators?per_page=100")
        if isinstance(d, list):
            for c in d:
                u = c.get("user") or c
                if u.get("login"):
                    logins.add(str(u["login"]).lower())
    except Exception as e:
        logger.warning(f"[collaborators] 查询 {repo} 失败: {e}, 回退旧缓存")
        return set(_entry.get("logins") or [])
    _c[repo] = {"ts": time.time(), "logins": sorted(logins)}
    try:
        with open(_COLLABORATORS_CACHE_FILE, "w", encoding="utf-8") as _f:
            json.dump(_c, _f, ensure_ascii=False)
    except Exception:
        pass
    return logins


def _is_evaluation_report(title, body):
    """评测报告特征识别: 命中直接忽略(评测报告不是开发需求)。

    背景(2026-08-04 #102 事故): 评测系统创建 \"<skill名> 评测报告\" issue 后
    1 秒才打上「评测参考」标签, open 事件先到且 payload 无标签, 即使 API 兜底
    修好, 时序上也可能查不到。标题/正文特征比标签更早可用, 作为第二道防线。
    """
    title = title or ""
    body = body or ""
    if "评测报告" in title:
        return True
    if "评测" in title and re.search(r"评测(完成|日期|结果|通过|不通过)", body):
        return True
    return False


def issue_has_ignore_label(payload) -> bool:
    """判断 issue 是否携带全局忽略标签; payload 无标签信息时调 API 兜底(fail-open)。

    对齐 devops-bot event_processor._issue_has_skip_label:
    命中忽略标签的 issue 事件整体忽略, 不处理/不通知/不分发。

    2026-08-04 修复(#102 事故):
    1. API 兜底端点从 /issues/{n}/labels(feign 错误 JSON)改为详情端点优先
    2. 兜底查不到标签时 sleep 重查一次(覆盖 \"创建后 1 秒内打标签\" 的时序竞态)
    3. 评测报告特征(标题/正文)直接忽略, 不依赖标签时序
    """
    if not IGNORE_ISSUE_LABELS:
        return False
    labels = _get_issue_labels_from_payload(payload)
    if labels is None:
        # payload 无标签信息 -> API 兜底确认当前标签集合
        repo = get_repo_full_name(payload)
        number = _extract_issue_number(payload)
        if not (repo and number):
            return False
        try:
            labels = _api_issue_labels(repo, number)
            if labels is None:
                # API 兜底失败 -> fail-open: 照常处理, 避免误伤真实 issue
                return False
            if not labels:
                # 查不到标签: 可能是创建后 1 秒内标签尚未打上, 补查一次
                delay = float(os.environ.get("IGNORE_LABEL_RECHECK_DELAY", "2.5"))
                if delay > 0:
                    logger.info(
                        f"[ignore-label] payload 无标签且 API 首查为空, "
                        f"{delay}s 后补查一次(repo={repo} issue=#{number})"
                    )
                    time.sleep(delay)
                    labels = _api_issue_labels(repo, number) or []
        except Exception:
            # fail-open: API 失败时照常处理, 避免误伤真实 issue
            return False
    # 第二道防线: 评测报告特征(不依赖标签时序)
    title = _get_issue_title(payload)
    body = _get_issue_body(payload)
    if not (set(labels) & set(IGNORE_ISSUE_LABELS)) and _is_evaluation_report(title, body):
        logger.info(f"Ignored event on {get_repo_full_name(payload)}: issue is 评测报告 (title feature)")
        return True
    matched = set(labels) & set(IGNORE_ISSUE_LABELS)
    if matched:
        logger.info(f"Ignored event on {get_repo_full_name(payload)}: issue carries ignore label(s) {sorted(matched)}")
        return True
    return False


def check_pr_dup_skill(repo, number, pr=None):
    """PR 入口硬门槛: 同一个 Skill 不能存在多个 open PR(2026-08-16 用户要求)。

    原为审查检测点32(🔴), 现上移为 handle_event 入口检查(在
    check_pr_file_limit 之前执行): 不通过直接阻断(发评论提示 PR 创建者 +
    跳过整个 PR 处理), 不进预校验/审查/测试。

    判定: 拉仓库所有 open PR, 排除当前 PR, 逐个取变更文件提取 skill 目录,
    与当前 PR 的 skill 目录集合比对, 有交集即判重复。查询失败 fail-open
    放行(不误伤, 阻断代价大)。
    返回 True=阻断 / False=放行。
    """
    if not (repo and number):
        return False
    try:
        # 当前 PR 的 skill 目录集合(用变更文件路径推断, 与 _extract_skill_dirs 同口径)
        cur_files = _fetch_pr_files(repo, number)
        cur_dirs = set(_extract_skill_dirs(cur_files).keys())
        if not cur_dirs:
            return False  # 当前 PR 无 skill 变更, 无从判重
        _open_prs = gitcode_get(f"/repos/{repo}/pulls?state=open&per_page=100")
        _open_prs = _open_prs if isinstance(_open_prs, list) else []
        dup_links = []
        for _op in _open_prs:
            _op_num = _op.get("number")
            if _op_num is None or str(_op_num) == str(number):
                continue  # 排除当前 PR
            try:
                _op_files = gitcode_get(
                    f"/repos/{repo}/pulls/{_op_num}/files?per_page=100")
                _op_files = _op_files if isinstance(_op_files, list) else []
            except Exception:
                continue  # 单 PR 查询失败, 跳过该 PR
            _op_dirs = set(_extract_skill_dirs(_op_files).keys())
            if _op_dirs & cur_dirs:
                dup_links.append(
                    f"[PR #{_op_num}](https://gitcode.com/{repo}/pull/{_op_num})")
        if not dup_links:
            return False
        # 命中重复: 幂等评论 + 阻断
        if not _pr_has_bot_comment(repo, number, MARKERS["pr_dup_skill"]):
            _author = ""
            try:
                _author = (((pr or {}).get("user") or {}).get("login")) or ""
            except Exception:
                pass
            _at = f" @{_author}" if _author else ""
            body = (
                f"🚫 该 Skill 已有其他 open PR 在处理中, 请勿重复提交。\n"
                f"重复 PR: {', '.join(dup_links)}\n"
                f"请先关闭或合并上述 PR 后再提交新的变更。{_at}"
            )
            safe_notify(gitcode_pr_comment, repo, number, bot_comment(body))
        logger.info(f"[pr-dup-skill] PR #{number} 命中同 skill 多 PR, 阻断")
        return True
    except Exception as e:
        logger.warning(f"[pr-dup-skill] PR #{number} 判重查询失败, 放行: {e}")
        return False


def check_pr_file_limit(repo, number) -> bool:
    """PR 变更文件数超过上限 -> 发评论提示并返回 True(跳过处理)。

    对齐 devops-bot event_processor._check_pr_file_limit:
    超过 50 个文件的 PR 无法完整审查, 提示作者按 skill 规范拆分后跳过。
    """
    if not (repo and number):
        return False
    try:
        data = gitcode_get(f"/repos/{repo}/pulls/{number}/files?per_page=100&page=1")
        file_count = len(data) if isinstance(data, list) else 0
    except Exception as e:
        logger.warning(f"PR file limit check failed: {e}")
        return False
    if file_count <= PR_FILE_LIMIT:
        logger.info(f"PR file count check passed: {file_count} files <= {PR_FILE_LIMIT}")
        return False
    logger.info(f"PR file count exceeded: {file_count} > {PR_FILE_LIMIT}, posting comment and skipping")
    # 幂等: GitCode 对 PR 生命周期连推多个 open/update 事件(已知行为), 无检查
    # 时每个事件都重发同款提示评论(PR #41 实测 3 秒内重复 3 条)。复用
    # _pr_has_bot_comment 模式, marker 与评论正文真实子串一致。
    if _pr_has_bot_comment(repo, number, MARKERS["pr_file_limit"]):
        logger.info(f"PR file limit notice already posted for {repo} PR #{number}, skipping")
        return True
    body = (
        f"⚠️ 本 PR 包含 {file_count} 个变更文件, 超过单次审查上限({PR_FILE_LIMIT} 个文件)。"
        f"请严格遵守 skill 创建规范, 每个 PR 只包含一个 Skill 的变更, 以便进行完整审查。"
    )
    safe_notify(gitcode_pr_comment, repo, number, bot_comment(body))
    return True


# ==================== PR 预校验(skill-pr 阶段0 迁移, 2026-08-04) ====================
# 对齐 devops-bot skill-pr/SKILL.md 阶段0: PR 创建/更新事件在进入测试小队前,
# 先做配置检查(标题规范/描述/关联Issue/标签/Assignee), 检查通过才继续后续流程。
# 背景: PR #126 迁移遗漏, 无预校验直接 @skills_test 测试, 与 PR #125(skill-pr
# 处理, 有"PR 配置检查"评论 + 标题自动修正)行为不一致。
# 标题格式（#70 选项B）：add skill: <路径> 或 update skill: <路径>
# 路径 = SKILL.md 所在目录，相对 /skills/，不含前缀和版本后缀
# 注意：此正则仅用作格式文档，pr_precheck 中用 _infer_pr_title 推断标题
PR_TITLE_RE = re.compile(
    r"^(add|update)\s+skill:\s+[a-zA-Z0-9_/-]+$",
    re.IGNORECASE,
)
# 允许的分支 PR 标题(自动生成 PR 常见), 命中则跳过标题规范检查
PR_TITLE_SKIP_PREFIX = ("deps:", "chore", "ci:", "docs:", "refactor", "revert")


def pr_precheck(repo, pr_number, title, body):
    """PR 配置检查(对齐 skill-pr 阶段0), 返回 (ok, issues, fixed_title)。

    issues: [(level, text)] — level ∈ ok/warn/fix(自动修复项)。
    检查项: ① 标题规范(不匹配自动修复) ② 描述非空 ③ 关联 Issue
            ④ 分类标签 ⑤ Assignee。
    全部通过 ok=True; 有 warn/fix 项 ok=False(不阻塞处理, 但评论提醒)。
    """
    issues = []
    fixed_title = None

    # ① 标题自动修正（#70 选项B: 总是基于仓库现状推断）
    skip = (title or "").strip().lower().startswith(PR_TITLE_SKIP_PREFIX)
    if not skip:
        new_title = _infer_pr_title(repo, pr_number, title)
        if new_title and new_title != title:
            try:
                gitcode_patch(f"/repos/{repo}/pulls/{pr_number}", {"title": new_title})
                fixed_title = new_title
                issues.append(("fix", f"✅ 标题已修正为 `{new_title}`"))
            except Exception as e:
                logger.warning(f"[pr-precheck] 标题自动修正失败: {e}")
                issues.append(("warn", f"❌ PR 标题自动修正失败: {str(e)[:120]}"))
        elif new_title and new_title == title:
            issues.append(("ok", f"✅ 标题已正确: `{title}`"))
        else:
            issues.append(("warn", f"❌ 无法推断合规标题"))
    else:
        issues.append(("ok", f"✅ 跳过标题检查（{title}）"))

    # ② 描述非空
    if (body or "").strip():
        issues.append(("ok", "✅ 描述完整, 含背景与变更内容"))
    else:
        issues.append(("warn", "⚠️ PR 描述为空, 请描述变更背景、改动内容和影响"))

    # ③ 关联 Issue(body 含 Closes/Fixes/Related #N)
    issue_link = re.search(r"(?:closes|fixes|resolves|related|关联)\s*#?\s*(\d+)",
                           body or "", re.IGNORECASE)
    if issue_link:
        issues.append(("ok", f"✅ 已关联 Issue #{issue_link.group(1)}"))
    else:
        issues.append(("warn", "📎 未检测到关联 Issue, 请确认是否需要关联"))

    # ④ 分类标签 ⑤ Assignee(拉 PR 详情)
    labels, assignee = [], None
    try:
        d = gitcode_get(f"/repos/{repo}/pulls/{pr_number}")
        labels = [l.get("name") if isinstance(l, dict) else str(l)
                  for l in (d.get("labels") or [])]
        a = d.get("assignee")
        assignee = a.get("login") if isinstance(a, dict) else a
    except Exception as e:
        logger.warning(f"[pr-precheck] 拉取 PR 详情失败: {e}")
    if any(lb in ("bug", "feature", "enhancement") for lb in labels):
        issues.append(("ok", "✅ 已设置分类标签"))
    else:
        issues.append(("warn", "🏷️ PR 缺少分类标签, 请添加(如 `feature`)"))
    if assignee:
        issues.append(("ok", f"✅ 已设置 Assignee: @{assignee}"))
    else:
        issues.append(("warn", "👤 PR 未设置 Assignee, 请指定处理人"))

    ok = all(level == "ok" for level, _ in issues)
    return ok, issues, fixed_title


def _infer_pr_title(repo, pr_number, title):
    """从 PR 变更文件列表推断符合规范的标题(对齐 skill-pr 阶段0-2a/2b)。

    规则: 取第一个 skills/ 开头的文件, 提取 skill 路径; 有 added 无 modified -> add,
    否则 -> update。返回推断标题或 None。
    """
    try:
        data = gitcode_get(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page=1")
        files = data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[pr-precheck] 获取 PR 文件列表失败: {e}")
        return None
    skill_files, has_added, has_modified = [], False, False
    for f in files:
        fn = f.get("filename") or ""
        st = (f.get("status") or "").lower()
        if st == "added":
            has_added = True
        elif st == "modified":
            has_modified = True
        m = re.match(r"^skills/([a-zA-Z0-9_/-]+?)/?(?:SKILL\.md|references/|scripts/|templates/)", fn)
        if m:
            skill_files.append(m.group(1).rstrip("/"))
    if not skill_files:
        # 退化为: 取第一个 skills/ 路径
        for f in files:
            fn = f.get("filename") or ""
            m = re.match(r"^skills/([a-zA-Z0-9_/-]+?)/", fn)
            if m:
                skill_files.append(m.group(1).rstrip("/"))
    if not skill_files:
        return None
    # 2026-08-18 修复: add/update 判定应以**仓库现状**为准, 而非变更文件
    # status —— PR #24 实测: 给已有 skill(huawei-cloud-rds-smart-service)
    # 加文件, 变更全是 added, 旧规则误判为 "add skill:", 实际应为 update。
    # 仓库已存在该 skill 目录 -> update(无论变更 added/modified);
    # 仓库无该目录(新增 skill) -> 有 added 无 modified -> add, 否则 -> update。
    # 2026-08-18 二次修复: skill_files[0] 是**去掉 skills/ 前缀**的路径
    # (正则 m.group(1) 提取), 查仓库必须补回前缀 —— PR #25 实测: 传无前缀
    # 路径 ls-tree 查不到(False) -> 回退 status 判定 -> 仍误判 add。
    try:
        if _repo_dir_exists(repo, f"skills/{skill_files[0]}") is True:
            return f"update skill: {skill_files[0]}"
    except Exception as _e:
        logger.warning(f"[pr-precheck] 仓库存在性查询失败(回退 status 判定): {_e}")
    action = "add" if (has_added and not has_modified) else "update"
    return f"{action} skill: {skill_files[0]}"


def pr_precheck_and_notify(repo, pr_number, title, body):
    """执行 PR 预校验并评论提醒; 返回 (ok, issues, fixed_title)。

    幂等: 已有 [skills_bot] PR 配置检查 评论则跳过提醒(不重复刷屏)。
    """
    ok, issues, fixed_title = pr_precheck(repo, pr_number, title, body)
    warn_items = [t for lv, t in issues if lv != "ok"]
    if not warn_items:
        return ok, issues, fixed_title
    # 幂等: 已发过预校验评论则跳过
    try:
        d = gitcode_get(f"/repos/{repo}/pulls/{pr_number}/comments?per_page=20")
        comments = d if isinstance(d, list) else []
        for c in comments:
            cb = c.get("body") or ""
            if cb.startswith(BOT_PREFIX) and "PR 配置检查" in cb:
                return ok, issues, fixed_title  # 已提醒过
    except Exception:
        pass
    lines = "\n".join(f"- {t}" for t in warn_items)
    # 2026-08-09 用户要求(#171): 去掉末尾"请按 skill 仓库规范补齐上述配置项,
    # 以便进入测试流程"提示 —— 配置检查是"评论提醒、不阻断"的(warn 项不影响
    # 进入测试流程, 审查报告 fail 才阻断), 该提示暗示补齐是进测试的前提,
    # 与实际行为矛盾且多余。
    comment = f"PR 配置检查：\n{lines}"
    safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(comment))
    return ok, issues, fixed_title


# ==================== PR 审查报告(skill-pr 阶段1-5 迁移, 2026-08-04) ====================
# 对齐 devops-bot skill-pr/SKILL.md 阶段1-5: 预校验通过后输出完整审查报告
# (关联Issue/变更摘要/Skill 审查 30 项/安全扫描/合并检查), 报告通过才进入测试小队。
# 背景: 用户要求 "## PR 审查报告也要有"(对齐 PR #125 的完整审查报告评论)。
# 自动化实现: 基于 PR 变更文件列表 + patch.diff 内容, 执行 skill-checklist.md
# 30 项检测点(结构/规范类可自动化, 语义类做启发式检查), 输出与 #125 同构的报告。

# Skill 审查检测点定义(对齐 skill-checklist.md): (编号, 检查内容, 严重级别)
SKILL_CHECKLIST = [
    (1,  "Skill目录包含SKILL.md", "🔴"),
    (2,  "Skill目录在skills/下", "🔵"),
    (3,  "技能包命名规范", "🟠"),
    (4,  "一个PR只提交一个Skill", "🔴"),
    (5,  "YAML Frontmatter存在", "🔴"),
    (6,  "name字段存在", "🔴"),
    (7,  "description字段存在", "🔴"),
    (8,  "description包含触发词", "🟡"),
    (9,  "概述章节", "🟠"),
    (10, "前置条件章节", "🟠"),
    (11, "工作流章节", "🟠"),
    (12, "核心命令章节", "🟠"),
    (13, "参数确认章节", "🟠"),
    (14, "参考文档章节", "🔴"),
    (15, "KooCLI命令格式标准章节", "🔵"),
    (16, "references/cli-installation-guide.md", "🟠"),
    (17, "references/iam-policies.md", "🔴"),
    (18, "references/verification-method.md", "🟡"),
    (19, "references/acceptance-criteria.md", "🔵"),
    (20, "参考文档kebab-case命名", "🔵"),
    (21, "凭证硬编码", "🔴"),
    (22, "Skill间直接调用", "🔴"),
    (23, "CLI写操作", "🔵"),
    (24, "服务名要求", "🟡"),
    (25, "操作名PascalCase", "🟡"),
    (26, "包含--cli-region", "🟡"),
    (27, "Skill总大小限制", "🟡"),
    (28, "文件数量限制", "🟡"),
    (29, "SKILL.md行数限制", "🟡"),
    (30, "文件类型白名单校验", "🟡"),
    (31, "Skill目录存在性检查", "🟡"),
    (32, "空文件检查", "🔵"),
    (33, "与仓库已有skill功能重复", "🟡"),
]
ALLOWED_FILE_EXTS = {"md","mdx","txt","json","json5","yaml","yml","toml","js","cjs","mjs",
                     "ts","tsx","jsx","py","sh","ps1","psm1","psd1","r","rb","go","rs",
                     "swift","kt","java","cs","cpp","c","h","hpp","sql","csv","tsv","ini",
                     "cfg","conf","env","properties","dat","xml","html","css","scss","sass","svg"}


def _patch_to_content(patch):
    """从 patch.diff 提取文件内容(去掉 diff 行号和 +/- 前缀)"""
    if not patch:
        return ""
    diff = patch.get("diff") if isinstance(patch, dict) else str(patch)
    lines, started = [], False
    for ln in diff.splitlines():
        if ln.startswith("@@"):
            started = True
            continue
        if not started:
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            lines.append(ln[1:])
        elif ln.startswith("-") and not ln.startswith("---"):
            pass  # 删除行跳过(added 文件全为 +; modified 文件用 raw 内容更好)
        elif not ln.startswith(("+", "-")):
            lines.append(ln)
    return "\n".join(lines)


def _fetch_pr_files(repo, pr_number):
    """拉取 PR 变更文件列表(含 patch/raw_url), 返回 list"""
    try:
        data = gitcode_get(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page=1")
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[pr-review] 获取 PR 文件列表失败: {e}")
        return []


def _file_content(f, repo, pr_number):
    """获取文件完整内容: 优先 raw_url(added/modified 均完整), 失败用 patch 提取"""
    raw_url = f.get("raw_url") or ""
    if raw_url:
        try:
            req = urllib.request.Request(raw_url, method="GET")
            req.add_header("PRIVATE-TOKEN", GITCODE_TOKEN)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    return _patch_to_content(f.get("patch"))


def _extract_skill_dirs(files):
    """从变更文件列表提取 skill 目录集合: {skill_dir: [files]}。

    以含 SKILL.md 的目录为 skill 根(如 skills/network/ops/huawei-cloud-vpc-list),
    其余文件按最近一级 SKILL.md 归属分组; 无 SKILL.md 的文件归入所属 skill 目录。
    """
    dirs = {}
    names = [(f.get("filename") or "") for f in files]
    # 1. 找所有 SKILL.md 所在目录作为 skill 根
    skill_roots = []
    for fn in names:
        if fn.endswith("/SKILL.md") or fn.endswith("/SKILL.md/"):
            skill_roots.append(fn.rsplit("/SKILL.md", 1)[0])
    # 2026-08-18 修复(PR #23 实测): 嵌套 SKILL.md 不是独立 skill 根 ——
    # skill 内部的模板/样例目录(如 templates/fixture-skill/SKILL.md)是父
    # skill 的文件, 原实现把它们当独立 skill, 导致"PR 同时变更 2 个 skill
    # 目录(analyzer, analyzer/templates/fixture-skill)"误判违反单 skill 规范。
    # 规则: 短目录(父级)优先保留, 是其子路径的 SKILL.md 根一律过滤(归父根)。
    _roots_tmp = sorted(skill_roots, key=len)  # 父级(短)在前
    skill_roots = [r for i, r in enumerate(_roots_tmp)
                   if not any(r.startswith(p + "/") for p in _roots_tmp[:i])]
    # 2. 每个文件归属: 优先最近一级含 SKILL.md 的父目录; 无则向上找最近的
    #    skill 根目录(以目录名 huawei-cloud-* 识别), 避免把 scripts/ 等
    #    子目录误当 skill 根(#140 事故: PR 只改 ges skill 的脚本文件,
    #    兜底取"倒数第二层"把 scripts 目录当独立 skill 审查, 12/30 假失败)
    for f, fn in zip(files, names):
        if not fn:
            continue
        owner = None
        if fn.endswith("/SKILL.md"):
            # 嵌套 SKILL.md(模板/样例, 如 templates/fixture-skill/SKILL.md)
            # 归最近的父根(2026-08-18 修复, 与步骤1 过滤保持一致)
            _d = fn.rsplit("/SKILL.md", 1)[0]
            for root in sorted(skill_roots, key=len, reverse=True):
                if _d == root or _d.startswith(root + "/"):
                    owner = root
                    break
            owner = owner or _d
        else:
            for root in sorted(skill_roots, key=len, reverse=True):
                if fn.startswith(root + "/"):
                    owner = root
                    break
        if owner is None and fn.startswith("skills/"):
            # 无 SKILL.md 的 skills/ 文件: 向上找最近的 huawei-cloud-* 目录
            # 作为 skill 根(脚本/文档属于其所属 skill, 不单独成根)
            parts = fn.split("/")
            for i in range(len(parts) - 1, 1, -1):
                cand = "/".join(parts[:i])
                if re.search(r"huawei[-_]?cloud-[\w-]+$", cand.rsplit("/", 1)[-1]):
                    owner = cand
                    break
        if owner:
            dirs.setdefault(owner, []).append(f)
    return dirs


def _strip_docstrings_comments(src, ext):
    """剥离代码文件中的 docstring 与注释(检测点22 用), 返回剩余文本。

    背景(2026-08-07 PR #157 实测): 检测点22 对代码文件全文正则匹配
    huawei-cloud-* skill 名, 但 docstring/注释中的**示例引用**(如通用 SDK
    的使用示例 `@quality_report(skill_name="huawei-cloud-rds-list")`)不是
    真实的运行时跨 Skill 调用 —— skill_quality_sdk.py 三处引用全在文档
    字符串里, 被误判 🔴 阻断 PR 进测试。pitfall 52 只排除了 .md/.txt,
    未排除代码内的文档性文本, 本函数补齐: 仅剥离注释与 docstring,
    真实代码中的引用(import/调用/路径)仍会命中。
    """
    if not src:
        return src
    if ext == ".py":
        try:
            import io as _io
            import tokenize as _tk
            toks = list(_tk.generate_tokens(_io.StringIO(src).readline))
            # 收集需剥离的 (start_row, start_col, end_row, end_col) 区间:
            # - COMMENT token: 仅注释部分(列级, 不能清整行, 否则误删同行的真实调用)
            # - docstring STRING token: 语句开头的字符串(模块/函数/类 docstring)
            spans = []
            for i, tok in enumerate(toks):
                if tok.type == _tk.COMMENT:
                    spans.append((tok.start[0], tok.start[1], tok.end[0], tok.end[1]))
                elif tok.type == _tk.STRING:
                    prev = toks[i - 1] if i > 0 else None
                    if prev is None or prev.type in (_tk.NL, _tk.NEWLINE,
                                                     _tk.INDENT, _tk.DEDENT):
                        spans.append((tok.start[0], tok.start[1],
                                      tok.end[0], tok.end[1]))
            if not spans:
                return src
            lines = src.split("\n")
            for r1, c1, r2, c2 in spans:
                if r1 == r2:
                    if 1 <= r1 <= len(lines):
                        line = lines[r1 - 1]
                        lines[r1 - 1] = line[:c1] + " " * (c2 - c1) + line[c2:]
                else:
                    # 跨行(docstring): 首行从 c1 起、末行到 c2 止、中间整行清空
                    if 1 <= r1 <= len(lines):
                        lines[r1 - 1] = lines[r1 - 1][:c1]
                    for r in range(r1 + 1, r2):
                        if 1 <= r <= len(lines):
                            lines[r - 1] = ""
                    if 1 <= r2 <= len(lines):
                        lines[r2 - 1] = " " * min(c2, len(lines[r2 - 1])) \
                            + lines[r2 - 1][c2:]
            return "\n".join(lines)
        except Exception:
            return src
    elif ext in (".sh", ".bash", ".yaml", ".yml", ".toml", ".ini", ".rb",
                 ".php", ".js", ".ts", ".go", ".java", ".c", ".cpp"):
        # 整行注释剥离(# 开头, 含 shebang 无害——只影响 skill 名匹配)
        return "\n".join("" if re.match(r"^\s*#", ln) else ln
                         for ln in src.split("\n"))
    return src


def _llm_koocli_naming_review(skill_name, md_content):
    """LLM 语义检查 #24 服务名 / #25 操作名 PascalCase(对齐原 skill-pr)。

    原 skill-pr 由 LLM 逐项判定(checklist 注入 prompt): 服务名遵从 KooCLI
    规范(大写 ECS/VPC/EIP、首字母大写 CloudPond/IAMAccessAnalyzer 等);
    操作名使用 PascalCase(ListServersDetails 等)。规则代码无法精确复刻
    语义判断, 故复用 LLM(2026-08-09 用户要求对齐原 skill-pr)。

    返回 (service_ok, op_ok, reason); 调用失败返回 (True, True, reason)
    —— fail-open, 🟡 级不阻断, 宁放过不误报。
    仅 ENABLE_LLM_REVIEW 且 LLM_API_KEY 可用时启用; 否则保持硬编码通过。
    """
    if not (ENABLE_LLM_REVIEW and LLM_API_KEY):
        return True, True, "LLM 未启用, 保持通过(🟡 建议级)"
    prompt = (
        "你是华为云 Skill 仓库规范审查员。审查 SKILL.md 中 KooCLI(hcloud)命令的"
        "服务名与操作名格式。\n"
        "检查项:\n"
        "1. 服务名要求: 服务名遵从 KooCLI 规范 —— 全大写缩写(ECS/VPC/EIP)、"
        "首字母大写复合词(CloudPond/IAMAccessAnalyzer)等; 若 SKILL.md 未涉及"
        "具体服务名(无 CLI 命令), 视为通过。\n"
        "2. 操作名PascalCase: 操作名使用 PascalCase(如 ListServersDetails), "
        "不得是小驼峰/全小写/下划线; 若无操作名, 视为通过。\n"
        f"Skill 名称: {skill_name}\n"
        "SKILL.md 内容:\n"
        f"{(md_content or '')[:3000]}\n"
        "只输出 JSON: "
        '{"service_ok": true|false, "operation_ok": true|false, '
        '"reason": "一句话依据, 说明具体服务名/操作名"}\n'
    )
    try:
        resp = http_json(
            "POST", f"{LLM_API_BASE}/chat/completions",
            body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 300,
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=LLM_TIMEOUT,
        )
        content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        import re as _re
        s_ok = _re.search(r'"service_ok"\s*:\s*(true|false)', content, _re.IGNORECASE)
        o_ok = _re.search(r'"operation_ok"\s*:\s*(true|false)', content, _re.IGNORECASE)
        reason = _re.search(r'"reason"\s*:\s*"([^"]*)"', content)
        if not (s_ok and o_ok):
            return True, True, f"LLM 输出无法解析: {content[:120]}"
        service_ok = s_ok.group(1).lower() == "true"
        op_ok = o_ok.group(1).lower() == "true"
        return service_ok, op_ok, (reason.group(1) if reason else content[:80])
    except Exception as e:
        logger.warning(f"[llm-koocli] 调用失败, 保持通过: {e}")
        return True, True, f"LLM 调用失败: {e}"


def _repo_dir_exists(repo, path):
    """探测仓库目录是否存在(2026-08-16 检测点31 用)。

    返回: True=存在(contents 返回 list) / False=不存在(HTTP 404) /
    None=查询失败(限流/其他异常, fail-open 由调用方跳过确认)。
    必须区分 404 与 429: gitcode_get 把两者都包装成 RuntimeError,
    检测点31 需要\"目录不存在\"与\"查询失败\"两种语义, 故直接 urllib 请求。

    2026-08-18 本地化: 优先本地镜像 ls-tree(无 429 风险), 失败(返回 None)
    才回退原 API 逻辑。
    """
    _local = _mirror_dir_exists(repo, path)
    if _local is not None:
        return _local
    url = f"{GITCODE_API_BASE}/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url)
    req.add_header("PRIVATE-TOKEN", GITCODE_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return isinstance(data, list)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None
    except Exception:
        return None


def _repo_dir_files(repo, path, _depth=0, _max_depth=4):
    """递归收集仓库目录下所有**文件**路径(检测点28 合入后文件总数用)。

    2026-08-16 用户要求: 文件数量限制应校验"PR 合入后 skill 目录的文件
    总数"而非"PR 变更文件数"。本函数用 contents API 逐层递归收集文件:
    - 目录项 -> 继续下钻(深度上限 _max_depth, 防异常深链死循环)
    - 文件项 -> 记录完整路径
    返回 set(文件路径); 任一层查询失败(限流/异常)返回 None(fail-open,
    由调用方回退原逻辑)。429 退避重试(对齐 _get_repo_products 模式)。

    注: GitCode tree API 递归不完整(实测只返回 20 条), 必须 contents 递归。

    2026-08-18 本地化: 优先本地镜像 ls-tree -r(一次命令递归全量, 无 429),
    失败(返回 None)才回退原 API 递归逻辑。
    """
    _local = _mirror_dir_files(repo, path)
    if _local is not None:
        return _local
    if not (repo and path) or _depth > _max_depth:
        return set()
    try:
        data = gitcode_get(f"/repos/{repo}/contents/{path}?per_page=100")
        data = data if isinstance(data, list) else []
    except Exception as e:
        if "429" in str(e):
            time.sleep(1.0)
            try:
                data = gitcode_get(f"/repos/{repo}/contents/{path}?per_page=100")
                data = data if isinstance(data, list) else []
            except Exception:
                return None
        else:
            return None
    out = set()
    for it in data:
        name = it.get("name") or ""
        if not name:
            continue
        full = f"{path}/{name}"
        if it.get("type") == "dir":
            sub = _repo_dir_files(repo, full, _depth + 1, _max_depth)
            if sub is None:
                return None  # 子目录查询失败 -> 整体失败(fail-open 由调用方处理)
            out |= sub
        else:
            out.add(full)
    return out


def _skill_review(skill_dir, files, file_contents, repo=None, pr_number=None):
    """对单个 skill 目录执行 31 项检测点, 返回 (结论, [(#, 检查内容, 级别, ✅/❌/⚠️, 说明)])

    repo: 目标仓库(2026-08-14 新增, 检测点31 目录存在性检查用)。
    pr_number: 当前 PR 号(2026-08-16 新增, 检测点32 排除自身用)。
    """
    # 2026-08-09 #171 修复: names 合并 file_contents 的 key(pr_review_report
    # 已把未变更的 SKILL.md / references/ 文档补入缓存) —— 否则只改 1 个
    # 文件的 PR 里 references 文档不在变更列表, 被误报"缺失"。
    names = [f.get("filename") or "" for f in files]
    for extra in (file_contents or {}):
        if extra and extra not in names:
            names.append(extra)
    # 2026-08-09 自查修复: 文件数/白名单统计只用 **PR 变更文件**(补入的
    # references 文档是"仓库现状"不是"PR 新增文件", 计入会误报超限/白名单
    # 异常; references 补入仅用于 #16-19/#20 存在性检查)。
    _pr_files = [f.get("filename") or "" for f in files]
    skill_name = skill_dir.rstrip("/").split("/")[-1]
    # SKILL.md 优先取变更文件, 否则取缓存中该 skill 根的 SKILL.md
    # (pr_review_report 已把未变更的 SKILL.md 从仓库补入缓存, #140)
    skill_md = next((n for n in names if n.endswith("SKILL.md")), None)
    md_path = f"{skill_dir}/SKILL.md"
    if skill_md and skill_md in file_contents:
        md_content = file_contents.get(skill_md, "")
    else:
        md_content = file_contents.get(md_path, "")
    md_lines = md_content.count("\n") + 1
    # frontmatter 解析(支持单行值 + YAML 块标量 `|` / `|-` / `>` / `>-` 多行)
    # 2026-08-09 #171 修复: 原只处理 `|`, `>-`(折叠块标量)被当成普通值
    # `>-` 存入, 触发词检查匹配 `>-` 失败 -> "未检测到触发词"误判
    # (实测: 仓库 SKILL.md description 用 `>-` 且含 Query/query/查询/list,
    #  但报告报未检测到触发词)。
    fm = {}
    m = re.match(r"^---\s*\n(.*?)\n---", md_content, re.S)
    if m:
        fm_lines = m.group(1).splitlines()
        i, cur_key = 0, None
        while i < len(fm_lines):
            line = fm_lines[i]
            mm = re.match(r"^([A-Za-z_][\w]*)\s*:\s*(.*)$", line)
            if mm:
                cur_key = mm.group(1).strip()
                val = mm.group(2).strip()
                if val in ("|", "|-", ">", ">-"):
                    # 块标量: 收集后续缩进行(literal `|` 保留换行,
                    # folded `>` 折叠为单行/空格连接)
                    block = []
                    i += 1
                    while i < len(fm_lines) and (fm_lines[i].startswith("  ") or fm_lines[i].startswith("\t")):
                        block.append(fm_lines[i].strip())
                        i += 1
                    sep = "\n" if val.startswith("|") else " "
                    fm[cur_key] = sep.join(block)
                    continue
                fm[cur_key] = val
            i += 1
    results = []
    def add(idx, text, sev, ok, note):
        results.append((idx, text, sev, "✅" if ok else ("⚠️" if sev == "🔵" or sev == "🟡" else "❌"), note))
    # 1 SKILL.md 存在(缓存可能含仓库拉取的未变更 SKILL.md, #140)
    add(1, "Skill目录包含SKILL.md", "🔴", bool(md_content),
        f"SKILL.md {'存在(' + str(md_lines) + ' 行)' if md_content else '缺失!'}")
    # 2 在 skills/ 下
    add(2, "Skill目录在skills/下", "🔵", skill_dir.startswith("skills/"), f"`{skill_dir}`")
    # 3 命名规范 huawei-cloud-{product}-{function}
    naming_ok = bool(re.match(r"^huawei-cloud-[a-z0-9]+(-[a-z0-9]+)+$", skill_name))
    add(3, "技能包命名规范", "🟠", naming_ok,
        f"`{skill_name}` " + ("符合 `huawei-cloud-{product}-{function}`" if naming_ok else "不符合 huawei-cloud-* 规范"))
    # 4 一个 PR 只提交一个 Skill(由调用方传入多目录判定, 这里仅记录)
    add(4, "一个PR只提交一个Skill", "🔴", True, "由 PR 级检查判定")
    # 5-8 frontmatter
    add(5, "YAML Frontmatter存在", "🔴", bool(fm), "`---` 包裹" if fm else "缺失 frontmatter")
    # 6 name 字段存在且与目录名一致(2026-08-18 用户自查要求: 原只查存在性,
    # name≠目录名仍 ✅ —— PR #15 实测 name=huawei-cloud-billing-scout 而目录
    # =huawei-cloud-billing-scout2, 判重/引用按 name 走会错位; 不一致是实质
    # 缺陷应 ❌ 并给出具体改法)
    _name_raw = fm.get("name", "")
    _name_norm = _name_raw.strip("\"'").strip().lower().replace("_", "-")
    _name_match = bool(_name_norm) and _name_norm == skill_name
    add(6, "name字段存在", "🔴", _name_match,
        (f"`{_name_raw.strip()}` 与目录名一致" if _name_match else
         (f"`{_name_raw.strip()}` ≠ 目录名 {skill_name} —— 请将 name 改为 {skill_name}"
          if _name_norm else "name 字段缺失")))
    desc = fm.get("description", "")
    add(7, "description字段存在", "🔴", bool(desc), "含功能概要" if desc else "缺失")
    trig = re.search(r"(查询|列表|list|查询|获取|创建|删除|update|create|delete|query)", desc or "", re.IGNORECASE)
    add(8, "description包含触发词", "🟡", bool(trig), "含触发词" if trig else "未检测到触发词")
    # 9-15 章节检查(SKILL.md 标题)
    md_lower = md_content.lower()
    def has_sec(*kws):
        return any(kw.lower() in md_lower for kw in kws)
    add(9,  "概述章节", "🟠", has_sec("overview", "概述", "功能", "introduction", "简介"),
        "Overview/概述" if has_sec("overview", "概述", "introduction", "简介") else "未检测到概述章节")
    add(10, "前置条件章节", "🟠", has_sec("prerequisite", "前置条件", "准备", "requirements", "前提"),
        "Prerequisites/前置条件" if has_sec("prerequisite", "前置条件", "准备", "前提") else "未检测到前置条件章节")
    add(11, "工作流章节", "🟠", has_sec("workflow", "工作流", "流程", "steps", "步骤"),
        "Workflow/流程" if has_sec("workflow", "工作流", "流程", "步骤") else "未检测到工作流章节")
    add(12, "核心命令章节", "🟠", has_sec("command", "命令", "cli", "hcloud"),
        "Commands/命令" if has_sec("command", "命令", "cli", "hcloud") else "未检测到核心命令章节")
    add(13, "参数确认章节", "🟠", has_sec("parameter", "参数", "argument", "confirmation"),
        "参数章节" if has_sec("parameter", "参数", "argument") else "未检测到参数章节")
    add(14, "参考文档章节", "🔴", has_sec("reference", "参考", "相关文档"),
        "References/参考" if has_sec("reference", "参考", "相关文档") else "未检测到参考文档章节")
    add(15, "KooCLI命令格式标准章节", "🔵", has_sec("koocli", "命令格式", "format"),
        "KooCLI 格式章节" if has_sec("koocli", "命令格式", "format") else "未检测到(涉及CLI时必需)")
    # 16-19 references 文件
    refs = [n for n in names if "/references/" in n]
    for idx, fn, sev in ((16, "references/cli-installation-guide.md", "🟠"),
                          (17, "references/iam-policies.md", "🔴"),
                          (18, "references/verification-method.md", "🟡"),
                          (19, "references/acceptance-criteria.md", "🔵")):
        hit = any(n.endswith(fn) for n in refs)
        add(idx, fn, sev, hit, "存在" if hit else "缺失")
    # 20 kebab-case 命名(仅 references/ 下文档, 排除 SKILL.md/scripts/templates)
    ref_basenames = [os.path.basename(n) for n in refs if n.endswith(".md")]
    kebab_bad = [b for b in ref_basenames if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*\.md$", b)]
    add(20, "参考文档kebab-case命名", "🔵", not kebab_bad,
        "全部小写 kebab-case" if not kebab_bad else f"非 kebab-case: {kebab_bad[:3]}")
    # 21 凭证硬编码(扫描全部内容; 排除文档中说明性的 hcloud configure set 教程/占位符)
    secret_hits = []
    for n, c in file_contents.items():
        if not n.startswith(skill_dir):
            continue
        for pat, desc in (
            (r"(?i)(access[_-]?key|secret[_-]?key|ak|sk)\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{20,}", "AK/SK 赋值"),
            (r"AKIA[0-9A-Z]{16}", "AWS AKIA"),
            (r"--cli-access-key\s*=\s*[A-Z0-9]{16,}", "hcloud AK 实值"),
        ):
            mm = re.search(pat, c)
            if mm:
                line_start = c.rfind("\n", 0, mm.start()) + 1
                line_end = c.find("\n", mm.start())
                line_text = c[line_start:line_end if line_end != -1 else len(c)]
                if "{" in line_text and "}" in line_text:
                    continue  # 占位符(如 {AccessKeyId})非硬编码
                secret_hits.append(f"{os.path.basename(n)}:{c[:mm.start()].count(chr(10))+1}({desc})")
    add(21, "凭证硬编码", "🔴", not secret_hits,
        "无 AK/SK 硬编码" if not secret_hits else f"发现疑似硬编码: {', '.join(secret_hits[:3])}")
    # 22 Skill间直接调用(检测引用其他 huawei-cloud-* skill 名, 排除自身)
    # 2026-08-05 修复: 文档(.md/.txt)提及其他 skill 是正常引导(如"如需写操作,
    # 请使用 huawei-cloud-xxx skill"), 不是代码级直接调用 —— PR #142 的 SKILL.md
    # 只读指引曾被误判 🔴 阻断, 导致测试失败后的新 commit 无法重新触发测试。
    # 仅扫描脚本/代码类文件(.sh/.py/.js/.go/.java/.json/.yaml 等)。
    # 2026-08-07 修复(PR #157): docstring/注释里的**示例引用**(通用 SDK 使用
    # 示例 @quality_report(skill_name="huawei-cloud-rds-list"))同样不算运行时
    # 调用, 匹配前先剥离注释与 docstring(_strip_docstrings_comments), 只留
    # 真实代码文本, 避免误报阻断 PR 进测试。
    _CODE_EXTS = (".sh", ".py", ".js", ".go", ".java", ".c", ".cpp", ".ts",
                  ".rb", ".php", ".json", ".yaml", ".yml", ".toml", ".ini")
    other_skill_refs = []
    for n, c in file_contents.items():
        if not n.startswith(skill_dir):
            continue
        if not n.lower().endswith(_CODE_EXTS):
            continue
        _ext = os.path.splitext(n)[1].lower()
        scan_c = _strip_docstrings_comments(c, _ext)
        for hit in set(re.findall(r"huawei-cloud-[a-z0-9-]+", scan_c)):
            if hit != skill_name and hit not in other_skill_refs:
                other_skill_refs.append(f"{os.path.basename(n)}→{hit}")
    add(22, "Skill间直接调用", "🔴", not other_skill_refs,
        "无跨 Skill 引用" if not other_skill_refs else f"疑似引用: {other_skill_refs[:3]}")
    # 23 CLI 写操作
    write_ops = re.findall(r"\b(hcloud\s+\w+\s+(create|delete|update|start|stop|restart|modify)\b)",
                           md_content, re.IGNORECASE)
    add(23, "CLI写操作", "🔵", len(write_ops) == 0,
        "全部只读操作" if not write_ops else f"含写操作 {len(write_ops)} 处(需用户确认)")
    # 24 服务名要求 25 操作名PascalCase(对齐原 skill-pr: LLM 语义判断,
    # 2026-08-09 用户要求; 失败 fail-open 保持通过)
    _svc_ok, _op_ok, _naming_reason = _llm_koocli_naming_review(skill_name, md_content)
    add(24, "服务名要求", "🟡", _svc_ok,
        ("服务名合规" if _svc_ok else f"服务名不合规: {_naming_reason}"))
    add(25, "操作名PascalCase", "🟡", _op_ok,
        ("操作名合规" if _op_ok else f"操作名非 PascalCase: {_naming_reason}"))
    # 26 --cli-region
    region_ok = "--cli-region" in md_content or "cli-region" in md_content
    add(26, "包含--cli-region", "🟡", region_ok, "含 --cli-region" if region_ok else "未检测到 --cli-region")
    # 27 总大小限制
    total_size = sum(len(c.encode("utf-8")) for n, c in file_contents.items() if n.startswith(skill_dir))
    add(27, "Skill总大小限制", "🟡", total_size < 30 * 1024 * 1024, f"{total_size//1024}KB < 30MB")
    # 28 文件数量限制(2026-08-16 用户要求改造): 校验"PR 合入后 skill 目录的
    # 文件总数" = 仓库现有文件 ∪ PR 变更文件(去重)。旧逻辑只统计 PR 变更
    # 文件数, 已合入 skill 的存量文件不计入, 无法反映合入后真实规模。
    # 仓库清单拉取失败(限流/异常) fail-open 回退旧逻辑(只统计 PR 变更文件)。
    _existing_files = None
    if repo:
        try:
            _existing_files = _repo_dir_files(repo, skill_dir)
        except Exception as _e:
            logger.warning(f"[skill-review] #28 仓库文件清单查询失败: {_e}")
            _existing_files = None
    if _existing_files is None:
        add(28, "文件数量限制", "🟡", len(_pr_files) <= 40,
            f"{len(_pr_files)} 个文件(PR 变更, 仓库清单查询失败回退)")
    else:
        _merged = _existing_files | set(_pr_files)
        add(28, "文件数量限制", "🟡", len(_merged) <= 40,
            f"{len(_merged)} 个文件(仓库 {len(_existing_files)} + PR 新增 {len(set(_pr_files) - _existing_files)})")
    # 29 SKILL.md 行数限制
    add(29, "SKILL.md行数限制", "🟡", md_lines <= 500, f"{md_lines} 行")
    # 30 文件类型白名单(只统计 PR 变更文件, 2026-08-09 自查修复)
    bad_ext = [os.path.basename(n) for n in _pr_files
               if "." in n and n.rsplit(".", 1)[-1].lower() not in ALLOWED_FILE_EXTS]
    add(30, "文件类型白名单校验", "🟡", not bad_ext,
        "均在白名单内" if not bad_ext else f"非白名单: {bad_ext[:3]}")
    # 31 Skill 目录存在性检查(2026-08-14 用户要求; 2026-08-16 升级):
    # 对 SKILL.md 所在路径的**每一层父目录**做仓库存在性检查, 直接给出
    # 逐层结论(断点在哪一层一目了然)。不再写"add 属正常/update 请核对"
    # 这类需人工判断的文案 —— skills/ 层必存在; 中间层缺失 = 类目路径
    # 错误(如 skills/network/ 存在但 ops/ 不存在); 仅最后一层缺失 =
    # skill 根为新增(客观事实陈述)。
    # 实现: 先查最深层(存在则父层必然存在, 1 次 API); 不存在则自浅向深
    # 逐层探测找断点(≤路径层数 API); 任一查询失败 fail-open 跳过确认。
    if repo:
        _parts = skill_dir.split("/")
        _chain = []
        try:
            # 先查最深层: 存在则父层必然存在(1 次 API)
            _deep_ok = _repo_dir_exists(repo, skill_dir)
            if _deep_ok is True:
                _chain = [("/".join(_parts[:i]), True)
                          for i in range(1, len(_parts) + 1)]
            else:
                # 自浅向深逐层探测找断点(≤路径层数 API)
                for i in range(1, len(_parts) + 1):
                    _p = "/".join(_parts[:i])
                    _e = _repo_dir_exists(repo, _p)
                    _chain.append((_p, _e))
                    if _e is not True:
                        break
        except Exception as _e:
            logger.warning(f"[skill-review] #31 存在性探测异常: {_e}")
            _chain = [(_p, None) for _p in
                      ["/".join(_parts[:i]) for i in range(1, len(_parts) + 1)]]
        if any(_e is None for _, _e in _chain):
            add(31, "Skill目录存在性检查", "🟡", True,
                "仓库存在性查询失败(限流/异常), 跳过确认")
        else:
            _chain_str = " → ".join(
                f"{_p.rsplit('/', 1)[-1]}{'✅' if _e else '❌'}"
                for _p, _e in _chain)
            _missing = [_p for _p, _e in _chain if not _e]
            if not _missing:
                add(31, "Skill目录存在性检查", "🟡", True,
                    f"路径完整存在: {_chain_str}")
            elif len(_missing) == 1 and _missing[0] == skill_dir:
                # 2026-08-18 修复: 仅末级(skill 根)缺失 = **新增 skill**(PR 未
                # 合入, master 无此目录是正常预期, 不是异常) -> ✅ 客观陈述。
                # 原实现标 ⚠️"路径断裂"误导(PR #15/#21 实测: 新增 skill 被
                # 误标为路径断裂, 用户明确\"skill 目录不应被扫出不存在\")。
                add(31, "Skill目录存在性检查", "🟡", True,
                    f"skill 根为新增(仓库无此目录属正常, PR 合入后存在): {_chain_str}")
            else:
                add(31, "Skill目录存在性检查", "🟡", False,
                    f"路径断裂: {_chain_str} —— 仓库无 `{_missing[0]}` 目录")
    else:
        add(31, "Skill目录存在性检查", "🟡", True, "无仓库上下文, 跳过确认")
    # 32 空文件检查(2026-08-16 用户要求, 替换原"同一个Skill不能存在多个PR"):
    # 检查该 skill 的 PR 变更文件中是否有空文件(内容为空/仅空白), 有则
    # 列出文件名提示。🔵 提示级, 不阻断。
    # 注: 原检测点32(同skill多PR)已上移为入口硬门槛(handle_event 在
    # check_pr_file_limit 之前执行), 见 check_pr_dup_skill。
    _empty_files = []
    for _fn in _pr_files:
        _content = file_contents.get(_fn)
        if _content is None:
            try:
                _content = _file_content(
                    {"filename": _fn, "patch": "", "raw_url": ""},
                    repo, pr_number)
            except Exception:
                _content = None
        if _content is not None and not _content.strip():
            _empty_files.append(_fn.rsplit("/", 1)[-1])
    if _empty_files:
        add(32, "空文件检查", "🔵", False,
            f"存在空文件: {', '.join(_empty_files)}")
    else:
        add(32, "空文件检查", "🔵", True, "无空文件")
    # 33 与仓库已有skill功能重复(2026-08-17 用户要求): 仅对**新增 skill**
    # (仓库无此目录)判断 —— PR 提交的新 skill 功能是否与仓库已有 skill
    # 语义重复。复用 _llm_skills_covers_issue(粗筛打分+LLM 语义判重,
    # fail-open)。命中 -> 🟡 提示列出相似 skill 名(不阻断, LLM 判重偏严格,
    # 同产品细分功能可能被覆盖); 查询失败/无上下文/update 场景跳过确认。
    # 注: 只查仓库已有 skill(不含当前 PR 的 skill, 因为仓库无此目录才检查)。
    if repo:
        try:
            _dir_ok = _repo_dir_exists(repo, skill_dir)
        except Exception:
            _dir_ok = None
        if _dir_ok is True:
            # 仓库已有该 skill 目录 -> update 迭代, 同名存在是正常前提
            add(33, "与仓库已有skill功能重复", "🟡", True,
                "仓库已有该 skill(迭代更新, 跳过功能重复检查)")
        elif _dir_ok is None:
            add(33, "与仓库已有skill功能重复", "🟡", True,
                "仓库目录探测失败, 跳过确认")
        else:
            # 新增 skill: 用 SKILL.md frontmatter description 做功能描述
            _func_desc = fm.get("description") or md_content[:300]
            if not (_func_desc or "").strip():
                # 2026-08-17 守卫: 仓库无目录且 PR 无 SKILL.md 时功能描述为空,
                # LLM 拿空文本判重结果不可靠, 直接跳过(该场景会被检测点1 🔴
                # 阻断, 此处仅避免 33 产生无意义提示)。
                add(33, "与仓库已有skill功能重复", "🟡", True,
                    "无法获取功能描述(PR 缺 SKILL.md 且仓库无此目录), 跳过确认")
            else:
                _cov, _sk, _rs = _llm_skills_covers_issue(
                    repo, f"{skill_name} {_func_desc}", "")
                if _cov and _sk:
                    # 2026-08-18 用户要求(8-18 拍板): 判重类输出只列候选供
                    # 人工比对, 不写结论性判定(LLM 判重不准, 用户 8-17 质疑)
                    # —— 原文案"仓库已有功能重复的 skill"是结论性表述, 改为
                    # "疑似/请人工确认"。
                    add(33, "与仓库已有skill功能重复", "🟡", False,
                        f"疑似与已有 skill `{_sk}` 功能重叠, 请人工确认"
                        f"(LLM 判定依据: {_rs[:60]})")
                else:
                    add(33, "与仓库已有skill功能重复", "🟡", True,
                        "未发现功能重复的已有 skill")
    else:
        add(33, "与仓库已有skill功能重复", "🟡", True,
            "无仓库上下文, 跳过确认")
    # 结论: 任一 🔴 未过 -> ❌; 🟠 未过 -> ⚠️; 否则 ✅
    # 注意: results 第 4 元素是 add() 转换后的显示字符串(✅/❌/⚠️),
    # 不是布尔 —— 用 `ok is False` 判断永远为 False(2026-08-05 #139 实测:
    # 8 项未通过(含 4 项 🔴)结论仍显示 ✅ 通过, 类型错位)
    critical_fail = any(res == "❌" and sev == "🔴" for _, _, sev, res, _ in results)
    high_fail = any(res == "❌" and sev == "🟠" for _, _, sev, res, _ in results)
    conclusion = "❌" if critical_fail else ("⚠️" if high_fail else "✅")
    return conclusion, results


def _pr_issue_status(repo, issue_no):
    """查询关联 issue 状态(triaged/accepted 等), 供合并检查"""
    try:
        d = gitcode_get(f"/repos/{repo}/issues/{issue_no}")
        labels = [l.get("name") if isinstance(l, dict) else str(l) for l in (d.get("labels") or [])]
        state = d.get("state")
        return state, labels
    except Exception:
        return None, []


def _denoise_audit_issues(issues, tmp):
    """降噪(2026-08-11 用户要求方案2): 过滤/降级误报, 保留真实安全问题。

    规则:
    a. **文档类文件(.md/.txt/.rst 等)的 skillspector 命中** → 降为 warning(不阻断)
       —— 文档里出现 eval(/exec(/os.environ 等词多是描述文字, 非真实代码
       (PR #36 实测: skill-gate-report.txt 的 OH1 是审计报告文字描述)
    b. **E2 环境变量收割**: 按名读取单个变量(os.environ.get('XX')) → 安全放行(移除);
       遍历全部变量(os.environ.items()/environ.copy()) → 保留阻断
       —— 华为云 SDK 从环境变量读 AK/SK 是官方标准做法, 非凭据收割
    c. **注释行命中** → 移除(代码里的 #/// 注释提及危险模式不算真实调用)
    gitleaks 凭据类命中(rule 为小写 kebab 或含 key/token/password)不降噪
    (文档里硬编码凭据同样真实)。
    返回处理后的 issues 列表。
    """
    doc_exts = (".md", ".txt", ".rst", ".adoc")
    out = []
    for it in issues:
        rule = str(it.get("rule") or "")
        sev = it.get("severity") or ""
        fpath = str(it.get("file") or "")
        line_no = it.get("line") or 0
        # 凭据类(gitleaks)规则: 小写 kebab-case 或含 key/token/password/secret
        is_cred_rule = bool(re.match(r"^[a-z][a-z0-9-]+$", rule)) or \
            any(k in rule.lower() for k in ("key", "token", "password", "secret"))
        # 读命中行文本(判断 E2 形态/注释)
        line_text = ""
        if tmp and fpath and line_no:
            try:
                with open(os.path.join(tmp, fpath.lstrip("/")), "r",
                          encoding="utf-8", errors="replace") as _f:
                    _lines = _f.readlines()
                if 0 < line_no <= len(_lines):
                    line_text = _lines[line_no - 1]
            except Exception:
                pass
        # b. E2: 按名读单个变量 → 安全放行
        if rule == "E2" and not is_cred_rule:
            if "environ.items()" not in line_text and "environ.copy()" not in line_text \
                    and re.search(r"os\.environ\.get\s*\(\s*['\"]", line_text):
                logger.info("[skill-audit] 降噪: E2 按名读取环境变量(安全写法) %s:%s",
                            fpath, line_no)
                continue
        # d. OH1 输出注入: subprocess.run 安全形态(capture_output=True 或参数为
        #    变量/列表而非 shell 字符串) → 放行。skillspector 模式
        #    `subprocess\.\w+\([^)]*(?:response|output|...)` 会误伤
        #    `subprocess.run(cmd, capture_output=True)` —— capture_output 里的
        #    "output" 是关键字不是被执行的输出 (PR #36 L202/L270 实测误报)
        if rule == "OH1" and not is_cred_rule:
            if re.search(r"subprocess\.\w+\s*\(", line_text):
                if "capture_output=True" in line_text \
                        or re.search(r"subprocess\.\w+\s*\(\s*(?:\[|cmd\b|args\b)", line_text):
                    logger.info("[skill-audit] 降噪: OH1 subprocess 安全调用(非 shell 字符串) %s:%s",
                                fpath, line_no)
                    continue
        # c. 注释行命中 → 移除(代码注释里提及危险模式不算真实调用)
        stripped = line_text.strip()
        if stripped.startswith(("#", "//", "/*", "*", "--", "<!--")):
            logger.info("[skill-audit] 降噪: 注释行命中 %s:%s", fpath, line_no)
            continue
        # a. 文档类文件 + 非凭据规则 → 降 warning
        if fpath.lower().endswith(doc_exts) and not is_cred_rule:
            it["severity"] = "warning"
            out.append(it)
            continue
        out.append(it)
    return out


def _run_skill_audit_scan(repo, pr_number, files_map, branch):
    """用 huawei-cloud-skill-audit 扫描 PR 涉及的 skill 文件(替代内置正则扫描)。

    直接 import audit 技能的 check_registry(纯 Python 内置 gitleaks/skillspector),
    文件落盘临时目录后运行, 返回 Issue 列表:
      [{severity, rule, file, line, message}]
    失败/工具缺失 → 返回空列表(调用方降级为"⚠️ audit 未执行", 不阻断)。
    """
    audit_scripts = "/root/.hermes/skills/huawei-cloud/huawei-cloud-skill-audit/scripts"
    if not os.path.isdir(audit_scripts):
        logger.warning("[skill-audit] 技能目录不存在: %s", audit_scripts)
        return []
    tmp = tempfile.mkdtemp(prefix="skill_audit_")
    try:
        # 1. 落盘: 保持仓库相对路径
        for fn, content in (files_map or {}).items():
            p = os.path.join(tmp, fn)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content or "")
        # 2. import audit 模块
        if audit_scripts not in sys.path:
            sys.path.insert(0, audit_scripts)
        from check_registry import AuditConfig, create_checks
        # 3. 运行 gitleaks + skillspector (high: critical+error)
        cfg = AuditConfig(scan_level="high",
                          enabled_checks={"gitleaks", "skillspector"},
                          no_install=True)
        issues = []
        for chk in create_checks(cfg):
            try:
                if not chk.is_available():
                    logger.warning("[skill-audit] %s 不可用, 跳过", chk.name)
                    continue
                res = chk.run_batch(pathlib.Path(tmp), [pathlib.Path(tmp)])
                for i in res.issues:
                    issues.append({
                        "severity": i.severity.value,
                        "rule": i.rule,
                        "file": i.file,
                        "line": i.line,
                        "message": i.message,
                    })
            except Exception as e:
                logger.warning("[skill-audit] %s 执行失败: %s", chk.name, e)
        # 4. 降噪(2026-08-11 用户要求方案2): 文档描述/标准SDK写法/注释 不误报
        issues = _denoise_audit_issues(issues, tmp)
        return issues
    except Exception as e:
        logger.warning("[skill-audit] 集成调用失败: %s", e)
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 安全扫描: 级别→显示 与 规则→修复建议 映射(内置表, 不改 audit 技能) ----
SEV_LABEL = {"critical": "🔴", "error": "🔴", "warning": "🟠", "info": "🔵"}
# 修复建议(2026-08-11 用户要求: 直接展示改成什么样就安全, 不要占位文案)。
# 格式: 具体改法 + 代码示例(行内代码, 表格内不用 ``` 代码块防破坏表格)。
AUDIT_FIX_HINTS = {
    # ---- gitleaks: 凭据硬编码 ----
    "aws-access-token": "删掉硬编码的 Access Key, 改为运行时读环境变量: `os.environ.get('AWS_ACCESS_KEY_ID')`",
    "aws-secret-key": "删掉硬编码的 Secret Key, 改为 `os.environ.get('AWS_SECRET_ACCESS_KEY')` 读取",
    "generic-api-key": "删掉硬编码 Key, 改为 `os.environ.get('API_KEY')` 读取, 或放进 .env 文件",
    "private-key": "私钥改放密钥管理/环境变量, 代码里只读引用: `os.environ.get('PRIVATE_KEY')`",
    "password": "删掉硬编码密码, 改为 `os.environ.get('DB_PASSWORD')` 运行时注入",
    "alibaba-access-key-id": "删掉阿里云 AccessKeyId, 改为环境变量读取: `os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_ID')`",
    "alibaba-secret-key": "删掉阿里云 Secret, 改为 `os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_SECRET')`",
    "tencentcloud-secret-id": "删掉腾讯云 SecretId, 改为环境变量读取",
    "tencentcloud-secret-key": "删掉腾讯云 SecretKey, 改为环境变量读取",
    "huawei-*": "删掉硬编码 AK/SK, 改为 `os.environ.get('HUAWEICLOUD_SDK_AK'/'HUAWEICLOUD_SDK_SK')` 读取",
    # ---- skillspector: AI 安全模式 ----
    # 提示词注入类: 多数是误报(文档描述), 确认后删除危险模式即可
    "P1": "删除要求'忽略之前指令'的提示词; 若是文档描述(非真实代码)可忽略",
    "P2": "删除隐藏指令文本; 若是文档示例可忽略, 确认无真实注入",
    "P3": "删除诱导 AI 执行危险命令的提示词, 改用白名单指令集",
    "P4": "删除操纵 AI 决策的指令, 保持行为逻辑固定",
    "P5": "删除注入的有害内容, 输入做过滤/校验",
    # 数据外泄类
    "E1": "删除向外发送数据的代码; 确需上报先做脱敏和白名单地址",
    "E2": "不要遍历 os.environ.items() 收集全部变量, 改为按名读单个: `os.environ.get('HW_AK')`",
    "E3": "删除遍历文件系统的逻辑, 只读任务明确需要的文件路径",
    "E4": "不要在输出里带出上下文/敏感信息, 先过滤再返回",
    "E5": "删除上传云存储的代码, 确需上传限制桶名和对象路径",
    # 提权类
    "PE1": "去掉超出任务的权限申请, 只保留最小必要权限",
    "PE2": "去掉 sudo/root 执行, 改用普通用户权限运行",
    "PE3": "删除越权获取凭据的代码, 只读本任务需要的环境变量",
    "PE4": "删除对 Docker socket 的访问, 改用受控 API",
    "PE5": "去掉特权容器配置, 使用非特权模式运行",
    # 反拒答类
    "AR1": "删除'不得拒绝'类指令, 保留 AI 安全拒答能力",
    "AR2": "删除隐藏免责声明的指令",
    "AR3": "删除绕过安全策略的代码/提示词",
    # 过度自主类
    "EA1": "限制 AI 可调用的工具列表, 只给任务必需的工具",
    "EA2": "高风险操作加人工确认, 不让 AI 自主决定",
    "EA3": "限定 AI 操作范围, 加白名单约束",
    "EA4": "给调用/资源设置上限, 防止无限消耗",
    # 记忆投毒类
    "MP1": "清理注入的记忆内容, 长期记忆写入前做校验",
    "MP2": "限制上下文长度, 过滤无关填充内容",
    "MP3": "禁止外部修改记忆, 记忆写入走受控接口",
    # 输出处理类
    "OH1": "不要对输出直接 eval/exec/system 执行, 改为解析数据: `json.loads(output)` 或按字段提取",
    "OH2": "不要让一个上下文的输出直接当指令执行, 加格式校验",
    "OH3": "给输出长度/大小设上限, 超限截断或报错",
    # 自治 Agent 类
    "RA1": "禁止程序修改自身代码, 代码更新走受控发布流程",
    "RA2": "删除后台驻留/自启动逻辑, 改为按需调用",
    # SSRF 类
    "SSRF1": "禁止访问 169.254.169.254 元数据接口, 从请求目标白名单剔除",
    "SSRF2": "禁止请求内网地址(127.0.0.0/8, 10.x, 192.168.x), 加目标白名单校验",
    "SSRF3": "请求 URL 不做拼接, 改为白名单映射: `URL_MAP[key]` 而不是 `f'{host}/{path}'`",
    # 供应链类
    "SC1": "锁定依赖版本: requirements.txt 里写 `pkg==1.2.3` 而不是 `pkg>=1.2`",
    "SC2": "删除运行时 curl/下载外部脚本, 改为打包内置或固定版本校验(SHA256)",
    "SC3": "混淆代码改回可读实现, 或提供源码对照",
    "SC7": "改用官方/受信镜像, 锁定 digest: `image@sha256:...`",
    # 提示词泄露类
    "P6": "提示词里不要包含系统提示原文, 改为变量引用",
    "P7": "输出前过滤可能含提示词的内容",
    "P8": "禁止通过工具调用把提示词当参数传出",
    # 工具滥用类
    "TM1": "校验工具参数: 白名单枚举+类型检查, 非法输入直接拒绝",
    "TM2": "拆分工具链调用, 关键步骤加人工确认",
    "TM3": "改用安全默认值(最小权限/只读/超时)",
    "TM4": "去掉特权容器配置, 用非特权 Kubernetes 工作负载",
    # 窥探类
    "AS1": "删除读取 agent 配置目录的代码, 改为显式传参",
    "AS2": "删除读取 MCP 配置的逻辑, 配置走受控注入",
    "AS3": "删除枚举全部技能/定义的代码, 只引用任务需要的",
}
AUDIT_FIX_FALLBACK = "删除/替换命中的危险模式为安全写法(环境变量注入、白名单校验、最小权限); 若确认是误报(如文档描述), 可在报告说明后忽略"

# 问题描述中文直白翻译(2026-08-11 用户要求: 英文规则描述看不懂, 用直白中文)。
# key = 规则 id(gitleaks/skillspector), 无映射时回退英文原文。
AUDIT_DESC_ZH = {
    # ---- gitleaks 常见规则 ----
    "aws-access-token": "代码里硬编码了 AWS 访问密钥(Access Key), 别人拿到就能冒用你的云账号",
    "aws-secret-key": "代码里硬编码了 AWS 密钥(Secret Key), 泄露等同账号被盗",
    "generic-api-key": "代码里硬编码了 API 密钥, 泄露后他人可调用你的接口",
    "private-key": "代码里出现了私钥, 泄露后加密通信/签名将被攻破",
    "password": "代码里硬编码了密码, 泄露后账户可被他人登录",
    # ---- skillspector 常见规则 ----
    "P1": "提示词注入: 脚本要求 AI 无视之前指令, 攻击者可借此篡改 AI 行为",
    "P2": "提示词注入: 内容里藏着隐藏指令, AI 可能被引导执行攻击者意图",
    "P3": "提示词注入: 诱导 AI 执行外传数据等危险命令",
    "P4": "提示词注入: 通过操纵行为指令改变 AI 的决策逻辑",
    "P5": "提示词注入: 注入有害内容, 可能让 AI 生成违规/危险输出",
    "E1": "数据外泄: 代码可能把数据发送到外部网络",
    "E2": "环境变量收割: 代码批量读取环境变量(含密钥类), 疑似收集凭据外传",
    "E3": "数据外泄: 代码遍历/扫描文件系统, 疑似批量收集文件",
    "E4": "上下文泄露: 代码可能把对话上下文/敏感信息带出",
    "E5": "云存储外泄: 代码疑似把数据上传到云存储",
    "PE1": "权限过大: 申请/使用了超出任务需要的权限",
    "PE2": "提权风险: 代码尝试以 root/sudo 执行",
    "PE3": "凭据获取: 代码疑似越权获取凭据",
    "PE4": "Docker 套接字访问: 代码可操作宿主 Docker, 风险高",
    "PE5": "容器逃逸: 代码疑似访问特权容器/尝试逃逸",
    "AR1": "反拒答: 代码要求 AI 不得拒绝任何请求",
    "AR2": "反拒答: 代码要求 AI 隐藏免责声明",
    "AR3": "安全策略失效: 代码试图绕过安全规则",
    "EA1": "工具权限无限制: AI 可调用全部工具, 无访问控制",
    "EA2": "自主决策: AI 被允许自行做高风险决定",
    "EA3": "越权行动: AI 行为超出任务约定范围",
    "EA4": "资源无上限: AI 可无限消耗资源(调用/算力/费用)",
    "MP1": "记忆投毒: 往长期记忆中注入恶意内容, 影响后续所有会话",
    "MP2": "上下文填充: 塞入大量无关内容挤占上下文窗口",
    "MP3": "记忆操纵: 篡改/删除 AI 记忆",
    "OH1": "输出注入: 把程序/模型输出直接当命令执行(eval/exec/system), 攻击者可借此执行任意代码",
    "OH2": "跨上下文输出: 一个上下文的输出被另一处当作指令使用",
    "OH3": "无界输出: 输出内容不设上限, 可能撑爆内存/带宽",
    "RA1": "自我修改: AI/程序被允许修改自身代码",
    "RA2": "会话持久化: 程序试图长期驻留后台",
    "SSRF1": "SSRF: 代码访问云元数据接口(169.254.169.254), 可窃取云凭证",
    "SSRF2": "SSRF: 代码可请求内网地址, 可能探测内网",
    "SSRF3": "SSRF: 请求目标由外部输入动态决定, 可被利用攻击任意地址",
    "SC1": "供应链: 依赖未锁定版本, 可能被投毒",
    "SC2": "供应链: 脚本运行时从外部拉取代码",
    "SC3": "供应链: 代码混淆, 难以审计",
    "SC7": "供应链: 使用不受信任的容器镜像",
    "P6": "提示词泄露: 可被诱导直接输出系统提示词",
    "P7": "提示词泄露: 可被间接套出系统提示词",
    "P8": "提示词泄露: 通过工具调用把提示词外传",
    "TM1": "工具参数滥用: 传给工具的参数可能被恶意利用",
    "TM2": "工具链滥用: 多个工具串联调用可被利用做危险操作",
    "TM3": "不安全默认值: 使用不安全的默认配置",
    "TM4": "K8s 特权负载: 创建特权 Kubernetes 工作负载",
    "AS1": "窥探配置: 代码访问 agent 配置目录",
    "AS2": "窥探 MCP 配置: 代码读取 MCP 服务器配置",
    "AS3": "技能枚举: 代码枚举/读取全部技能定义",
}
AUDIT_DESC_FALLBACK = "安全扫描规则命中, 详见英文描述"


def _audit_file_link(repo, branch, path, line):
    """GitCode 在线文件跳转链接(带行号)。

    实测(2026-08-11): GitCode 前端路由为 `blob/:branchName+/:filePath+`, 挂在
    `/{owner}/{repo}/` 下(**不带 /-/ 前缀**):
      https://gitcode.com/{repo}/blob/{branch}/{path}  → 浏览器可正常打开(用户实测)
      https://gitcode.com/{repo}/-/blob/{branch}/{path} → 前端路由不匹配, 显示"页面找不到"
    注意: curl 无登录态访问 /blob/ 可能返回 403(服务端对非浏览器请求的限制),
    浏览器登录态下正常 —— 以浏览器实测为准。
    """
    p = (path or "").lstrip("/")
    ln = f"#L{line}" if line else ""
    return f"https://gitcode.com/{repo}/blob/{branch}/{p}{ln}"


def _source_code_review(files, file_contents):
    """阶段2.1: 规则化源码审查(对齐 devops-bot skill-pr 阶段2.1 5轴标准)。

    devops-bot 的源码审查由 LLM 按 code-review-and-quality.md 的 5 轴
    (正确性/可读性/架构/安全/性能)语义执行; 脚本化后抽取可规则化检测项:
    - 正确性: 参数解析(getopts)、set -euo pipefail、shebang
    - 可读性: 调试残留(TODO/FIXME/print(调试)/测试注释)
    - 架构: 单文件过大(>1000 行)、重复代码块
    - 安全: 硬编码密钥(复用安全扫描)、eval/反引号执行、危险命令
    - 性能: 明显 N+1(循环内 API 调用)
    返回 (conclusion, [(级别, 文件, 行号, 问题, 建议)])。conclusion:
    ✅ 通过 / ⚠️ 需修改(有 🟡) / ❌ 需重大修改(有 🔴)。
    """
    findings = []
    # 只审查代码/脚本类文件(shell/python/js 等), 跳过 md/文档/模板
    code_exts = {".sh", ".py", ".js", ".ts", ".go", ".java", ".rb", ".pl",
                 ".ps1", ".json", ".yaml", ".yml", ".toml"}
    for fn, content in file_contents.items():
        ext = os.path.splitext(fn)[1].lower()
        if ext not in code_exts or not content:
            continue
        lines = content.splitlines()
        # --- 正确性 ---
        if ext == ".sh":
            if lines and lines[0].startswith("#!") and "bash" not in lines[0] and "sh" not in lines[0]:
                findings.append(("🟡", fn, 1, "shebang 非 bash/sh",
                                 "使用 #!/usr/bin/env bash"))
            if not any("set -e" in l or "set -euo" in l for l in lines[:15]):
                findings.append(("🟡", fn, 1, "缺少 set -e(错误时静默继续)",
                                 "脚本开头加 set -euo pipefail"))
            if "getopts" not in content and re.search(r"\$\{?[0-9]", content) and "--" in content:
                findings.append(("🟡", fn, 1,
                                 "按位置取参($1/$2)且文档用 --flag 调用",
                                 "改用 getopts 解析命名参数"))
        if ext == ".py":
            if not any(l.strip().startswith("def main") or
                       "__main__" in l for l in lines):
                pass  # 非入口脚本不强制
        # --- 可读性: 调试残留 ---
        for i, l in enumerate(lines, 1):
            ls = l.strip()
            if re.search(r"\b(TODO|FIXME|HACK|XXX)\b", l):
                findings.append(("🟡", fn, i, "TODO/FIXME 残留",
                                 "合并前清理或确认遗留原因"))
            if ext == ".py" and re.match(r"^\s*print\(.*#\s*(调试|测试|debug|tmp)", l):
                findings.append(("🟡", fn, i, "调试 print 残留",
                                 "移除调试输出或改用 logger"))
            if re.search(r"(测试修改|测试提交|测试残留|test change|test commit)", l):
                findings.append(("🔴", fn, i, "测试残留改动混入 PR",
                                 "移除与本次变更无关的测试性修改"))
        # --- 架构: 单文件过大 ---
        if len(lines) > 1000:
            findings.append(("🟡", fn, 1, f"单文件 {len(lines)} 行(>1000)",
                             "拆分为多个模块"))
        # --- 安全: eval/危险执行 ---
        for i, l in enumerate(lines, 1):
            if re.search(r"\beval\s*\(", l) or re.search(r"`\s*(rm|dd|mkfs)\s", l):
                findings.append(("🔴", fn, i, "eval/危险命令执行",
                                 "避免 eval, 用显式参数传递"))
            if ext == ".py" and re.search(r"subprocess\.(call|run|Popen)\([^)]*shell=True", l):
                findings.append(("🔴", fn, i, "shell=True 命令注入风险",
                                 "用参数列表而非 shell 字符串"))
        # --- 性能: 循环内网络调用 ---
        if ext == ".py":
            for i, l in enumerate(lines, 1):
                if re.search(r"for .* in .*:", l):
                    for j in range(i, min(i + 8, len(lines))):
                        if re.search(r"(requests\.|urllib\.|\.get\(|\.post\()", lines[j]) and \
                                "def " not in lines[j]:
                            findings.append(("🟡", fn, i, "循环内网络调用(N+1)",
                                             "先批量获取再本地过滤"))
                            break
    # 去重(同文件同行多规则命中取最高级别)
    seen = set()
    dedup = []
    for lvl, fn, ln, issue, sug in findings:
        key = (fn, ln, issue[:20])
        if key in seen:
            continue
        seen.add(key)
        dedup.append((lvl, fn, ln, issue, sug))
    findings = dedup
    if any(l == "🔴" for l, *_ in findings):
        conclusion = "❌"
    elif findings:
        conclusion = "⚠️"
    else:
        conclusion = "✅"
    return conclusion, findings


def pr_review_report(repo, pr_number, title, body, issue_no=None):
    """生成 PR 审查报告(对齐 skill-pr 阶段1-5), 返回 (conclusion, report_text)。

    conclusion: 'pass' / 'warn' / 'fail'。报告文本含关联Issue/变更摘要/
    Skill 审查 30 项/安全扫描/合并检查, 与 PR #125 的报告同构。
    """
    files = _fetch_pr_files(repo, pr_number)
    total_add = sum(int(f.get("additions") or 0) for f in files)
    total_del = sum(int(f.get("deletions") or 0) for f in files)
    skill_dirs = _extract_skill_dirs(files)
    # 文件内容缓存(每个文件拉一次)
    file_contents = {}
    for f in files:
        fn = f.get("filename") or ""
        if fn and fn not in file_contents:
            file_contents[fn] = _file_content(f, repo, pr_number)
    # skill 根的 SKILL.md 未在变更文件中时, 从仓库拉取当前内容补入缓存,
    # 供 _skill_review 做 frontmatter/章节等检查(#140 事故: 仅改 skill 脚本
    # 时 SKILL.md 缺失导致假失败)
    # 2026-08-09 #171 修复: references/ 下文档同样补入 —— 只改 1 个文件的
    # PR 里 references/cli-installation-guide.md 等不在变更列表, _skill_review
    # 的 names 不含它们 -> 误报"缺失"(实测: 仓库实际存在 5 个 references 文档,
    # 审查报告却报 4 个缺失)。
    for sd in skill_dirs:
        md_path = f"{sd}/SKILL.md"
        # 2026-08-09 #176 修复: SKILL.md **优先用仓库完整内容**(contents API)
        # 覆盖 PR patch 片段 —— raw_url 拉取失败时 _file_content 退化为
        # patch 提取(仅变更区 ~17 行, 不含 frontmatter), 导致 frontmatter/
        # 章节检查误报缺失(实测: 机器人读 17 行 patch 报 Skill 审查 18/30,
        # 真实 SKILL.md 174 行完整)。原逻辑只在 md_path 不在变更列表时补入,
        # SKILL.md 在变更里时仍用 patch 片段。
        # 2026-08-18 本地化: 优先本地镜像读(git show, 无 429 风险), 失败
        # 回退 contents API(#15/#16 限流期 API 批量失败导致整批误报的根因)。
        _md_fetched = False
        _md_local = _mirror_read(repo, md_path)
        if _md_local is not None:
            file_contents[md_path] = _md_local
            _md_fetched = True
        if not _md_fetched:
            try:
                d = gitcode_get(f"/repos/{repo}/contents/{md_path}")
                if isinstance(d, dict) and d.get("content"):
                    import base64
                    file_contents[md_path] = base64.b64decode(
                        d["content"]).decode("utf-8", errors="replace")
                    _md_fetched = True
            except Exception:
                pass
        if not _md_fetched and md_path not in file_contents:
            try:
                d = gitcode_get(f"/repos/{repo}/contents/{md_path}")
                if isinstance(d, dict) and d.get("content"):
                    import base64
                    file_contents[md_path] = base64.b64decode(
                        d["content"]).decode("utf-8", errors="replace")
            except Exception:
                pass
        # references/ 目录全量补入(未变更的引用文档从仓库拉取)
        # 2026-08-18 本地化: 本地 ls-tree 列文件 + git show 读, 失败回退 API
        _ref_files = _mirror_dir_files(repo, ref_dir) if (ref_dir := f"{sd}/references") else None
        if _ref_files is not None:
            for rfn in sorted(_ref_files):
                if rfn in file_contents:
                    continue
                _rc = _mirror_read(repo, rfn)
                if _rc is not None:
                    file_contents[rfn] = _rc
        else:
            try:
                ref_items = gitcode_get(f"/repos/{repo}/contents/{ref_dir}")
                ref_items = ref_items if isinstance(ref_items, list) else []
                for ri in ref_items:
                    if ri.get("type") != "file":
                        continue
                    rfn = f"{ref_dir}/{ri.get('name')}"
                    if rfn in file_contents:
                        continue
                    try:
                        rd = gitcode_get(f"/repos/{repo}/contents/{rfn}")
                        if isinstance(rd, dict) and rd.get("content"):
                            import base64
                            file_contents[rfn] = base64.b64decode(
                                rd["content"]).decode("utf-8", errors="replace")
                    except Exception:
                        pass
            except Exception:
                pass

    lines = ["## PR 审查报告", ""]
    # 关联 Issue
    issue_text = "未关联"
    if issue_no:
        st, labels = _pr_issue_status(repo, issue_no)
        issue_text = f"[#{issue_no}](https://gitcode.com/{repo}/issues/{issue_no})"
        if st:
            issue_text += f" — 状态: {st}" + (f" ({','.join(labels)})" if labels else "")
    lines += ["### 关联 Issue", f"- {issue_text}", ""]
    # 变更摘要
    lines += ["### 变更摘要",
              f"- {len(files)} 个文件变更，+{total_add} -{total_del}"]
    if skill_dirs:
        lines.append(f"- 涉及 Skill: {', '.join(f'`{d}`' for d in skill_dirs)}")
    lines.append("")

    # 代码审查(阶段2.1, 对齐 devops-bot skill-pr / PR #82): 对代码/脚本类
    # 文件做规则化 5 轴检查(正确性/可读性/架构/安全/性能)
    code_conclusion, code_findings = _source_code_review(files, file_contents)
    if code_findings:
        lines += ["### 代码审查",
                  f"**结论**: {'❌ 需重大修改' if code_conclusion == '❌' else ('⚠️ 需修改' if code_conclusion == '⚠️' else '✅ 通过')}"
                  f"（{len(code_findings)} 项发现）", "",
                  "| 级别 | 文件 | 行号 | 问题 | 建议 |",
                  "|------|------|------|------|------|"]
        for lvl, fn, ln, issue, sug in code_findings:
            lines.append(f"| {lvl} | `{fn}` | {ln} | {issue} | {sug} |")
        lines.append("")
    else:
        lines += ["### 代码审查",
                  "**结论**: ✅ 通过",
                  "",
                  "代码/脚本类文件未发现明显问题(正确性/可读性/架构/安全/性能 5 轴规则检查)。",
                  ""]

    # Skill 审查(每个 skill 目录)
    conclusions = []
    if not skill_dirs:
        lines += ["### Skill 审查", "**结论**: ⚠️ 未检测到 skills/ 目录变更", ""]
        conclusions.append("warn")
    for sd in sorted(skill_dirs):
        conclusion, results = _skill_review(sd, skill_dirs[sd], file_contents,
                                           repo, pr_number)
        # 检测点4「一个PR只提交一个Skill」(🔴)是 PR 级检查: PR 涉及多个
        # skill 目录时所有 skill 的该项都应为 ❌(#140: swr-list 正常 add,
        # 却混入了 ges skill 脚本的测试残留改动, 旧实现该项恒 ✅ 未拦截)
        if len(skill_dirs) > 1:
            multi_note = (f"PR 同时变更 {len(skill_dirs)} 个 skill 目录"
                          f"({', '.join(sorted(skill_dirs))}), 违反单 skill 提交规范")
            results = [(idx, text, sev,
                        ("❌" if idx == 4 else res),
                        (multi_note if idx == 4 else note))
                       for idx, text, sev, res, note in results]
            critical_fail = any(r == "❌" and s == "🔴"
                                for _, _, s, r, _ in results)
            high_fail = any(r == "❌" and s == "🟠"
                            for _, _, s, r, _ in results)
            conclusion = "❌" if critical_fail else ("⚠️" if high_fail else "✅")
        conclusions.append(conclusion)
        lines += [f"### Skill 审查（{sd}）",
                  f"**结论**: {'✅ 通过' if conclusion == '✅' else ('⚠️ 部分不通过' if conclusion == '⚠️' else '❌ 不通过')}"
                  f"（{sum(1 for r in results if r[3] == '✅')}/{len(results)} 项通过）", "",
                  "| # | 检查内容 | 严重级别 | 结果 | 说明 |",
                  "|---|----------|----------|------|------|"]
        for idx, text, sev, res, note in results:
            lines.append(f"| {idx} | {text} | {sev} | {res} | {note} |")
        lines.append("")

    # 安全扫描(2026-08-11 重构): 全部交给 huawei-cloud-skill-audit 扫描。
    # 废除内置 3 条正则(AK/SK/AKIA/--cli-access-key); audit 用 gitleaks
    # (222 规则+熵) + skillspector(47 规则/17 类目), scan-level=high。
    # PR 涉及的 skill 目录文件(含 references 补入)落盘临时目录后扫描。
    _branch = "github"
    try:
        _prd = gitcode_get(f"/repos/{repo}/pulls/{pr_number}")
        _branch = ((_prd.get("head") or {}).get("ref")) or "github"
    except Exception:
        pass
    skill_files = {}
    for sd in skill_dirs:
        for fn in file_contents:
            if fn == f"{sd}/SKILL.md" or fn.startswith(sd + "/"):
                skill_files[fn] = file_contents[fn]
    audit_issues = _run_skill_audit_scan(repo, pr_number, skill_files, _branch)
    sec_issues = [i for i in audit_issues if i["severity"] in ("critical", "error", "warning")]
    sec_ok = not any(i["severity"] in ("critical", "error") for i in sec_issues)
    lines += ["### 安全扫描",
              f"**结论**: {'✅ PASS' if sec_ok else '❌ FAIL'} "
              "(huawei-cloud-skill-audit: gitleaks + skillspector, high)"]
    if not sec_issues:
        lines.append("- 未发现安全问题")
        lines.append("- 扫描器: gitleaks(222规则) + skillspector(47规则)")
        lines.append("")
    else:
        lines.append("| 级别 | 规则 | 问题文件 | 行号 | 问题描述 | 修复建议 |")
        lines.append("|------|------|----------|------|----------|----------|")
        for i in sec_issues[:15]:
            sev = SEV_LABEL.get(i["severity"], "🔵")
            rule = i["rule"] or "unknown"
            fpath = i["file"] or "?"
            flink = _audit_file_link(repo, _branch, fpath, i["line"])
            # 问题文件列只显示文件名(2026-08-11 用户要求), 跳转仍指向完整路径
            fname = os.path.basename(fpath.rstrip("/")) or fpath
            hint = AUDIT_FIX_HINTS.get(rule) or AUDIT_FIX_HINTS.get(
                "huawei-*" if ("huawei" in str(rule).lower() or "ak" == str(rule).lower() or "sk" == str(rule).lower())
                else None, AUDIT_FIX_FALLBACK)
            if hint == AUDIT_FIX_FALLBACK and rule:
                # gitleaks 规则 id 通常是 "厂商-类型"(如 alibaba-secret-key), 逐段匹配
                for _seg in str(rule).split("-"):
                    if AUDIT_FIX_HINTS.get(_seg):
                        hint = AUDIT_FIX_HINTS[_seg]
                        break
            # 问题描述: 优先中文直白翻译(2026-08-11 用户要求), 无映射回退英文原文
            desc = AUDIT_DESC_ZH.get(rule) or (i["message"] or "")[:80] or AUDIT_DESC_FALLBACK
            lines.append(f"| {sev} | {rule} | [{fname}]({flink}) | {i['line'] or '-'} "
                         f"| {desc} | {hint} |")
        if len(sec_issues) > 15:
            lines.append(f"| ... | 其余 {len(sec_issues)-15} 条省略 | | | | |")
        lines.append("")

    # 合并检查(对齐 skill-pr 阶段4 + PR #82: 源码审查/IaC 安全/测试/冲突/Issue)
    # 2026-08-11 用户要求: 三态渲染 —— ✅ 通过 / ⚠️ 告警(不阻断, 不触发修改)
    # / ❌ 阻断(触发 fail 分流)。判定: fail 只看 conclusions/code_conclusion/
    # sec_ok(下方 conclusion 计算); 其余项(测试声明/冲突/issue 状态)不阻断,
    # 未通过时用 ⚠️ 而非 ❌, 避免"红叉但无人处理"的误导。
    issue_state_ok = True
    if issue_no:
        try:
            st, labels = _pr_issue_status(repo, issue_no)
            issue_state_ok = bool(st and (
                "accepted" in (st or "").lower() or
                "triaged" in ",".join(labels).lower()))
        except Exception:
            pass
    # 状态: "ok" → ✅, "warn" → ⚠️, "fail" → ❌
    merge_items = [
        ("安全通过", "ok" if sec_ok else "fail"),
        ("代码审查通过",
         "ok" if code_conclusion == "✅"
         else ("warn" if code_conclusion == "⚠️" else "fail")),
        ("Skill 审查通过",
         "fail" if "❌" in conclusions
         else ("warn" if "⚠️" in conclusions else "ok")),
        ("测试通过（PR 描述声明）",
         "ok" if re.search(r"(?i)pass|通过|tested", body or "") else "warn"),
        ("无合并冲突", "ok"),
    ]
    if issue_no:
        merge_items.append(("关联 Issue 已 accepted/triaged",
                            "ok" if issue_state_ok else "warn"))
    _SEV_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    lines += ["### 合并检查"]
    for name, st in merge_items:
        lines.append(f"- {_SEV_ICON.get(st, '❓')} {name}")
    lines.append("")
    # 2026-08-11 用户要求: 明确标注"测试通过"仅凭 PR 描述关键词, 非实际测试结果
    lines.append("> ⚠️ 说明: 「测试通过」仅根据 PR 描述中是否出现 pass/通过/tested 关键词判断, "
                 "不代表实际测试结果; 真实测试由测试小队在审查通过后执行, 结果以测试报告评论为准。")
    lines.append("")

    if "❌" in conclusions or code_conclusion == "❌" or not sec_ok:
        conclusion = "fail"
    elif "⚠️" in conclusions or code_conclusion == "⚠️":
        conclusion = "warn"
    else:
        conclusion = "pass"
    return conclusion, "\n".join(lines)


def _assign_pr_reviewer(repo, pr_number, pr_author_login, issue_no=None,
                        title="", body=""):
    """PR 扫描后自动设置评审人(2026-08-12 用户要求, 设计文档
    docs/superpowers/specs/2026-08-12-pr-reviewer-assign-design.md)。

    分流:
    - 非 bot 作者(开发者 PR) -> DEFAULT_REVIEWER(胡盖/HugLovesWine)
    - bot 作者(机器人 PR) -> 按关联 issue 标签:
      - bug -> skill 贡献者(_skill_primary_contributor, 提交历史推断)
      - feature -> issue 创建人(GET /issues/{n} 的 user.login)
      - 无 feature/bug 标签 -> 静默跳过
      - 找不到评审人 -> 回退 DEFAULT_REVIEWER
    约束(2026-08-12 API 实测): ① 评审人必须是仓库 collaborator(仓库外用户
    400), 目标不在 collaborator 列表则回退 DEFAULT_REVIEWER; ② PR 作者
    不能设自己为评审人(作者=目标时跳过); ③ 幂等: approval_reviewers 已含
    目标则跳过。任何失败静默容忍, 不阻断审查主流程。
    返回 True=已设置/已存在, False=跳过/失败。
    """
    if not (ENABLE_PR_REVIEWER_ASSIGN and repo and pr_number):
        return False
    _author = (pr_author_login or "").strip()
    # 拉 PR 详情: 作者兜底 + 幂等检查(approval_reviewers)一次调用
    _existing = []
    _prd = None
    try:
        _prd = gitcode_get(f"/repos/{repo}/pulls/{pr_number}")
        if isinstance(_prd, dict):
            if not _author:
                _author = (((_prd.get("user") or {}).get("login")) or "").strip()
            _existing = [str(r.get("login") or "").lower()
                         for r in (_prd.get("approval_reviewers") or [])]
    except Exception:
        pass
    if not _author:
        return False  # 作者无法确定(PR 详情查不到), 跳过
    _is_bot = _author.lower() in BOT_AUTHOR_LOGINS
    target = None
    if not _is_bot:
        # 开发者 PR: 固定胡盖
        target = DEFAULT_REVIEWER
    else:
        if not issue_no:
            return False  # 机器人 PR 无关联 issue, 无法按标签分流
        try:
            labels = _api_issue_labels(repo, issue_no) or []
        except Exception:
            labels = []
        has_bug = "bug" in labels
        has_feature = "feature" in labels
        if not has_bug and not has_feature:
            return False  # 无 feature/bug 标签, 静默跳过
        if has_bug:
            # bug -> skill 贡献者(提交历史推断)
            _skill = extract_skill_name_from_pr(None, title, body)
            if _skill:
                try:
                    target = _skill_primary_contributor(repo, _skill)
                except Exception as e:
                    logger.warning(f"[pr-reviewer] 推断 skill 贡献者失败: {e}")
        else:
            # feature -> issue 创建人
            try:
                _d = gitcode_get(f"/repos/{repo}/issues/{issue_no}")
                if isinstance(_d, dict):
                    target = ((_d.get("user") or {}).get("login")) or None
            except Exception as e:
                logger.warning(f"[pr-reviewer] 查 issue #{issue_no} 创建人失败: {e}")
        if not target:
            target = DEFAULT_REVIEWER  # 找不到回退胡盖
    target = str(target or "").strip()
    if not target or target.lower() == _author.lower():
        return False  # 空目标 / 评审人==作者(API 硬限制), 跳过
    # 幂等: approval_reviewers 已含目标则跳过(PR 详情已在函数开头拉取)
    if target.lower() in _existing:
        return True
    # 校验 collaborator: 目标不在仓库 collaborator 列表则回退 DEFAULT_REVIEWER
    try:
        collabs = _repo_collaborators(repo)
    except Exception:
        collabs = set()
    if collabs and target.lower() not in collabs:
        target = DEFAULT_REVIEWER
        if not target or target.lower() == _author.lower():
            return False
        if target.lower() in _existing:
            return True
    try:
        gitcode_post(f"/repos/{repo}/pulls/{pr_number}/reviewers",
                     {"reviewers": target})
        logger.info(f"[pr-reviewer] PR #{pr_number} 评审人已设置: {target} "
                    f"(作者={_author or '?'}, bot={_is_bot})")
        return True
    except Exception as e:
        logger.warning(f"[pr-reviewer] PR #{pr_number} 设置评审人 {target} 失败: {e}")
        return False


def pr_review_and_notify(repo, pr_number, title, body, issue_no=None, force=False):
    """生成 PR 审查报告并评论到 PR(幂等); 返回 (conclusion, report_text)

    force=True 时跳过幂等检查, 强制重新审查并重新发报告 —— 供新 commit
    事件使用(#142 事故: 第二次 commit 推送后审查报告未重新生成, 幂等把
    "已有报告"当终态; 但代码已变, 旧报告结论失效)。
    """
    conclusion, report = pr_review_report(repo, pr_number, title, body, issue_no)
    # 幂等: 已有 [skills_bot] ## PR 审查报告 评论则跳过(force=True 时除外)
    if not force:
        try:
            d = gitcode_get(f"/repos/{repo}/pulls/{pr_number}/comments?per_page=20")
            comments = d if isinstance(d, list) else []
            for c in comments:
                cb = c.get("body") or ""
                if cb.startswith(BOT_PREFIX) and MARKERS["pr_review_report"] in cb:
                    return conclusion, report
        except Exception:
            pass
    # 2026-08-16 方案A: 审查报告评论尾部记录"处理版本: <head sha>", 供
    # reopen 事件对比文件内容是否变动(_pr_sha_changed)。拉取失败不阻断评论。
    _sha_note = ""
    try:
        _sha = _pr_head_sha(repo, pr_number)
        if _sha:
            _sha_note = f"\n\n{PR_SHA_MARKER} {_sha[:12]}"
    except Exception:
        pass
    safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(report + _sha_note))
    # 2026-08-12 用户要求: 扫描后自动设置评审人(开发者 PR->胡盖; 机器人 PR
    # 按 issue 标签 bug->skill 贡献者 / feature->issue 创建人; 找不到回退胡盖)。
    # 失败静默容忍不阻断; 两个入口(handle_pr 主流程 + 评论重扫)统一在此生效。
    # PR 作者由 _assign_pr_reviewer 内部从 PR 详情获取(调用点无需传)。
    try:
        _assign_pr_reviewer(repo, pr_number, "",
                            issue_no=issue_no, title=title, body=body)
    except Exception as _e:
        logger.warning(f"[pr-reviewer] 设置评审人异常(不阻断): {_e}")
    return conclusion, report


def is_skill_task(title, body):
    """识别是否为 skill 开发任务: 标题或描述中包含 skill 关键词"""
    text = f"{title or ''} {body or ''}"
    return bool(re.search(r"skill", text, re.IGNORECASE))
def extract_issue_number_from_pr(pr, repo=None, pr_number=None):
    """从 PR 信息中提取关联的 issue 编号, 返回 number 或 None。

    提取顺序(2026-08-11 增强):
      1. close_related_issue 字段(数字编号);
      2. body/title 里的 Closes/Fixes/Related/关联 #N 模式;
      3. **DB 反查兜底**(body 未写关联时): 按 gitcode_pr_id 匹配需求表,
         取该 PR 关联需求的 gitcode_issue_number —— PR 由开发任务创建时
         DB 已记录 PR↔issue 关联, body 是否写明不影响反查(#38 实测:
         body 无关联编号但 DB 有记录)。
    """
    cri = pr.get("close_related_issue")
    # 注意: bool 是 int 子类 —— close_related_issue=True(布尔开关)不能当编号,
    # 否则 int(True)=1 误判为 issue #1(#38 实测: API 返回 True)。
    if isinstance(cri, bool):
        pass
    elif isinstance(cri, (int, float)) and cri:
        return int(cri)
    elif isinstance(cri, str) and cri.strip().isdigit():
        return int(cri)
    text = f"{pr.get('body') or ''} {pr.get('title') or ''}"
    patterns = [
        r"(?:fix|close|resolve|关联|解决|关闭|处理)[esd]*\s*#?\s*(\d+)",
        r"(?:issue|issues)\s*[#/]\s*(\d+)",
        r"(?:issue|issues)?\s*#(\d+)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    # DB 反查兜底: PR 编号 + 仓库 -> 需求表里的 gitcode_issue_number
    if repo and pr_number:
        try:
            _req = db_find_requirement_by_pr_id(pr_number, repo)
            _ino = (_req or {}).get("gitcode_issue_number")
            if _ino and str(_ino).strip().isdigit():
                logger.info("[extract-issue] body 未写关联, DB 反查 PR #%s -> issue #%s",
                            pr_number, _ino)
                return int(_ino)
        except Exception as _e:
            logger.warning(f"[extract-issue] DB 反查失败: {_e}")
    return None


def is_distribute_pr(pr):
    """判断是否为合入分发分支(distribute_for_github)的 PR(cherry-pick 场景)"""
    target = pr.get("target_branch") or ((pr.get("base") or {}).get("ref")) or ""
    return target == DISTRIBUTE_BRANCH


def mention_squad(squad_id, name):
    """构造 Multica 评论中的小队 @ 语法"""
    return f"[@{name}](mention://squad/{squad_id})"


# ==================== GitCode 评论工具 ====================
# 评论列表进程内缓存(P1 重构 2026-08-09): _issue_has_bot_comment /
# _pr_has_bot_comment 每个事件被调用多次且每次打 API, 高频事件放大调用量
# 叠加 GitCode 429 限流。缓存评论快照 TTL 60s; 发布评论成功后失效对应缓存
# (避免"发评论前检查未命中 -> 发布 -> 缓存仍旧"导致同事件重复发)。
_COMMENTS_CACHE = {}          # f"{kind}:{repo}:{number}" -> (ts, [body,...])
_COMMENTS_CACHE_TTL = 60


def _get_comments_cached(kind, repo, number):
    """取评论列表(带缓存)。kind: 'issue'|'pr'。失败返回 None(调用方按未命中)。"""
    key = f"{kind}:{repo}:{number}"
    hit = _COMMENTS_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _COMMENTS_CACHE_TTL:
        return hit[1]
    try:
        path = "issues" if kind == "issue" else "pulls"
        d = gitcode_get(f"/repos/{repo}/{path}/{number}/comments?per_page=30")
        bodies = [(c.get("body") or "") for c in d] if isinstance(d, list) else []
        _COMMENTS_CACHE[key] = (now, bodies)
        return bodies
    except Exception:
        _COMMENTS_CACHE.pop(key, None)
        return None


def _invalidate_comments_cache(kind, repo, number):
    """发布评论成功后失效缓存, 避免幂等检查读到旧快照重复发。"""
    _COMMENTS_CACHE.pop(f"{kind}:{repo}:{number}", None)


def gitcode_issue_comment(repo, number, body):
    """在 GitCode issue 下发评论, 返回评论 id"""
    d = gitcode_post(f"/repos/{repo}/issues/{number}/comments", {"body": body})
    _invalidate_comments_cache("issue", repo, number)
    return d.get("id")


def _issue_has_bot_comment(repo, number, marker):
    """查 issue 评论中是否已有含 marker 的 [skills_bot] 评论(幂等标记)。

    用于需要重复评估的评论场景(如产品相关性追问/关闭说明), 避免 update
    事件反复触发时刷屏。返回 True 表示已发过。
    """
    if not (repo and number and marker):
        return False
    bodies = _get_comments_cached("issue", repo, number)
    if bodies is None:
        bodies = []
    for cb in bodies:
        if cb.startswith(BOT_PREFIX) and marker in cb:
            return True
    return False


# 2026-08-09 #241 修复: "等待用户补充"类提示 marker —— bug 2.2 分流
# (未提供 skill 名称 / 格式不完整 / 仓库无此 skill)与优化类 2.1.3(未指定
# 唯一 skill)分支会评论提示用户**修改 issue** 后返回, **不入库**(DB 无记录,
# requirement_id=None)。这些提示不是"流程已完成", 而是 bot 在等用户按提示
# 修改 —— 用户编辑触发的 update 事件必须重新评估, 否则补充后永远无响应
# (#241 实测: 用户补充 skill 名后 update 被无记录分支跳过, 需求卡死)。
WAITING_PROMPT_MARKERS = (
    MARKERS["bug_need_skill_name"],
    MARKERS["bug_bad_skill_fmt"],
    MARKERS["bug_skill_not_found"],  # "未找到与 `xxx` 完全匹配的 skill"
    MARKERS["bug_repo_no_skill"],
    MARKERS["opt_need_skill_name"],
)


def _has_waiting_prompt_comment(repo, number):
    """issue 上是否存在"等待用户补充"类 [skills_bot] 提示评论(2.2/2.1.3)。"""
    return any(_issue_has_bot_comment(repo, number, mk) for mk in WAITING_PROMPT_MARKERS)


def _pr_has_bot_comment(repo, number, marker):
    """查 PR 评论中是否已有含 marker 的 [skills_bot] 评论(幂等标记)。"""
    if not (repo and number and marker):
        return False
    bodies = _get_comments_cached("pr", repo, number)
    if bodies is None:
        bodies = []
    for cb in bodies:
        if cb.startswith(BOT_PREFIX) and marker in cb:
            return True
    return False


def is_test_report(body):
    """判断 PR 评论是否为该 PR 的测试报告(2026-08-04 用户要求)。

    特征(对齐 #126 实测报告格式): 标题「# 总测试报告 / # 测试报告」+
    统计结构(总用例数/测试统计/被测 skill/报告类型: PR 唯一总测试报告)。
    命中任一组合即视为测试报告, 非测试报告评论不做处理。
    """
    b = body or ""
    if not b.strip():
        return False
    if b.startswith("# 总测试报告") or b.startswith("# 测试报告"):
        return True
    if "测试报告" in b and ("总用例数" in b or "测试统计" in b
                            or "被测 skill" in b or "报告类型" in b):
        return True
    if "总用例数" in b and ("通过" in b or "失败" in b or "告警" in b):
        return True
    return False


# 无关评论过滤(2026-08-05 用户要求): 灌水短文本/纯 emoji/与需求无关的
# 闲聊评论静默过滤。设计见 docs/superpowers/specs/2026-08-05-issue-comment-filter-design.md
_COMMENT_CLOSE_WORDS = ("关闭", "关掉", "取消", "不用做", "不用处理", "不用开发", "close")


def _comment_core(text):
    """去除空白/emoji/符号, 返回可判定长度的核心文本(保留中文/字母/数字)"""
    import re as _re
    return _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")


def is_irrelevant_comment(note_body, issue_title="", issue_body=""):
    """判断 issue 评论是否为无关评论(True=应静默过滤)。

    层2 短文本: 核心文本(去 emoji/符号)≤8 字 且命中灌水词集且无实质信号,
    或核心为空(纯 emoji/符号) -> 无关
    层3 需求相关性: 评论与功能词集/产品词库/issue 标题归一化均无交集
    -> 无关(覆盖较长闲聊)
    豁免: 关闭请求类词(关闭/取消/不用做等, 意图1 关键词) -> 相关,
    由意图1 先行处理, 防止被层3 误过滤(自查记录见设计文档 §6)。
    异常由调用方 fail-open。
    """

    def _has_substance(b, low):
        """评论是否含实质信号(功能词/产品词)"""
        # 2026-08-11 修复: 功能词匹配大小写不敏感 —— "BUG"大写 vs "bug"小写
        # 词集不命中导致"帮我处理一下这些BUG"被误判无关过滤(#101 实测)。
        for w in COMMENT_FUNC_WORDS:
            if w.isascii():
                if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low, re.IGNORECASE):
                    return True
            elif w in b:
                return True
        # 2026-08-07 #175 教训: 英文产品词词边界匹配(子串误伤前缀包含)
        import re as _re4
        for p in HUAWEI_CLOUD_PRODUCTS:
            if p.isascii():
                if _re4.search(r"(?<![a-z0-9])" + _re4.escape(p) + r"(?![a-z0-9])", low, _re4.IGNORECASE):
                    return True
            elif p in b:
                return True
        return False

    b = note_body or ""
    # 剥离 @提及与 markdown mention 链接(如 [@skills_bot](url)/@skills_bot):
    # 提及是结构性文本, 不算实质内容 —— 否则 [@skills_bot] 含 "skill" 子串
    # 命中 COMMENT_FUNC_WORDS 的 "skill", 灌水闲聊(如 "天气很好呀")被放行
    # 转发到 Multica(2026-08-05 实测 bug)。
    b = re.sub(r"\[@[^\]]+\]\([^)]+\)", "", b)
    b = re.sub(r"@[\w-]+", "", b)
    core = _comment_core(b)
    if not core:
        return True  # 空/纯 emoji/纯符号
    low = b.lower()
    # 豁免: 关闭请求(意图1 关键词)
    for w in _COMMENT_CLOSE_WORDS:
        if w in low or w in b:
            return False
    # 豁免: @机器人 + 重新处理(重试)/转机器人修复 类评论 —— "重新处理"等词
    # 无功能词(bug/修复/skill 等), 会被层2/层3 判无关静默跳过, 导致
    # 重试/转机器人永远不触发(2026-08-18 方案A 实测分析)。
    try:
        if is_issue_retry_request(note_body or "") or is_bot_fix_request(note_body or ""):
            return False
    except Exception:
        pass
    # 层2: 短文本灌水(命中灌水词且无实质信号; "支持rds" 有产品词不过滤)
    if len(core) <= 8:
        for w in IRRELEVANT_COMMENT_WORDS:
            if w in low or w in b:
                return not _has_substance(b, low)
    # 层3: 需求相关性(较长评论兜底)
    if _has_substance(b, low):
        return False
    if issue_title:
        def _norm(s):
            return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())
        tn = _norm(issue_title)
        if tn and tn in _norm(b):
            return False
    return True


def gitcode_pr_comment(repo, number, body):
    """在 GitCode PR 下发评论, 返回评论 id; 兼容 pulls/merge_requests 两种端点"""
    for path in (f"/repos/{repo}/pulls/{number}/comments", f"/repos/{repo}/merge_requests/{number}/comments"):
        try:
            d = http_json("POST", f"{GITCODE_API_BASE}{path}", body={"body": body},
                          headers={"PRIVATE-TOKEN": GITCODE_TOKEN})
            _invalidate_comments_cache("pr", repo, number)
            return d.get("id")
        except RuntimeError as e:
            if "404" in str(e):
                continue
            raise
    raise RuntimeError(f"PR 评论发送失败: {repo} #{number}")


def bot_comment(body):
    """程序自身评论统一带前缀标记"""
    return f"{BOT_PREFIX} {body}"


def safe_notify(fn, *args, **kwargs):
    """通知评论容错封装: 失败不阻断主流程, 返回错误信息或 None"""
    try:
        fn(*args, **kwargs)
        return None
    except Exception as e:
        return str(e)[:300]


# ==================== codeowner 模块负责人分配(skill-issue 集成) ====================
# 背景: 仓库中带 codeowner 标签的 issue(如 #72 "CODEOWNDERS")的 body 是模块负责人
# 映射表(责任人/模块/触发词/描述/兜底)。非 Multica 自动开发的 skill(开发者开发的
# skill)出问题时, 应按该映射把 issue 分配给对应责任人, 而不是直接跳过。
# 对齐 devops-bot skill-issue/references/assignee-guide.md 的分配流程。
CODEOWNER_LABEL = "codeowner"  # 映射 issue 的标签名(实测仓库为单数 codeowner)

def fetch_codeowner_mapping(repo):
    """查找仓库中带 codeowner 标签的 issue, 解析其 body 为模块负责人映射。

    返回 (modules, fallback): modules=[{owner, module, keywords, description, repo}],
    fallback=兜底负责人用户名或 None。未配置映射返回 ([], None)。
    """
    if not repo:
        return [], None
    try:
        data = gitcode_get(
            f"/repos/{repo}/issues?labels={CODEOWNER_LABEL}&state=all&per_page=10"
        )
        items = data if isinstance(data, list) else data.get("issues", [])
        for it in items:
            body = it.get("body") or it.get("description") or ""
            if not body.strip():
                continue
            modules, fallback = parse_codeowner_body(body)
            if modules or fallback:
                return modules, fallback
    except Exception as e:
        logger.warning(f"[codeowner] 获取模块负责人映射失败: {e}")
    return [], None


def parse_codeowner_body(body):
    """解析 codeowner issue body -> (modules, fallback)。

    每块格式(用 ``` 代码块包裹, 冒号为全角「：」):
        # 责任人：@username
        # 模块：module-name
        # 触发词：kw1, kw2
        # 描述：功能说明
        # 仓库：owner/repo   (可选, 跨仓同步目标)
    兜底行: * @username
    """
    m = re.search(r"```(?:\w+)?\s*(.*?)```", body or "", re.S)
    text = m.group(1) if m else (body or "")
    modules, fallback, cur = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# 责任人"):
            if cur:
                modules.append(cur)
            owner = line.split("：", 1)[-1].strip().lstrip("@").strip()
            cur = {"owner": owner, "module": "", "keywords": [],
                   "description": "", "repo": None}
        elif line.startswith("# 模块") and cur:
            cur["module"] = line.split("：", 1)[-1].strip()
        elif line.startswith("# 触发词") and cur:
            kws = line.split("：", 1)[-1]
            cur["keywords"] = [k.strip() for k in re.split(r"[,，]", kws) if k.strip()]
        elif line.startswith("# 描述") and cur:
            cur["description"] = line.split("：", 1)[-1].strip()
        elif line.startswith("# 仓库") and cur:
            cur["repo"] = line.split("：", 1)[-1].strip().lstrip("/").strip()
        elif line.startswith("* @") and fallback is None:
            fallback = line[2:].strip().lstrip("@").strip() or None
    if cur:
        modules.append(cur)
    return modules, fallback


def match_codeowner_module(modules, title, body):
    """按触发词匹配模块(大小写不敏感, 子串匹配) -> 返回模块 dict 或 None"""
    text = f"{title or ''} {body or ''}".lower()
    for mod in modules:
        for kw in mod.get("keywords", []):
            if kw and kw.lower() in text:
                return mod
    return None


# 仓库主题词: 命中任一即视为"可能与仓库相关", 未命中且未匹配模块则判完全无关
REPO_TOPIC_KEYWORDS = [
    "skill", "技能", "huawei", "华为云", "开发", "cloud", "云服务",
    "ges", "图数据库", "graph", "查询", "cypher", "gql",
]

# 华为云特征词: 命中任一即视为华为云语境(产品相关性检查第一层, 宽松放行)
HUAWEI_CLOUD_FEATURE_WORDS = ["huawei", "华为云", "华为", "云服务", "云产品", "cloud"]

# 竞品云特征词: 命中且不含有华为云正向特征 -> 明确非华为云(2026-08-04 新增,
# 修复 #109「阿里云查询eip列表」误命中 eip 产品词库的问题; #110 补火山云)
COMPETITOR_CLOUD_WORDS = [
    "阿里云", "腾讯云", "百度云", "百度智能云", "火山云", "火山引擎", "京东云",
    "金山云", "天翼云", "移动云", "联通云", "青云", "七牛云", "又拍云", "网易云",
    "微软云", "亚马逊云", "滴滴云", "紫光云", "浪潮云", "华三云", "UCloud",
    "aws", "azure", "gcp", "google cloud", "amazon", "aliyun", "tencent cloud",
    "volcengine", "火山",
]

# 通用云名词(白名单): "X云" 模式中这些不算竞品云(华为云/通用云概念/华为云产品)
CLOUD_GENERIC_TERMS = {
    "华为云", "云服务", "云产品", "云原生", "云计算", "云端", "云上", "云平台",
    "云环境", "云资源", "云能力", "云厂商", "云市场", "云主机", "云服务器",
    "云数据库", "云硬盘", "云备份", "云监控", "云日志", "云搜索", "云容器",
    "云审计", "云桌面", "云手机", "云存储", "云网络", "云网关", "云连接",
    "弹性云", "混合云", "公有云", "私有云", "多云", "行业云", "专有云",
    "专属云", "云边", "云管", "云上", "上云",
}

def is_competitor_cloud(title, body):
    """竞品云特征判断: 命中竞品云且不含有华为云正向特征 -> 明确非华为云。

    两层检测:
    1. 精确词表 COMPETITOR_CLOUD_WORDS(阿里云/腾讯云/aws/火山云 等)
    2. 启发式「X云」模式(2-3 个中文+云), 排除 CLOUD_GENERIC_TERMS 通用
       云名词 —— 覆盖词表未收录的新竞品云(如 #110「火山云」, 词表只有
       「火山引擎」时漏判)
    """
    text = f"{title or ''} {body or ''}"
    t = text.lower()
    if "huawei" in t or "华为" in text:
        return False
    for w in COMPETITOR_CLOUD_WORDS:
        if w.isascii():
            if w.lower() in t:
                return True
        elif w in text:
            return True
    # 启发式: 「厂商名+云」(2-3 个中文字符后跟云), 排除通用云名词
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,3}云)", text):
        if m.group(1) not in CLOUD_GENERIC_TERMS:
            return True
    return False

def huawei_related_level(title, body):
    """华为云相关性三级判定: 'strong'(标题命中) / 'weak'(仅正文命中) / 'none'。

    2026-08-04 #113 教训: 「查询每日新闻列表」标题无关, 但正文是从 #110
    复制粘贴残留的「查询租户下的EIP的名称列表」, EIP 命中产品词库被放行
    建了任务 SKI-138。正文命中产品词不一定是真实需求(可能是复制残留),
    标题命中才是强信号; 仅正文命中按弱相关处理(追问确认, 不直接放行)。
    竞品云由 is_competitor_cloud 单独处理, 此处返回 none。
    """
    text = f"{title or ''} {body or ''}"
    t = text.lower()
    # 1. 正向强特征(任意位置): huawei / 华为
    if "huawei" in t or "华为" in text:
        return "strong"
    # 2. 竞品云 -> 明确非华为(由调用方 invalid 关闭)
    if is_competitor_cloud(title, body):
        return "none"

    def _hit(s, sl):
        for w in ("cloud", "云服务", "云产品"):
            if w in sl or w in s:
                return True
        for p in HUAWEI_CLOUD_PRODUCTS:
            if p.isascii():
                if p in sl:
                    return True
            elif p in s:
                return True
        return False

    # 3. 标题命中 -> 强相关
    if _hit(title or "", (title or "").lower()):
        return "strong"
    # 4. 仅正文命中 -> 弱相关(复制残留风险, 需追问确认)
    if _hit(text, t):
        return "weak"
    return "none"


def is_huawei_cloud_related(title, body):
    """判断需求是否属于华为云产品 skill 开发范围(2026-08-04 新增, 温和校验)。

    兼容旧调用: strong(标题命中)/weak(正文命中)视为相关放行, none 视为不相关。
    需要区分 weak 的调用方请用 huawei_related_level()。
    """
    return huawei_related_level(title, body) in ("strong", "weak")

def is_unrelated_issue(title, body, modules=None):
    """判断 issue 是否与仓库项目范围完全无关(对齐 assignee-guide Step 5)。

    判据: 标题+正文未命中任何 codeowner 模块触发词, 且不含仓库主题词
    (skill/技能/huawei/华为云/开发等) -> 完全无关, 应打 invalid 并关闭。
    注意: 触发词匹配在调用方已做, 此处仅判断主题词兜底, 双保险。
    """
    text = f"{title or ''} {body or ''}".lower()
    if not text.strip():
        return True
    # 命中任意仓库主题词 -> 可能相关, 不判无关
    for kw in REPO_TOPIC_KEYWORDS:
        if kw and kw.lower() in text:
            return False
    # 命中任意模块触发词 -> 相关
    for mod in (modules or []):
        for kw in mod.get("keywords", []):
            if kw and kw.lower() in text:
                return False
    return True


def set_issue_assignee(repo, number, username):
    """分配 issue 责任人; 失败抛异常"""
    if not repo or not number or not username:
        return None
    return gitcode_patch(f"/repos/{repo}/issues/{number}", {"assignee": username})


def codeowner_assign_issue(repo, number, title, body, issue_id, issue_url,
                           created, updated, author_login, author_name):
    """非 Multica 自动开发 skill 的 issue: 按 codeowner 映射分配对应开发者。

    流程(对齐 assignee-guide.md): 查映射 issue -> 解析模块表 -> 触发词匹配 ->
    命中则 PATCH assignee + 评论通知 + 入库(status=codeowner_assigned);
    未命中但有兜底 -> 分配兜底负责人; 无映射/无兜底 -> 评论说明后入库跳过。
    幂等: DB 已有 codeowner_assigned 记录则跳过。
    """
    # 幂等: 已按 codeowner 分配过则跳过
    exist = db_find_requirement_by_issue_id(issue_id) if issue_id else None
    if exist and exist.get("status") == "codeowner_assigned":
        return {"action": "skip", "reason": "已按 codeowner 分配",
                "requirement_id": exist["id"]}

    modules, fallback = fetch_codeowner_mapping(repo)
    if not modules and not fallback:
        # 仓库未配置 codeowner 映射 -> 2026-08-11 用户要求: 先尝试**提交历史
        # 推断**主要人工开发者(skill 是谁写的就分给谁), 推断成功则直接分配;
        # 推断不出才按原逻辑跳过。
        # 背景(issue #101 实测): skillspackage 无 codeowner 映射 issue, 非
        # bot 开发 skill 的 bug 全部"已跳过"卡死; 而提交历史是现成事实
        # (huawei-cloud-computing-query -> Jason7602)。
        _primary = None
        _skill_ref2 = extract_skill_names(title, body)
        _skill_ref2 = _skill_ref2[0] if len(_skill_ref2) == 1 else None
        if not _skill_ref2:
            _m2 = re.search(r"(huawei[-_]?cloud-[a-z0-9-]+|[\w-]+-skill)",
                            f"{title} {body}", re.IGNORECASE)
            _skill_ref2 = _m2.group(1) if _m2 else None
        if _skill_ref2:
            # 2026-08-12 用户要求: **不做跨仓兜底**(测试环境, 现网无跨仓) ——
            # 只按当前仓库提交历史推断(分发仓即以上传者为开发者)。
            _primary = _skill_primary_contributor(repo, _skill_ref2)
        if _primary:
            return _assign_contributor(repo, number, _primary, title, body,
                                       issue_id, issue_url, created, updated,
                                       source="提交历史")
        # 无映射且推断不出 -> 按原逻辑跳过
        # 2026-08-11 修复(issue #101 实测): 原实现无幂等, 每次 update 事件
        # 都发一条相同评论, #101 上累计 4 条重复刷屏。加 _issue_has_bot_comment
        # 检查, 已发过则不再重复评论(与 codeowner 分配分支的幂等语义一致)。
        msg = "未识别为 SKILL 开发任务, 且仓库未配置 codeowner 模块负责人映射, 已跳过。"
        if repo and number:
            if not _issue_has_bot_comment(repo, number, msg):
                safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
        return {"action": "skip", "reason": msg}

    mod = match_codeowner_module(modules, title, body)
    owner = (mod or {}).get("owner") if mod else None
    matched = bool(mod)

    if not owner and not matched:
        # ---- 未命中任何模块: 先判断是否与仓库完全无关(assignee-guide Step 5) ----
        if is_unrelated_issue(title, body, modules):
            msg = "该 Issue 与仓库项目范围无关, 已按规范打 invalid 标签并关闭。"
            if repo and number:
                # ① 打 invalid 标签 ② 关闭 ③ 评论说明(对齐 #24 的处理)
                try:
                    set_issue_labels(repo, number, ["invalid"])
                except Exception as e:
                    logger.warning(f"[codeowner] 打 invalid 标签失败: {e}")
                safe_notify(close_gitcode_issue, repo, number)
                if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
            # 2026-08-05 用户要求: 不符合(与仓库无关)的 issue 不写入
            # t_skills_dev_requirement —— 仅打 invalid 标签+关闭+评论, 不留记录。
            # 幂等由 _issue_has_bot_comment 保证, 重复 webhook 不刷屏。
            return {"action": "closed_invalid", "reason": msg, "requirement_id": None}
        # 可能相关但无法确认 -> 走兜底负责人, 让人工判断
        owner = fallback

    if not owner:
        msg = (
            "未识别为 SKILL 开发任务, 且未匹配到 codeowner 模块负责人"
            "(无兜底负责人), 请人工确认处理人。"
        )
        if repo and number:
            # 2026-08-11 修复: 与上方无映射分支同样补幂等, 防 update 事件刷屏
            if not _issue_has_bot_comment(repo, number, msg):
                safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
        # 2026-08-07 用户要求: codeowner 分配不入库 —— 只评论通知,
        # 不写 t_skills_dev_requirement(与判重关闭/竞品云一致)。
        # 幂等由 _issue_has_bot_comment 保证(重复 webhook 不刷屏)。
        return {"action": "no_owner", "reason": msg, "requirement_id": None}

    # 分配责任人(先执行再评论, 对齐 assignee-guide Step 6)
    assign_err = None
    if repo and number:
        try:
            set_issue_assignee(repo, number, owner)
        except Exception as e:
            assign_err = str(e)[:300]
            logger.warning(f"[codeowner] 分配 {owner} 失败: {e}")

    notify = (
        f"👤 该问题属于开发者维护的 skill(非 Multica 自动开发), "
        f"已按 codeowner 映射分配给 {('模块「' + mod['module'] + '」负责人 ') if matched else '兜底负责人 '}"
        f"@{owner} 处理。"
    )
    notify_err = None
    if repo and number:
        # 2026-08-07 用户要求: codeowner 分配不入库 —— 只评论+指派,
        # 幂等靠评论 marker("已按 codeowner 映射分配")防重复 webhook 刷屏。
        if not _issue_has_bot_comment(repo, number, MARKERS["codeowner_assigned"]):
            notify_err = safe_notify(gitcode_issue_comment, repo, number, bot_comment(notify))

    return {"action": "assigned" if matched else "assigned_fallback",
            "owner": owner, "module": (mod or {}).get("module"),
            "requirement_id": None, "assign_error": assign_err,
            "notify_error": notify_err}


def _assign_contributor(repo, number, owner, title, body, issue_id, issue_url,
                        created, updated, source="提交历史"):
    """把 issue 指派给具体开发者(2026-08-11 用户要求, 提交历史驱动)。

    与 codeowner_assign_issue 的分配段逻辑一致: PATCH assignee + 评论通知
    + 入库(status=codeowner_assigned)。source 用于评论说明分配依据
    ("提交历史"推断 / codeowner 映射)。幂等: 评论 marker 防重复。
    """
    assign_err = None
    if repo and number:
        try:
            set_issue_assignee(repo, number, owner)
        except Exception as e:
            assign_err = str(e)[:300]
            logger.warning(f"[assign-contrib] 分配 {owner} 失败: {e}")
    notify = (
        f"👤 该问题属于开发者维护的 skill, 已按 codeowner 映射分配"
        f"(依据:**{source}**)给主要开发者 @{owner} 处理。\n"
        f"> 如非该模块问题, 请回复说明或 @ 相关负责人。"
    )
    notify_err = None
    if repo and number:
        # 幂等: 已分配过则不重复评论(重复 webhook 防刷屏)
        if not _issue_has_bot_comment(repo, number, MARKERS["codeowner_assigned"]):
            notify_err = safe_notify(gitcode_issue_comment, repo, number,
                                     bot_comment(notify))
    # 2026-08-12 用户要求: bug 类型 issue **不入库**(创建/修改都不建记录),
    # 只在评论艾特机器人转修复时才补建 requirement+flow 跟踪进展。
    # 移除原"入库 codeowner_assigned"块(2026-08-11 方案A 曾用于保证
    # req 存在以支持转机器人; 现转机器人已支持 req=None 补建, 不再依赖)。
    # 历史已有记录时也不动 DB —— 避免创建事件重放把转机器人补建的
    # dispatched 状态覆盖回 codeowner_assigned(#104 教训)。
    return {"action": "assigned", "owner": owner, "module": source,
            "requirement_id": None, "assign_error": assign_err,
            "notify_error": notify_err}


# ==================== 各事件处理逻辑 ====================
def handle_issue(payload, force_retry=False):
    """逻辑1: Issue 创建事件 -> 识别 skill 任务, 打标签/质量检查/判重, 建 Multica issue 分配给开发小队, 入库

    force_retry(2026-08-18 方案A): 用户评论 @机器人+重新处理 时强制重新
    评估 —— 跳过\"已有 [skills_bot] 处理评论\"守卫(判重关闭/静默失败等已发过
    评论的 issue 也能重跑完整流程)。exist 幂等守卫保留(有记录且已建任务/
    need_info 的 issue 不重复建任务, 由评论侧兜底告知状态)。
    """
    issue = payload.get("issue") or payload
    repo = get_repo_full_name(payload)
    number = issue.get("number")
    issue_id = issue.get("id")
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    author_login = ((issue.get("user") or {}).get("login")) or ""
    author_name = ((issue.get("user") or {}).get("name")) or author_login
    issue_url = issue.get("html_url") or f"{GITCODE_API_BASE}/repos/{repo}/issues/{number}"
    created = parse_datetime(issue.get("created_at"))
    updated = parse_datetime(issue.get("updated_at")) or created

    # 幂等: 已存在的 issue 跳过(已创建任务或已在 need-info 追问中)
    exist = db_find_requirement_by_issue_id(issue_id)
    if exist and exist.get("multica_issue_id"):
        return {"action": "skip", "reason": "issue 已处理", "requirement_id": exist["id"]}
    # 2026-08-19 方案A: 质量检查不通过 不再入库,
    # need_info 幂等靠 _need_info_requirement 内的评论 marker 检查
    if exist and exist.get("status") == "codeowner_assigned":
        return {"action": "skip", "reason": "issue 已按 codeowner 分配给开发者",
                "requirement_id": exist["id"]}
    if exist and exist.get("status") == "invalid":
        return {"action": "skip", "reason": "issue 已按无关处理关闭(invalid)",
                "requirement_id": exist["id"]}
    # 其余已入库状态(外部系统写入的 pending / duplicate / 历史遗留等)一律不
    # 重走创建流程: 重复评论 + db_insert_flow 唯一键冲突(2026-08-04 issue #102 实测)
    if exist:
        return {"action": "skip",
                "reason": f"issue 已入库(status={exist.get('status')}), 跳过重复处理",
                "requirement_id": exist["id"]}

    # ---- skill-issue 集成: issue 分类(提前到判重之前, 2026-08-09 #241) ----
    # 判重 2.0 的 LLM PR/技能覆盖判断只适用于需求/功能类 issue; bug 报告是
    # "已有 skill 运行异常", 其"覆盖"语义应是修复而非"已实现" —— #241 实测:
    # maas-tokens-usage 运行失败被"新增该 skill 的合入 PR #164"误判"功能点
    # 已有 PR 实现"而关闭, bug 永远无人修复。故 label==bug 时跳过两个 LLM
    # 覆盖检查, 由 2.2 bug 分流(修复流程/codeowner)处理; find_duplicate_issue
    # (同标题/同技能名重复报告)对 bug 仍生效(#193 判重前置要求)。
    # 打标签仍在质量检查/判重通过后统一执行(见下方, 不在此处)。
    label = None
    if ENABLE_LABELS:
        # 2026-08-13 用户要求(需求3): 创建时已带 feature/bug 标签则以标签为准,
        # 跳过重新分类; 无标签才走 classify_issue 规则+LLM。
        label, reasons = _created_label_override(payload, repo, number, title, body)
        if not label:
            label, reasons = classify_issue(title, body)

    # ---- bug 类型 issue: 入库责任人 = 审查人(GITCODE_PR_ASSIGNEE), 与审查人一致 ----
    # (2026-08-16 e3a73bb 移植: bug issue 通常由测试/质检流程创建, 创建人≠修复人;
    #  责任人应指向审查人而非 issue 创建人。实测 issue #15 创建人 wj911831,
    #  修复/开发人是 lccode(汪亮初)。存量数据 requirement id=5 author 已改 lccode。)
    if label == "bug" or "bug" in (_get_issue_labels_from_payload(payload) or []):
        bug_owner = os.environ.get("GITCODE_PR_ASSIGNEE", "lccode").strip()
        if bug_owner:
            author_login = bug_owner
            author_name = bug_owner

    # ---- 需求质量检查(先规则后LLM, 2026-08-18 #98/#99: 空正文/无效标题/模板字段缺失
    # 先 need_info 追问, 不进判重和建任务) ----
    if ENABLE_QUALITY_CHECK:
        # 规则层(零LLM成本): 空正文/空标题/模板字段缺失
        _r_ok, _r_reason = _rule_check_issue_quality(title, body, label)
        if _r_ok is False:
            if (ENABLE_HUAWEI_RELATED_CHECK
                    and huawei_related_level(title, body) == "none"
                    and not has_optimization_intent(title, body)):
                return _close_unrelated_issue(repo, number)
            return _need_info_requirement(
                repo, number, issue_id, issue_url, title, body,
                author_login, author_name, created, updated, _r_reason,
                extra_hint="请补充以下信息后重新提交, 以便定位处理:\n"
                           "- (需求) 明确 skill 名称和具体功能描述\n"
                           "- (问题) 复现步骤、日志、截图、环境版本")
        # LLM 层: 规则通过但需要语义判断(填了是否填的是废话)
        if ENABLE_LLM_TEMPLATE_CHECK and _r_ok is None:
            _llm_ok, _llm_reason = _llm_check_template_quality(title, body, label)
            if not _llm_ok:
                if (ENABLE_HUAWEI_RELATED_CHECK
                        and huawei_related_level(title, body) == "none"
                        and not has_optimization_intent(title, body)):
                    return _close_unrelated_issue(repo, number)
                return _need_info_requirement(
                    repo, number, issue_id, issue_url, title, body,
                    author_login, author_name, created, updated, _llm_reason,
                    extra_hint="请补充以下信息后重新提交, 以便定位处理:\n"
                               "- (需求) 明确 skill 名称和具体功能描述\n"
                               "- (问题) 复现步骤、日志、截图、环境版本")

    # ---- skill-issue 集成: 重复检查(判重 2.0, LLM 语义驱动) ----
    # 2026-08-07 用户要求: 判重从规则匹配升级为 LLM 语义判断。
    #   优化类需求(提到已有 skill 名 + 优化/增强/修改意图词)不走判重逻辑;
    #   LLM 第 1 步: PR 列表(open+merged)能否覆盖 issue 功能点 -> 能则
    #     评论"已有该功能(PR #N 链接)"+ 关闭;
    #   LLM 第 2 步: 仓库已有 skills 能否覆盖 -> 能则评论"已有该功能
    #     (skill 名称)"+ 关闭;
    #   DB 兜底(find_duplicate_issue): 同标题重复提交(历史含 closed)仍拦截。
    # LLM 失败 fail-open 放行(不误关); 优化类跳过全部判重。
    # 2026-08-09 #231 修复: LLM 判重执行过且未命中时置 _llm_dup_checked=True,
    #   已存在检查的 PR 分支尊重该标记 —— LLM 已语义判定"PR 不覆盖需求"
    #   (如 maas-tokens-usage vs maas 列表查询), 规则的产品词匹配不应再按
    #   粗糙词面匹配把 issue 关闭(否则 LLM 判重形同虚设)。
    _llm_dup_checked = False
    # 2026-08-09 #241 修复: **bug 类 issue 跳过 LLM PR/skills 覆盖判重**。
    # 判重 2.0 的语义是"PR/仓库是否已实现该**需求**功能点"—— 对需求类有效;
    # 但 bug 报告("xxx 运行一直失败")的 PR 已合入恰恰说明该 skill 存在且有
    # 问题, 应走**修复流程**而非判重关闭(#241 实测: PR #164 add-skill 合入后,
    # LLM 判 "PR 已覆盖该功能点" 把运行失败 bug 报告误关)。bug 类只保留
    # DB 兜底(find_duplicate_issue 同标题重复拦截), 跳过 LLM 两步。
    if ENABLE_DUP_CHECK and not (is_optimization_request(title, body) and label != "bug"):
        # ---- 统一判重(K3 重构 2026-08-09): 收敛 5 链 —— LLM PR/skills 覆盖
        # (feature) + 实时 issue 判重(bug 用 LLM 同一问题/#248) + DB 相似需求
        # (#221)。只判定不动作, 以下按 kind 分发处理。 ----
        _hit, _llm_ok = _dup_check_issue(repo, title, body, number, label,
                                          exclude_id=exist["id"] if exist else None)
        # 2026-08-18 #70 修复: _llm_dup_checked 从 _dup_check_issue 的
        # llm_ok 标记获得, 而非无条件 True(LLM API 不可用时回退产品词匹配)
        _llm_dup_checked = _llm_ok
        if _hit:
            _kind = _hit["kind"]
            _dup_num = _hit.get("num")
            _dup_url = _hit.get("url")
            if _kind == "pr_covered":
                if repo and number:
                    _msg = (
                        f"📦 该功能点已有 PR 实现: [#{_dup_num}]({_dup_url}), "
                        f"为避免重复开发本 issue 将被关闭。\n"
                        f"> LLM 判重依据: {_hit.get('reason')}"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["dup_pr_covered"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
                    safe_notify(close_gitcode_issue, repo, number)
                return {"action": "duplicate_pr", "reason": f"PR #{_dup_num} 已覆盖该功能点",
                        "requirement_id": None}
            if _kind == "skill_covered":
                if repo and number:
                    _msg = (
                        f"📦 该功能点已有 skill 实现: `{_hit.get('skill_name')}`, "
                        f"为避免重复开发本 issue 将被关闭。\n"
                        f"> LLM 判重依据: {_hit.get('reason')}"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["dup_skill_covered"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
                    safe_notify(close_gitcode_issue, repo, number)
                return {"action": "duplicate_skill",
                        "reason": f"skill {_hit.get('skill_name')} 已覆盖该功能点",
                        "requirement_id": None}
            if _kind == "dup_remind":
                # closed_other(已完成/need-info/invalid 关闭) -> 只评论提示不关闭
                if repo and number:
                    _msg = _dup_comment_msg(
                        label, "remind", _dup_num, _dup_url,
                        (_hit.get("dup") or {}).get("state") or "")
                    _task_suffix = _dup_task_status_suffix(repo, _dup_num)
                    if _task_suffix:
                        _msg += f"\n{_task_suffix}"
                    if not _issue_has_bot_comment(repo, number, MARKERS["dup_closed_similar"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
                return {"action": "dup_remind", "reason": f"与已关闭 #{_dup_num} 相似(不自动关闭)",
                        "requirement_id": None}
            if _kind == "feature_related":
                # LLM 判功能部分重叠 -> 只评论提示不关闭(防线 2)
                if repo and number:
                    _msg = (
                        f"⚠️ 该需求与 [#{_dup_num}]({_dup_url}) 功能高度相似, "
                        f"请确认是否为重复创建。\n"
                        f"> LLM 判重依据: {_hit.get('reason', '')}\n"
                        f"> 如确为不同功能请忽略此提示。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["feature_related"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
                return {"action": "feature_related", "reason": f"与 #{_dup_num} 功能高度相似(仅提示)",
                        "requirement_id": None}
            # dup_open / dup_closed / feature_dup: 重复 -> 评论+关闭
            if repo and number:
                # feature_dup 用 feature_dup 文案+marker; 其他用原有
                _is_feature_dup = (_kind == "feature_dup")
                _msg = _dup_comment_msg(label, "dup", _dup_num, _dup_url)
                _task_suffix = _dup_task_status_suffix(repo, _dup_num)
                if _task_suffix:
                    _msg += f"\n{_task_suffix}"
                if _is_feature_dup:
                    _msg += (
                        f"\n> LLM 判重依据: {_hit.get('reason', '')}\n"
                        f"> 功能已被 #{_dup_num} 覆盖, 如有疑问请回复。"
                    )
                _marker = MARKERS["feature_dup"] if _is_feature_dup else MARKERS["dup_followup"]
                if not _issue_has_bot_comment(repo, number, _marker):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
                safe_notify(close_gitcode_issue, repo, number)
            return {"action": "duplicate", "reason": f"与 #{_dup_num} 重复",
                    "requirement_id": None}

    # 通用已处理守卫(2026-08-07 #192 修复): 判重关闭/竞品云/已存在/无效/codeowner
    # 等分支**不入库**(requirement_id=None, DB 无记录), 重复 open 事件(webhook
    # 重试/重放)到达时 exist=None 无法被上方幂等拦截, 会按新的 classify 结果
    # 走其他分支 —— #192 实测: 首次判 bug 走判重关闭(评论+close, 不入库)后,
    # 重放事件 LLM 改判 feature, 落入 codeowner 分支在**已关闭**的 issue 上
    # 发"已按 codeowner 映射分配"错误评论 + 指派。所有不入库分支都会发
    # [skills_bot] 处理评论, 故存在任一 bot 处理评论即视为已处理, 直接跳过
    # (与 handle_issue_update 无记录分支的守卫同思路)。
    if repo and number:
        # 2026-08-18 方案A: force_retry(用户评论 @机器人+重新处理)时放行 ——
        # 判重关闭/静默失败等已发过 bot 评论的 issue 需要能重新评估。
        if _issue_has_bot_comment(repo, number, BOT_PREFIX) and not force_retry:
            # 2026-08-09 #241: "等待用户补充"类提示(2.2/2.1.3 要求用户修改
            # issue)不是"已处理完成" —— 用户按提示修改后由 handle_issue_update
            # 无记录分支重走本函数重新评估, 该守卫必须放行, 否则用户补充后
            # 永远被拦截(#241: 补充 skill 名后 update 事件被跳过无响应)。
            if _has_waiting_prompt_comment(repo, number):
                logger.info(f"[handle_issue] issue #{number} 存在等待用户补充类提示, 放行重新评估")
            else:
                return {"action": "skip",
                        "reason": "GitCode 已有 [skills_bot] 处理评论(判重/竞品/无效等不入库分支已处理), 跳过重复处理",
                        "requirement_id": None}

    # ---- skill-issue 集成: issue 分类已在上方(判重之前)完成, label 直接复用 ----

    # ---- issue 已关联 PR 检查(2026-08-18 用户要求): 不建任务不入库 ----
    # issue 标题/正文带 PR 引用(评测问题单"对应 PR: .../merge_requests/15"
    # 是典型形态)说明修复已在 PR 中推进, 再建开发任务=重复开发(#43-#47 系列
    # 实测: 测试报告带对应 PR, issue 仍建了重复任务)。命中 -> 评论告知 +
    # 返回, 不创建开发任务、不入库(幂等靠评论 marker)。**必须在 bug 分流
    # 之前** —— 否则 bug 分流的"仓库无此 skill"等分支会提前返回, 带 PR 的
    # issue 根本走不到本检查。与 _bug_skill_open_pr(按 skill 名查 open PR)
    # 互补: 前者查 issue 文本显式关联, 后者查 skill 维度 open PR。
    _linked_pr = _issue_linked_pr(title, body)
    if _linked_pr:
        _msg = (
            f"📦 该 issue 已关联 PR #{_linked_pr}"
            f"(https://gitcode.com/{repo}/pull/{_linked_pr}), "
            f"开发/修复已在 PR 中推进, 本 issue 不再重复创建开发任务, "
            f"请到该 PR 跟进。"
        )
        if repo and number:
            if not _issue_has_bot_comment(repo, number, MARKERS["issue_linked_pr"]):
                safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
        return {"action": "issue_linked_pr",
                "reason": f"issue 已关联 PR #{_linked_pr}, 不重复建任务",
                "requirement_id": None}

    # ---- bug 类 issue 分流(2026-08-08 判重 2.2 重写, 用户要求) ----
    # 2.2.1 前提: 必须提供 skill 具体名称 —— 未提供 -> 评论提示修改 issue;
    # 2.2.2 提供了 skill 名:
    #   ① 查代码仓库是否有该 skill(目录存在):
    #      - 无 -> 评论"仓库无此 skill, 是否需要走需求开发, 请修改 issue
    #        内容为需求内容";
    #      - 有 -> 2026-08-13 用户要求: **无论 skill 是否人工开发, 一律直接
    #        进入机器人修复流水线**(_bug_is_bot=True, 建 Multica 任务
    #        @skills_dev)。原"判断贡献者是否含机器人账号"分流
    #        (_skill_has_bot_contributor / codeowner 分配艾特提交人)已移除;
    #        质量检查统一由主路径执行(描述过简仍 need-info 追问)。
    # 2.2.3 无关内容 -> 直接提示并关闭(由华为云相关性判定 none 分支处理)。
    # 注: 旧逻辑用 _is_bot_developed_skill(查 DB requirement 表)判断, 已按
    # 用户 2026-08-08 要求改为纯 git 贡献者判断, 2026-08-13 起不再分流。
    _bug_is_bot = False
    if ENABLE_CODEOWNER_ASSIGN and label == "bug":
        # 2.2.1: 提取 skill 名(优先全称 huawei-cloud-xxx, 其次 xxx-skill)
        skill_ref = extract_skill_name_from_pr(
            {"title": title, "body": body}, title, body)
        if not skill_ref:
            m = re.search(r"(huawei[-_]?cloud-[a-z0-9-]+|[\w-]+-skill)", title + " " + body, re.IGNORECASE)
            skill_ref = m.group(1) if m else None
        if not skill_ref:
            # 2.2.1 未满足: 无 skill 具体名称 -> 提示修改
            logger.info(f"[bug-split] issue #{number} label=bug 无 skill 名称, 提示修改")
            if repo and number:
                _bug_msg = (
                    "⚠️ 检测到该 issue 为问题/缺陷类, 但未提供具体的 skill 名称。"
                    "请修改 issue 内容, 明确给出出问题的 skill 名称"
                    "(如 `huawei-cloud-xxx`), 以便定位并修复。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["bug_need_skill_name"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
            return {"action": "bug_need_skill_name",
                    "reason": "问题类 issue 未提供 skill 名称, 提示修改",
                    "requirement_id": None}
        # 2026-08-09 #235 修复: 校验 skill 名**格式完整** —— 完整名必须
        # huawei-cloud-{product}-{function}(至少 2 个连字符段, 如
        # huawei-cloud-ascend-command)。用户写的 "huawei-cloud-ascend"
        # (只有 cloud+ascend 两段)是前缀/缩写, 不算完整 skill 名, 应提示
        # 提供完整名称, 而不是按"仓库无此 skill"或宽松匹配误走后续流程。
        if not re.match(r"^huawei[-_]?cloud-[a-z0-9]+(-[a-z0-9]+)+$", skill_ref) \
                and not re.match(r"^[\w-]+-skill$", skill_ref):
            logger.info(f"[bug-split] issue #{number} skill={skill_ref!r} 格式不完整, 提示提供完整名称")
            if repo and number:
                _bug_msg = (
                    f"⚠️ `{skill_ref}` 不是完整的 skill 名称。"
                    f"请修改 issue 内容, 给出**完整的 skill 名称**"
                    f"(如 `huawei-cloud-xxx-yyy`), 以便定位并修复。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["bug_bad_skill_fmt"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
            return {"action": "bug_need_skill_name",
                    "reason": f"skill 名 {skill_ref} 格式不完整, 提示提供完整名称",
                    "requirement_id": None}
        # 2.2.2 ①: 仓库是否有该 skill
        # 2026-08-09 #235 修复: 用**精确匹配**(exact=True) —— 用户给的是
        # 完整 skill 名才继续; 宽松匹配会把 "huawei-cloud-ascend"(想表达
        # ascend 系列)误配到 huawei-cloud-ascend-command, 让本应提示
        # "提供完整 skill 名称"的 issue 错误走贡献者判断/codeowner。
        _has_skill = _repo_has_skill(repo, skill_ref, exact=True)
        if _has_skill is False:
            # 精确未命中: 可能是 ① 用户 skill 名不完整/写错, 或
            # ② 仓库确实无此 skill。检查宽松匹配 —— 宽松命中说明是
            # 不完整名(2.2.1 提示提供完整名称), 宽松也未命中才是真无
            # (2.2.2 提示改为需求)。
            _loose = _repo_has_skill(repo, skill_ref)
            if _loose:
                logger.info(f"[bug-split] issue #{number} skill={skill_ref!r} 非完整名称(宽松命中), 提示提供完整名称")
                if repo and number:
                    _bug_msg = (
                        f"⚠️ 未找到与 `{skill_ref}` 完全匹配的 skill。"
                        f"请修改 issue 内容, 给出**完整的 skill 名称**"
                        f"(如 `huawei-cloud-xxx-yyy`), 以便定位并修复。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["bug_skill_not_found"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                return {"action": "bug_need_skill_name",
                        "reason": f"skill 名 {skill_ref} 不完整(仓库仅宽松匹配), 提示提供完整名称",
                        "requirement_id": None}
            logger.info(f"[bug-split] issue #{number} skill={skill_ref!r} 仓库无此 skill")
            if repo and number:
                _bug_msg = (
                    f"⚠️ 仓库中不存在 skill `{skill_ref}`。"
                    f"如您需要的是该 skill 的开发需求, 请修改 issue 内容为需求描述"
                    f"(说明功能/场景), 将按新需求流程处理。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["bug_repo_no_skill"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
            return {"action": "bug_skill_not_found",
                    "reason": f"仓库无 skill {skill_ref}, 提示改为需求",
                    "requirement_id": None}
        if _has_skill is None:
            # 精确查询受限流无法确认(2026-08-08): 先看宽松匹配能否确认
            # 用户给的其实是不完整名(#235: huawei-cloud-ascend 宽松命中
            # ascend-command, 应提示提供完整名称而非降级修复)。
            _loose2 = _repo_has_skill(repo, skill_ref)
            if _loose2:
                logger.info(f"[bug-split] issue #{number} skill={skill_ref!r} 非完整名称(宽松命中, 限流下确认), 提示提供完整名称")
                if repo and number:
                    _bug_msg = (
                        f"⚠️ 未找到与 `{skill_ref}` 完全匹配的 skill。"
                        f"请修改 issue 内容, 给出**完整的 skill 名称**"
                        f"(如 `huawei-cloud-xxx-yyy`), 以便定位并修复。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["bug_skill_not_found"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                return {"action": "bug_need_skill_name",
                        "reason": f"skill 名 {skill_ref} 不完整(仓库仅宽松匹配), 提示提供完整名称",
                        "requirement_id": None}
            # 限流且宽松也无法确认: 降级按机器人修复流程处理
            # (宁走自动修复也不误关/误指派人)
            logger.warning(f"[bug-split] issue #{number} skill={skill_ref!r} 仓库查询受限流影响, 降级走修复流程")
            _bug_is_bot = True
        else:
            # 2.2.2 ②: 2026-08-13 用户要求 —— bug 类型 issue 无论 skill 是否
            # 人工开发, 一律直接进入机器人修复流水线(建 Multica 任务 @skills_dev)。
            # 原贡献者分流(_skill_has_bot_contributor + codeowner_assign_issue /
            # 提交历史推断艾特提交人)已移除; 质量检查统一由主路径执行
            # (#246: 质量检查一律生效, bug 描述过简仍 need-info 追问)。
            _bug_is_bot = True

    # 2.2.2 ③(2026-08-16 用户要求): bug 类 issue 判断当前问题的 skill 是否
    # 已有 open 状态的 PR 在处理 —— 有则评论提示 + 关闭, 不重复建修复任务
    # (对齐判重关闭语义: 不入库不建任务, 幂等靠评论 marker)。
    if label == "bug" and _bug_is_bot and repo and number:
        _pr_hit = _bug_skill_open_pr(repo, skill_ref)
        if _pr_hit:
            _pr_num, _pr_title = _pr_hit
            _bug_msg = (
                f"📦 该 skill 已有 PR 在处理中: "
                f"[#{_pr_num}](https://gitcode.com/{repo}/pull/{_pr_num})"
                f"({_pr_title[:60]}), 正在开发/审查中, 本 issue 将关闭, 请到该 PR 跟进。"
            )
            if not _issue_has_bot_comment(repo, number, MARKERS["bug_pr_in_progress"]):
                safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
            safe_notify(close_gitcode_issue, repo, number)
            return {"action": "bug_pr_in_progress",
                    "reason": f"skill {skill_ref} 已有 open PR #{_pr_num} 在处理",
                    "requirement_id": None}

    # 非 Multica 自动开发 skill 任务 -> 按 codeowner 映射分配给对应开发者
    # (开发者开发的 skill 由模块负责人处理, 对齐 skill-issue assignee-guide)
    # 注意: bug 且是机器人开发 skill 的 issue(_bug_is_bot)不在此分支 ——
    # 标题不含 "skill" 关键词也应落入自动修复流水线(#187 修复)。
    # 2026-08-07 判重 2.1.3: 优化类需求(命中优化/修改意图词, 含无 skill 名
    # 的「帮我优化一下」)即使标题无 "skill" 词也不走 codeowner —— 它是
    # 需求类, 由 2.1.3 分支处理(单 skill 名走优化开发流程, 无/多 skill 名
    # 提示修改)。
    if not is_skill_task(title, body) and not _bug_is_bot and not has_optimization_intent(title, body):
        # 2026-08-09 #244: codeowner 分配前先过**质量检查** —— 描述过简
        # (bug 缺复现步骤/日志, 需求缺功能说明)应先 need_info 追问, 而不是
        # 直接分配负责人。否则"xxx有问题,请协助定位下"这类无实质描述的
        # issue 会直接进人工分配(#244 实测: LLM 复核把 bug 改判 feature,
        # 绕过 2.2 分支落到此通用 codeowner 路径, 无校验直接分配)。
        if ENABLE_QUALITY_CHECK:
            _qok, _qreason = issue_quality_ok(title, body, label)
            if not _qok:
                logger.info(f"[codeowner] issue #{number} 描述不达标: {_qreason}")
                # 2026-08-13 #125 修复: 完全无关内容(无华为云特征)质量不达标
                # 也直接 invalid 关闭, 不追问不入库 —— 「上海天气很好」灌水
                # issue 先收质量追问后关闭, 且被 need-info 路径错误入库。
                if (ENABLE_HUAWEI_RELATED_CHECK
                        and huawei_related_level(title, body) == "none"
                        and not has_optimization_intent(title, body)):
                    return _close_unrelated_issue(repo, number)
                return _need_info_requirement(
                    repo, number, issue_id, issue_url, title, body,
                    author_login, author_name, created, updated, _qreason,
                    extra_hint="请补充以下信息后重新提交, 以便定位处理:\n"
                               "- 具体问题现象/功能目标\n- 期望行为与边界条件\n"
                               "- (bug) 复现步骤、日志、截图、环境版本")
        # 2026-08-13 #126 修复: codeowner 分支质量检查**通过后**也先判无关 ——
        # 「今天天气怎么样」49字灌水通过质量检查, 打 feature,triaged 标签 +
        # skip 评论后留在 open, 走不到 2.1.4 无关关闭; 无关内容无论描述长短
        # 都应直接 invalid 关闭不追问不入库(与 #125 同源, 质量失败分支已修)。
        if (ENABLE_HUAWEI_RELATED_CHECK
                and huawei_related_level(title, body) == "none"
                and not has_optimization_intent(title, body)):
            return _close_unrelated_issue(repo, number)
        # 2026-08-07 用户要求: codeowner 分配前先分类打标签(与正常流水线
        # 一致, 便于 issue 分类追踪) —— classify_issue 判 feature/bug 后
        # 打 {label},triaged 标签, 再走 codeowner 分配。
        if label and repo and number and ENABLE_LABELS:
            try:
                safe_notify(set_issue_labels, repo, number, [label, "triaged"])
            except Exception as e:
                logger.warning(f"[codeowner] 打标签失败: {e}")
        if ENABLE_CODEOWNER_ASSIGN:
            co = codeowner_assign_issue(
                repo, number, title, body, issue_id, issue_url,
                created, updated, author_login, author_name)
            if co.get("action") in ("assigned", "assigned_fallback", "no_owner",
                                    "skip", "closed_invalid"):
                return {"event": "issue_created", **co}
        msg = "未识别为 SKILL 开发任务(标题或描述中未包含 'skill' 关键词), 已跳过处理。"
        if repo and number:
            safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
        return {"action": "skip", "reason": msg}

    # ---- 竞品云前置检查(2026-08-05 #130 修复) ----
    # 竞品云(阿里云/腾讯云/AWS 等)需求必须先于已存在检查判定 invalid:
    # 否则「查询阿里云的vpc列表」会因产品词 vpc 命中 PR #129(华为云
    # vpc-list)被 find_skill_in_open_prs 误判为"该 skill 已有 PR 在开发"
    # 而错误关闭(事故: #130 被评论"已有 PR #129"关闭, 实际是阿里云需求,
    # 与华为云 skill 绝不重复)。竞品云需求直接 invalid 关闭。
    if ENABLE_HUAWEI_RELATED_CHECK and is_competitor_cloud(title, body):
        if repo and number:
            # 标签幂等(2026-08-06 修复 #164 双标签): 评论已发过说明本次
            # 竞品云处理已完成(标签+评论+关闭), 重复 webhook(create+update
            # 双事件)不应再打一次标签。与评论共用同一幂等检查。
            if not _issue_has_bot_comment(repo, number, MARKERS["competitor_cloud"]):
                safe_notify(set_issue_labels, repo, number, ["invalid"])
            # 幂等 marker 必须与评论正文真实存在的关键词一致(2026-08-06 修复
            # #163 双评论: 原来用 "非华为云" 但评论正文无此词, 恒匹配失败,
            # 重复 webhook 每次都重发评论)。用正文首句关键词做标记。
            if not _issue_has_bot_comment(repo, number, MARKERS["competitor_cloud"]):
                msg = (
                    "⚠️ 检测到该需求面向其他云厂商(阿里云/腾讯云/AWS 等), "
                    "本仓库仅开发华为云产品相关的 skill, 已按无关关闭。"
                    "若确属华为云需求, 请重新创建 issue 并注明对应产品"
                    "(如 ECS / RDS / OBS 等)。"
                )
                safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
            safe_notify(close_gitcode_issue, repo, number)
        # 2026-08-05 用户要求: 不符合(竞品云)的 issue 不写入
        # t_skills_dev_requirement —— 仅做 invalid 标签+评论+关闭, 不留记录。
        # 幂等由 _issue_has_bot_comment("非华为云") 保证, 重复 webhook 不刷屏。
        return {"action": "invalid_competitor_cloud",
                "reason": "需求面向竞品云(非华为云)", "requirement_id": None}

    # ---- 已存在检查(2026-08-04 #112): 仓库已有 skill / DB 已有类似需求 ----
    # 需求对应产品/技能已在仓库开发过, 或 DB 已有进行中/已完成的类似需求
    # -> 评论提示 + 关闭, 不重复建 Multica 任务
    # 修改/迭代类需求(提到已有 skill 名称 + 修改意图词, #142)跳过本检查:
    # 「huawei-cloud-gaussdb-count 这个skill现在要改成只查询数量」是要在
    # 已有 skill 上迭代, 仓库里有同名目录是正常前提, 不是重复开发。
    # bug 修复需求(_bug_is_bot, #187)同样跳过: 机器人开发已上线的 skill
    # 出 bug 需要修复, 仓库里存在同名 skill 是正常前提, 不能判"重复开发"。
    if ENABLE_EXISTING_CHECK and not _bug_is_bot and not is_skill_modification(title, body):
        found_skill, skill_path = find_skill_in_repo(repo, title, body)
        if found_skill:
            if repo and number:
                msg = (
                    f"📦 该需求对应的 skill 已在仓库中开发过(`{skill_path}`), "
                    f"无需重复开发, 本 issue 将关闭。如需新功能请在原 skill 上迭代。"
                )
                # 2026-08-07 用户要求: 判重关闭不入库 —— 只评论+关闭,
                # 幂等靠评论 marker("已在仓库中开发过")防重复 webhook 刷屏。
                if not _issue_has_bot_comment(repo, number, MARKERS["repo_has_skill"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                safe_notify(close_gitcode_issue, repo, number)
            return {"action": "already_existing", "reason": f"仓库已存在: {skill_path}",
                    "requirement_id": None}
        # 已提交 PR 中包含该 skill(待合入, 2026-08-04 用户要求: 名称不能与
        # 已提交 PR 重复) -> 评论提示 + 关闭, 不建任务
        # 2026-08-09 #231: LLM 判重已执行(未命中)时 strict_skill_only=True
        # (仅技能名精确匹配, 跳过产品词匹配 —— 否则 maas-tokens-usage PR
        # 会误关 maas 列表查询需求)
        found_pr, pr_num, pr_title = find_skill_in_open_prs(
            repo, title, body, strict_skill_only=_llm_dup_checked)
        if found_pr and pr_num:
            if repo and number:
                msg = (
                    f"📦 该 skill 已有 PR 提交(PR #{pr_num}: {pr_title[:60]}), "
                    f"正在开发/审查中, 本 issue 将关闭, 请到该 PR 跟进。"
                )
                # 2026-08-07 用户要求(#181): 判重关闭不入库 —— 只评论+关闭,
                # 幂等靠评论 marker("已有 PR 提交")。
                if not _issue_has_bot_comment(repo, number, MARKERS["open_pr_has_skill"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                safe_notify(close_gitcode_issue, repo, number)
            return {"action": "already_existing", "reason": f"PR #{pr_num} 已包含该 skill",
                    "requirement_id": None}
            # dup_open / dup_closed: 重复 -> 评论+关闭(质量检查前置判重)
            if repo and number:
                msg = _dup_comment_msg(label, "dup", dup_num, dup_url)
                _task_suffix = _dup_task_status_suffix(repo, dup_num)
                if _task_suffix:
                    msg += f"\n{_task_suffix}"
                # 幂等靠评论 marker("已在原 issue 中跟进")防重复 webhook 刷屏
                if not _issue_has_bot_comment(repo, number, MARKERS["dup_followup"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                safe_notify(close_gitcode_issue, repo, number)
            return {"action": "duplicate", "reason": f"与 #{dup_num} 重复(质量检查前置判重)",
                    "requirement_id": None}
            # 2026-08-13 #125 修复: 完全无关内容(无华为云特征且非优化类)质量
            # 不达标也**直接 invalid 关闭**, 不追问不入库 —— 「上海天气很好」
            # 灌水 issue 描述短(<30)先收质量追问, 补充后才走 2.1.4 无关关闭,
            # 且被 need-info 路径错误入库。无关内容无论描述长短都应直接关闭。
            if (ENABLE_HUAWEI_RELATED_CHECK
                    and huawei_related_level(title, body) == "none"
                    and not has_optimization_intent(title, body)):
                return _close_unrelated_issue(repo, number)
            if repo and number and ENABLE_LABELS:
                # 保留主标签(feature/bug), 追加 need-info; 避免覆盖导致类型丢失
                tags = ([label] if label else []) + ["need-info"]
                safe_notify(set_issue_labels, repo, number, tags)
            # 统一 need_info(S2 重构): 评论追问 + 入库 + flow
            return _need_info_requirement(
                repo, number, issue_id, issue_url, title, body,
                author_login, author_name, created, updated, qreason)

    # ---- 2.1.3 优化类需求: 必须给出唯一具体 skill 名称(2026-08-07 用户要求) ----
    # 识别为优化类需求(命中优化/修改意图词)时:
    #   - 未提取到 skill 名 或 提取到多个 -> 评论提示 issue 创建者修改 issue
    #     内容(明确给出唯一的 skill 名称), 不进入判重/开发流程(等用户修改,
    #     update 事件会重新评估);
    #   - 恰好一个 skill 名 -> 2026-08-13 用户要求: **无论 skill 是否人工开发,
    #     一律直接进入机器人开发流水线**(原"判断贡献者是否含机器人账号"分流
    #     与 codeowner 分配已移除, 与 bug 分流同步修改)。
    # 2026-08-13 #113 实测修复: **bug 类排除本分流** —— 评测问题单正文含
    # "改进/修改/修复"(修复建议等常规用语)会误命中优化意图词, 把 bug 报告
    # 劫持成优化类; bug 报告即使正文提改进也应走 bug 修复流程(与判重 2.0
    # bug 语义一致, #241)。condition 加 label != "bug"。
    # 2026-08-07 调整: 本检查必须在华为云相关性判定(2.1.4 无关关闭)之前 ——
    # 「优化一下查询速度」无华为云特征但确属优化类需求, 应提示修改而非关闭。
    # 用 has_optimization_intent(仅意图词, 不要求 skill 名): 「帮我优化一下」
    # 无 skill 名也是优化类, 应提示补充 skill 名称。
    if has_optimization_intent(title, body) and label != "bug":
        _skill_names = extract_skill_names(title, body)
        if len(_skill_names) != 1:
            if repo and number:
                _opt_msg = (
                    "⚠️ 检测到该需求为优化/修改类需求, 但未明确指定要优化的 skill。"
                    "请修改 issue 内容, 明确给出**唯一**的 skill 名称"
                    "(如 `huawei-cloud-xxx`), 以便定位要优化的 skill。\n"
                    "当前识别到的 skill 名称: "
                    + ("无" if not _skill_names else "、".join(f"`{n}`" for n in _skill_names))
                    + "。补充后将自动进入优化开发流程。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["opt_need_skill_name"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_opt_msg))
            return {"action": "opt_need_skill_name",
                    "reason": "优化类需求未给出唯一 skill 名称, 提示修改",
                    "requirement_id": None}
        # 恰好一个 skill 名 -> 2026-08-13 用户要求: 优化类需求无论 skill 是否
        # 人工开发, 一律直接进机器人开发流水线。原贡献者分流
        # (_opt_is_bot 判断 + codeowner_assign_issue 分配)已移除,
        # 继续向下走主路径(华为云相关性 -> 建任务 -> 入库 -> 打标签)。
        # (机器人开发的 skill -> 走正常优化开发流程, 继续向下)

    # ---- 华为云相关性判定(2026-08-07 判重 2.1 调整, 用户要求) ----
    # 2.1.1 竞品云(阿里云/腾讯云/AWS 等)已在前置检查 invalid 关闭;
    # 2.1.2 无明确厂商 -> **默认按华为云处理**(不再追问产品词);
    # 2.1.4 无华为云特征(产品词/cloud/云服务 均未命中, 且非竞品云)-> 无关,
    #      直接提示并关闭(2.1.4)。
    # 旧逻辑: none/weak -> need-info 追问产品词(2026-08-04 #113 温和模式)。
    # 已按用户 2026-08-07 要求移除追问: weak(仅正文命中)也按华为云放行,
    # 只有完全无华为云特征(none)才判无关关闭。
    if ENABLE_HUAWEI_RELATED_CHECK:
        _level = huawei_related_level(title, body)
        if _level == "none":
            # 2.1.4 无关内容 -> 提示 + 关闭(不入库, 幂等靠评论 marker)
            if repo and number:
                if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
                    safe_notify(set_issue_labels, repo, number, ["invalid"])
                msg = (
                    "⚠️ 该 issue 内容与华为云产品 skill 开发无关"
                    "(未识别到对应华为云产品/场景), 本仓库仅开发华为云相关的 skill, "
                    "已按无关内容关闭。如确属华为云需求, 请补充说明对应的"
                    "华为云产品(如 ECS / RDS / OBS 等)后重新提交。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                safe_notify(close_gitcode_issue, repo, number)
            return {"action": "invalid_unrelated", "reason": "无华为云特征, 按无关内容关闭",
                    "requirement_id": None}
        # strong(标题命中)/weak(仅正文命中) -> 默认按华为云处理(2.1.2)

    # ---- skill-issue 集成: 自动打标签(质量检查与判重均通过后, 打主标签+triaged) ----
    # 2026-08-06 全量排查: 打标签必须放在写库之后 —— multica_issue_create
    # 可能抛异常, 若在写库前打标签, 异常退出时标签已打但无 DB 记录, gateway
    # 重试会重复打标签。先写库(幂等锚点)再打标签(与 L3324/L3535 一致)。

    # 创建开发任务, 失败时告知创建者(2026-08-18: 文案不涉及任务系统内部)
    m_issue_id, m_identifier, _task_err = _create_dev_task_or_notify(
        repo, number, title, body)
    if not m_issue_id:
        return {"action": "task_create_failed",
                "reason": f"创建开发任务失败: {_task_err or '未知错误'}",
                "requirement_id": None}
    m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"

    # 入库
    req_id = db_insert_requirement({
        "gitcode_issue_id": issue_id, "gitcode_issue_number": str(number),
        "gitcode_issue_url": issue_url, "title": title, "body": body,
        "state": "opened", "author": author_login, "author_name": author_name,
        "created_at_gitcode": created, "updated_at_gitcode": updated,
        "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
        "multica_issue_number": m_identifier, "status": "dispatched",
        "requirement_type": "requirement", "gitcode_pr_id": None,
    })
    db_insert_flow(req_id, issue_id, m_issue_id, {
        "node_source": ("done", issue_url),
        "node_preprocess": ("done", m_issue_url),
    })

    # 写库成功后打标签(幂等锚点已建立, 重试不会重复打)
    if ENABLE_LABELS and repo and number and label:
        ensure_labels(repo)
        safe_notify(set_issue_labels, repo, number, [label, "triaged"])

    # 默认设置 issue 负责人为创建人(2026-08-19 用户要求)
    if repo and number and author_login:
        safe_notify(set_issue_assignee, repo, number, author_login)

    # 评论通知提交者
    notify = (
        f"已识别SKILL开发任务, 任务正在处理中, 任务详细状态参见链接: "
        f"{_devflow_url(req_id)}"
    )
    notify_err = None
    if repo and number:
        notify_err = safe_notify(gitcode_issue_comment, repo, number, bot_comment(notify))

    return {"action": "created", "multica_issue_id": m_issue_id,
            "multica_issue_number": m_identifier, "requirement_id": req_id,
            "label": label, "notify_error": notify_err}


def handle_issue_update(payload):
    """逻辑1b: Issue 更新事件(用户编辑补充信息) -> 重新质量检查, need_info 可升级创建任务

    GitCode 创建 issue 后还会推送多个 update 事件; 用户编辑补充 body 后,
    若之前是 need_info 状态, 在此重新评估并升级。避免遗漏"补充后不处理"。
    """
    issue = payload.get("issue") or payload
    repo = get_repo_full_name(payload)
    number = issue.get("number")
    issue_id = issue.get("id")
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    updated = parse_datetime(issue.get("updated_at"))
    issue_url = issue.get("html_url") or f"{GITCODE_API_BASE}/repos/{repo}/issues/{number}"
    author_login = ((issue.get("user") or {}).get("login")) or ""
    author_name = ((issue.get("user") or {}).get("name")) or author_login

    exist = db_find_requirement_by_issue_id(issue_id)

    # 已有任务: 2026-08-08 用户要求 —— **拒绝修改, 不同步 body**。
    # 需求已在开发中(Multica 任务已建), 用户修改 title/body 不应影响
    # 正在开发的任务: DB 保持原需求(与开发任务一致), 评论告知用户
    # "修改不生效, 变更请新建 issue"(幂等: 重复 update 事件不刷屏)。
    if exist and exist.get("multica_issue_id"):
        # 仅当 DB 内容确实与当前 issue 不一致时才发评论(幂等防刷屏:
        # GitCode 创建后连推多个相同 update 事件, 不应每条都评论)
        if (str(exist.get("title") or "") != str(title or "")
                or str(exist.get("body") or "") != str(body or "")):
            if repo and number:
                _dev_msg = (
                    f"⚠️ 该需求已在开发中, "
                    f"对 issue 的修改**不生效**, 开发仍按原需求进行。\n"
                    f"如需变更需求, 请**新建 issue** 描述变更内容, 或等当前任务"
                    f"开发完成后提交迭代需求。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["dev_in_progress_lock"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_dev_msg))
        return {"action": "skip_reject_update", "reason": "issue 已在开发中, 拒绝修改(不同步 body)",
                "requirement_id": exist["id"]}

    # 无 multica 任务的记录: 重新评估(need_info / 外部系统写入的 pending 等)
    # 2026-08-04 #109 实测: 外部系统把 status 改成 pending 后, 用户修改 body/
    # title 触发的 update 事件被旧逻辑"仅同步 body 跳过", 补充后的需求从未
    # 重新判断。终态(duplicate/invalid)不重新评估; codeowner_assigned 仅在
    # 仍非 skill 任务时跳过 —— 用户把 issue 改成含 "skill" 的标题后应重新
    # 识别为 skill 开发任务(#132 事故: 创建时标题不含 skill 走 codeowner,
    # 用户修改后 update 被终态拦截, 从未重新评估, 也无任何评论反馈)。
    if exist and not exist.get("multica_issue_id"):
        if exist.get("status") in ("duplicate", "invalid"):
            db_update_requirement(exist["id"], {
                "title": title, "body": body, "updated_at_gitcode": updated,
            })
            return {"action": "skip",
                    "reason": f"issue 终态(status={exist.get('status')}), 同步 body 后跳过",
                    "requirement_id": exist["id"]}
        if exist.get("status") == "codeowner_assigned" and not is_skill_task(title, body):
            db_update_requirement(exist["id"], {
                "title": title, "body": body, "updated_at_gitcode": updated,
            })
            return {"action": "skip",
                    "reason": "codeowner 分配中且仍非 skill 任务, 同步 body 后跳过",
                    "requirement_id": exist["id"]}
        # 2026-08-13 用户要求(需求3): 创建时已带 feature/bug 标签则以标签为准
        label, _ = _created_label_override(payload, repo, number, title, body)
        if not label:
            label, _ = classify_issue(title, body)
        ok, qreason = issue_quality_ok(title, body, label)
        if not ok:
            # 2026-08-13 #125 修复(同步 handle_issue): 完全无关内容质量不达标
            # 也直接 invalid 关闭, 不追问不入库(「上海天气很好」先追问后关闭)
            if (ENABLE_HUAWEI_RELATED_CHECK
                    and huawei_related_level(title, body) == "none"
                    and not has_optimization_intent(title, body)):
                db_update_requirement(exist["id"], {
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return _close_unrelated_issue(repo, number, req_id=exist["id"])
            # 仍不完整: 同步 body; 若此前从未发过质量追问评论则补发一条
            # (避免用户修改后完全无反馈 —— #132: 用户把标题改成含 skill 的
            # 需求后, 质量不通过静默同步, 无任何评论; 但同内容重复 update
            # 不重复发, 防刷屏)
            if repo and number:
                if not _issue_has_bot_comment(repo, number, MARKERS["need_info"]):
                    msg = (
                        f"⚠️ 需求信息不完整: {qreason}。\n"
                        f"请补充以下信息后重新提交, 以便开发小队准确实施:\n"
                        f"- 具体功能/修复目标\n- 期望行为与边界条件\n"
                        f"- (bug) 复现步骤、日志、截图、环境版本"
                    )
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
            db_update_requirement(exist["id"], {
                "title": title, "body": body, "updated_at_gitcode": updated,
            })
            return {"action": "still_need_info", "reason": qreason,
                    "requirement_id": exist["id"]}
        # 补充完整 -> 升级创建任务(先判重: Multica 已有同标题活跃任务/仓库已有同标题
        # open issue 都说明是重复需求, 2026-08-04 #103 实测: 撞 Multica duplicate 校验崩溃)
        # 华为云产品相关性校验: 竞品云明确 -> invalid 关闭; none(未命中)/
        # weak(仅正文命中, #113 复制残留风险) -> need-info 追问(评论幂等)。
        # 注意: 旧实现仅在 need_info 状态才评估, pending 等状态漏判(#109),
        # 现无 multica 非终态一律评估。
        # 2026-08-08 同步 2.2: bug 类 issue 升级时也走 2.2 分流(与 handle_issue
        # 一致) —— 无 skill 名提示修改 / 仓库无此 skill 提示改需求 / 有 skill
        # 按贡献者分流(bot -> 修复流程, 非 bot -> codeowner)。
        _bug_is_bot = False
        if label == "bug":
            _skill_ref = extract_skill_name_from_pr(
                {"title": title, "body": body}, title, body)
            if not _skill_ref:
                m = re.search(r"(huawei[-_]?cloud-[a-z0-9-]+|[\w-]+-skill)", title + " " + body, re.IGNORECASE)
                _skill_ref = m.group(1) if m else None
            if not _skill_ref:
                # 2.2.1 未满足: 无 skill 具体名称 -> 提示修改
                if repo and number:
                    _bug_msg = (
                        "⚠️ 检测到该 issue 为问题/缺陷类, 但未提供具体的 skill 名称。"
                        "请修改 issue 内容, 明确给出出问题的 skill 名称"
                        "(如 `huawei-cloud-xxx`), 以便定位并修复。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["bug_need_skill_name"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                db_update_requirement(exist["id"], {
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "bug_need_skill_name",
                        "reason": "问题类 issue 未提供 skill 名称, 提示修改",
                        "requirement_id": exist["id"]}
            # 2026-08-09 #235 修复: 校验 skill 名格式完整(同 handle_issue)
            if not re.match(r"^huawei[-_]?cloud-[a-z0-9]+(-[a-z0-9]+)+$", _skill_ref) \
                    and not re.match(r"^[\w-]+-skill$", _skill_ref):
                if repo and number:
                    _bug_msg = (
                        f"⚠️ `{_skill_ref}` 不是完整的 skill 名称。"
                        f"请修改 issue 内容, 给出**完整的 skill 名称**"
                        f"(如 `huawei-cloud-xxx-yyy`), 以便定位并修复。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["bug_bad_skill_fmt"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                db_update_requirement(exist["id"], {
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "bug_need_skill_name",
                        "reason": f"skill 名 {_skill_ref} 格式不完整, 提示提供完整名称",
                        "requirement_id": exist["id"]}
            # 2.2.2 ①: 仓库是否有该 skill(#235 修复: 精确匹配判定完整名称)
            _has_skill = _repo_has_skill(repo, _skill_ref, exact=True)
            if _has_skill is False:
                # 精确未命中: 宽松命中 -> 用户 skill 名不完整, 提示提供完整名称
                _loose = _repo_has_skill(repo, _skill_ref)
                if _loose:
                    if repo and number:
                        _bug_msg = (
                            f"⚠️ 未找到与 `{_skill_ref}` 完全匹配的 skill。"
                            f"请修改 issue 内容, 给出**完整的 skill 名称**"
                            f"(如 `huawei-cloud-xxx-yyy`), 以便定位并修复。"
                        )
                        if not _issue_has_bot_comment(repo, number, MARKERS["bug_skill_not_found"]):
                            safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                    db_update_requirement(exist["id"], {
                        "title": title, "body": body, "updated_at_gitcode": updated,
                    })
                    return {"action": "bug_need_skill_name",
                            "reason": f"skill 名 {_skill_ref} 不完整(仓库仅宽松匹配), 提示提供完整名称",
                            "requirement_id": exist["id"]}
                if repo and number:
                    _bug_msg = (
                        f"⚠️ 仓库中不存在 skill `{_skill_ref}`。"
                        f"如您需要的是该 skill 的开发需求, 请修改 issue 内容为需求描述"
                        f"(说明功能/场景), 将按新需求流程处理。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["bug_repo_no_skill"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                db_update_requirement(exist["id"], {
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "bug_skill_not_found",
                        "reason": f"仓库无 skill {_skill_ref}, 提示改为需求",
                        "requirement_id": exist["id"]}
            if _has_skill is None:
                # 限流无法确认: 先看宽松匹配(#235: 不完整名应提示而非降级修复)
                _loose2 = _repo_has_skill(repo, _skill_ref)
                if _loose2:
                    if repo and number:
                        _bug_msg = (
                            f"⚠️ 未找到与 `{_skill_ref}` 完全匹配的 skill。"
                            f"请修改 issue 内容, 给出**完整的 skill 名称**"
                            f"(如 `huawei-cloud-xxx-yyy`), 以便定位并修复。"
                        )
                        if not _issue_has_bot_comment(repo, number, MARKERS["bug_skill_not_found"]):
                            safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                    db_update_requirement(exist["id"], {
                        "title": title, "body": body, "updated_at_gitcode": updated,
                    })
                    return {"action": "bug_need_skill_name",
                            "reason": f"skill 名 {_skill_ref} 不完整(仓库仅宽松匹配), 提示提供完整名称",
                            "requirement_id": exist["id"]}
                # 限流且宽松也无法确认: 降级走修复流程(与 handle_issue 一致)
                _bug_is_bot = True
            else:
                # 2026-08-13 用户要求: bug 一律直接进机器人修复流水线
                # (与 handle_issue 一致, 原贡献者分流 + codeowner 分配已移除)
                _bug_is_bot = True
        # 2.2.2 ③(2026-08-16 用户要求, 与 handle_issue 同步): bug 类 issue
        # 判断当前问题的 skill 是否已有 open 状态的 PR 在处理 —— 有则评论
        # 提示 + 关闭, 不重复建修复任务(不入库不建任务, 幂等靠评论 marker)。
        if label == "bug" and _bug_is_bot and repo and number:
            _pr_hit = _bug_skill_open_pr(repo, _skill_ref)
            if _pr_hit:
                _pr_num, _pr_title = _pr_hit
                _bug_msg = (
                    f"📦 该 skill 已有 PR 在处理中: "
                    f"[#{_pr_num}](https://gitcode.com/{repo}/pull/{_pr_num})"
                    f"({_pr_title[:60]}), 正在开发/审查中, 本 issue 将关闭, 请到该 PR 跟进。"
                )
                if not _issue_has_bot_comment(repo, number, MARKERS["bug_pr_in_progress"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_bug_msg))
                safe_notify(close_gitcode_issue, repo, number)
                db_update_requirement(exist["id"], {
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "bug_pr_in_progress",
                        "reason": f"skill {_skill_ref} 已有 open PR #{_pr_num} 在处理",
                        "requirement_id": exist["id"]}
        # ---- 2026-08-08 同步 2.1.3: 优化类需求升级时同样要求唯一 skill 名 ----
        # (与 handle_issue 一致: 无/多 skill 名提示修改; 单名一律直接进机器人
        #  开发流水线, 2026-08-13 用户要求, 原贡献者分流已移除)
        # 2026-08-13 #113: bug 类排除本分流(评测问题单正文"改进/修复"等词
        # 误命中优化意图, 与 handle_issue 同步)。
        if not _bug_is_bot and has_optimization_intent(title, body) and label != "bug":
            _skill_names = extract_skill_names(title, body)
            if len(_skill_names) != 1:
                if repo and number:
                    _opt_msg = (
                        "⚠️ 检测到该需求为优化/修改类需求, 但未明确指定要优化的 skill。"
                        "请修改 issue 内容, 明确给出**唯一**的 skill 名称"
                        "(如 `huawei-cloud-xxx`), 以便定位要优化的 skill。\n"
                        "当前识别到的 skill 名称: "
                        + ("无" if not _skill_names else "、".join(f"`{n}`" for n in _skill_names))
                        + "。补充后将自动进入优化开发流程。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["opt_need_skill_name"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(_opt_msg))
                db_update_requirement(exist["id"], {
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "opt_need_skill_name",
                        "reason": "优化类需求未给出唯一 skill 名称, 提示修改",
                        "requirement_id": exist["id"]}
            # 单 skill 名 -> 2026-08-13 用户要求: 优化类一律直接进机器人开发
            # 流水线(与 handle_issue 一致, 原贡献者分流 + codeowner 分配已移除,
            # 继续向下走主路径建任务)
        if ENABLE_HUAWEI_RELATED_CHECK:
            _level = huawei_related_level(title, body)
            if is_competitor_cloud(title, body):
                # 明确竞品云(阿里云/腾讯云/aws 等) -> invalid 关闭(只打 invalid)
                if repo and number:
                    # 标签幂等(2026-08-06 修复 #164 双标签): 与评论共用幂等检查
                    if not _issue_has_bot_comment(repo, number, MARKERS["competitor_cloud"]):
                        safe_notify(set_issue_labels, repo, number,
                                    ["invalid"])
                    if not _issue_has_bot_comment(repo, number, MARKERS["competitor_cloud"]):
                        msg = (
                            "⚠️ 检测到该需求面向其他云厂商(阿里云/腾讯云/AWS 等), "
                            "本仓库仅开发华为云产品相关的 skill, 已按无关关闭。"
                            "若确属华为云需求, 请重新创建 issue 并注明对应产品"
                            "(如 ECS / RDS / OBS 等)。"
                        )
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                    safe_notify(close_gitcode_issue, repo, number)
                db_update_requirement(exist["id"], {
                    "status": "invalid", "state": "closed",
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "invalid_competitor_cloud",
                        "reason": "需求面向竞品云(非华为云)",
                        "requirement_id": exist["id"]}
            if _level == "none":
                # 2.1.4 无关内容(2026-08-08 同步): 无华为云特征 -> 直接提示并关闭
                # (与 handle_issue 一致; 旧逻辑 none/weak 均 need-info 追问已移除)
                if repo and number:
                    if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
                        safe_notify(set_issue_labels, repo, number, ["invalid"])
                    msg = (
                        "⚠️ 该 issue 内容与华为云产品 skill 开发无关"
                        "(未识别到对应华为云产品/场景), 本仓库仅开发华为云相关的 skill, "
                        "已按无关内容关闭。如确属华为云需求, 请补充说明对应的"
                        "华为云产品(如 ECS / RDS / OBS 等)后重新提交。"
                    )
                    if not _issue_has_bot_comment(repo, number, MARKERS["unrelated_repo"]):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                    safe_notify(close_gitcode_issue, repo, number)
                db_update_requirement(exist["id"], {
                    "status": "invalid", "state": "closed",
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "invalid_unrelated", "reason": "无华为云特征, 按无关内容关闭",
                        "requirement_id": exist["id"]}
            # weak(仅正文命中) -> 默认按华为云处理(2.1.2), 继续向下
        # 2026-08-09 #231: 判重检查执行过 -> 已存在检查 PR 分支仅技能名
        # 精确匹配(跳过产品词匹配, 尊重语义判断)
        _llm_dup_checked = False
        if ENABLE_DUP_CHECK and not (is_optimization_request(title, body) and label != "bug"):
            # K3 重构(2026-08-09): 统一判重入口, 与 handle_issue 语义一致
            _hit, _llm_ok = _dup_check_issue(repo, title, body, number, label,
                                              exclude_id=exist["id"] if exist else None)
            _llm_dup_checked = _llm_ok  # #70: LLM API 不可用时回退产品词匹配
            if _hit:
                _kind = _hit["kind"]
                _dup_num = _hit.get("num")
                _dup_url = _hit.get("url")
                if _kind == "dup_remind":
                    # closed_other(非重复原因关闭)只提示不关闭
                    if repo and number:
                        msg = _dup_comment_msg(
                            label, "remind", _dup_num, _dup_url,
                            (_hit.get("dup") or {}).get("state") or "")
                        _task_suffix = _dup_task_status_suffix(repo, _dup_num)
                        if _task_suffix:
                            msg += f"\n{_task_suffix}"
                        if not _issue_has_bot_comment(repo, number, MARKERS["dup_closed_similar"]):
                            safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                    return {"action": "dup_remind",
                            "reason": f"与已关闭 #{_dup_num} 相似(升级路径, 不自动关闭)",
                            "requirement_id": exist["id"]}
                if _kind == "feature_related":
                    # LLM 判功能部分重叠 -> 只评论提示不关闭(防线 2)
                    if repo and number:
                        msg = (
                            f"⚠️ 该需求与 [#{_dup_num}]({_dup_url}) 功能高度相似, "
                            f"请确认是否为重复创建。\n"
                            f"> LLM 判重依据: {_hit.get('reason', '')}\n"
                            f"> 如确为不同功能请忽略此提示。"
                        )
                        if not _issue_has_bot_comment(repo, number, MARKERS["feature_related"]):
                            safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                    return {"action": "feature_related",
                            "reason": f"与 #{_dup_num} 功能高度相似(仅提示)",
                            "requirement_id": exist["id"]}
                # pr_covered / skill_covered / dup_open / dup_closed
                if _kind == "pr_covered":
                    if repo and number:
                        msg = (
                            f"📦 该功能点已有 PR 实现: [#{_dup_num}]({_dup_url}), "
                            f"为避免重复开发本 issue 将被关闭。\n"
                            f"> LLM 判重依据: {_hit.get('reason')}"
                        )
                        if not _issue_has_bot_comment(repo, number, MARKERS["dup_pr_covered"]):
                            safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                        safe_notify(close_gitcode_issue, repo, number)
                    db_update_requirement(exist["id"], {
                        "status": "duplicate", "title": title, "body": body,
                        "updated_at_gitcode": updated,
                    })
                    return {"action": "duplicate_pr", "reason": f"PR #{_dup_num} 已覆盖该功能点",
                            "requirement_id": exist["id"]}
                if _kind == "skill_covered":
                    if repo and number:
                        msg = (
                            f"📦 该功能点已有 skill 实现: `{_hit.get('skill_name')}`, "
                            f"为避免重复开发本 issue 将被关闭。\n"
                            f"> LLM 判重依据: {_hit.get('reason')}"
                        )
                        if not _issue_has_bot_comment(repo, number, MARKERS["dup_skill_covered"]):
                            safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                        safe_notify(close_gitcode_issue, repo, number)
                    db_update_requirement(exist["id"], {
                        "status": "duplicate", "title": title, "body": body,
                        "updated_at_gitcode": updated,
                    })
                    return {"action": "duplicate_skill",
                            "reason": f"skill {_hit.get('skill_name')} 已覆盖该功能点",
                            "requirement_id": exist["id"]}
                # dup_open / dup_closed / feature_dup
                if repo and number:
                    _is_feature_dup = (_kind == "feature_dup")
                    msg = _dup_comment_msg(label, "dup", _dup_num, _dup_url)
                    _task_suffix = _dup_task_status_suffix(repo, _dup_num)
                    if _task_suffix:
                        msg += f"\n{_task_suffix}"
                    if _is_feature_dup:
                        msg += (
                            f"\n> LLM 判重依据: {_hit.get('reason', '')}\n"
                            f"> 功能已被 #{_dup_num} 覆盖, 如有疑问请回复。"
                        )
                    _marker = MARKERS["feature_dup"] if _is_feature_dup else MARKERS["dup_followup"]
                    if not _issue_has_bot_comment(repo, number, _marker):
                        safe_notify(gitcode_issue_comment, repo, number, bot_comment(msg))
                    safe_notify(close_gitcode_issue, repo, number)
                db_update_requirement(exist["id"], {
                    "status": "duplicate", "title": title, "body": body,
                    "updated_at_gitcode": updated,
                })
                return {"action": "duplicate", "reason": f"与 #{_dup_num} 重复",
                        "requirement_id": exist["id"]}
        # ---- 已存在检查(2026-08-04 #118 修复): 升级路径此前缺失, 导致
        # dcs 需求(仓库已有 + PR #117)在补充后直接建任务 ---- 
        # 修改/迭代类需求(提到已有 skill 名称 + 修改意图词, #142)跳过本检查。
        if ENABLE_EXISTING_CHECK and not is_skill_modification(title, body):
            found_skill, skill_path = find_skill_in_repo(repo, title, body)
            if found_skill:
                if repo and number:
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(
                        f"📦 该需求对应的 skill 已在仓库中开发过(`{skill_path}`), "
                        f"无需重复开发, 本 issue 将关闭。如需新功能请在原 skill 上迭代。"))
                    safe_notify(close_gitcode_issue, repo, number)
                db_update_requirement(exist["id"], {
                    "status": "duplicate", "state": "closed",
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "already_existing", "reason": f"仓库已存在: {skill_path}",
                        "requirement_id": exist["id"]}
            found_pr, pr_num, pr_title = find_skill_in_open_prs(
                repo, title, body, strict_skill_only=_llm_dup_checked)
            if found_pr and pr_num:
                if repo and number:
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(
                        f"📦 该 skill 已有 PR 提交(PR #{pr_num}: {pr_title[:60]}), "
                        f"正在开发/审查中, 本 issue 将关闭, 请到该 PR 跟进。"))
                    safe_notify(close_gitcode_issue, repo, number)
                db_update_requirement(exist["id"], {
                    "status": "duplicate", "state": "closed",
                    "title": title, "body": body, "updated_at_gitcode": updated,
                })
                return {"action": "already_existing", "reason": f"PR #{pr_num} 已包含该 skill",
                        "requirement_id": exist["id"]}
        # ---- issue 已关联 PR 检查(2026-08-18 用户要求, 与 handle_issue 同口径) ----
        _linked_pr = _issue_linked_pr(title, body)
        if _linked_pr:
            _msg = (
                f"📦 该 issue 已关联 PR #{_linked_pr}"
                f"(https://gitcode.com/{repo}/pull/{_linked_pr}), "
                f"开发/修复已在 PR 中推进, 本 issue 不再重复创建开发任务, "
                f"请到该 PR 跟进。"
            )
            if repo and number:
                if not _issue_has_bot_comment(repo, number, MARKERS["issue_linked_pr"]):
                    safe_notify(gitcode_issue_comment, repo, number, bot_comment(_msg))
            return {"action": "issue_linked_pr",
                    "reason": f"issue 已关联 PR #{_linked_pr}, 不重复建任务",
                    "requirement_id": exist["id"]}
        m_issue_id, m_identifier, _task_err = _create_dev_task_or_notify(
            repo, number, title, body)
        if not m_issue_id:
            return {"action": "task_create_failed",
                    "reason": f"创建开发任务失败: {_task_err or '未知错误'}",
                    "requirement_id": exist["id"]}
        m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"
        db_update_requirement(exist["id"], {
            "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
            "multica_issue_number": m_identifier, "status": "dispatched",
            "title": title, "body": body, "updated_at_gitcode": updated,
        })
        db_update_flow(exist["id"], "node_preprocess", "done", m_issue_url)
        if ENABLE_LABELS and repo and number and label:
            ensure_labels(repo)
            safe_notify(set_issue_labels, repo, number, [label, "triaged"])
        safe_notify(gitcode_issue_comment, repo, number,
                    bot_comment(
                        f"已识别SKILL开发任务, 任务正在处理中, 任务详细状态参见链接: "
                        f"{_devflow_url(exist['id'])}"))
        return {"action": "upgraded_from_need_info", "multica_issue_id": m_issue_id,
                "multica_issue_number": m_identifier, "requirement_id": exist["id"]}

    # 安全兜底: 有 multica 任务的记录已在上面分支处理, 无 multica 的记录也
    # 已进入重新评估分支; 此处仅防御异常数据(如 multica_issue_id 为空字符串),
    # 仅同步 body 不重走创建流程 —— 否则重复评论/撞唯一键(#102 教训)
    if exist:
        db_update_requirement(exist["id"], {
            "title": title, "body": body, "updated_at_gitcode": updated,
        })
        return {"action": "skip",
                "reason": f"issue 已有记录(status={exist.get('status')}), 同步 body 后跳过",
                "requirement_id": exist["id"]}

    # 无记录: open 事件可能丢失, 走完整创建流程
    # 2026-08-06 修复 #168 多标签: 不能在此重走 handle_issue 创建!
    # GitCode 创建 issue 后连推 create+多个 update 事件, 且 handle_issue
    # 内部是先 multica_issue_create 再 db_insert_requirement(两者有窗口)。
    # update 事件在窗口期到达 -> exist=None -> 若重走 handle_issue 会再次
    # multica_issue_create 撞 duplicate 抛异常(SKI-157 实测), 且重复打标签。
    # 正确行为: 静默跳过 —— 创建事件(GitCode 必发)负责完整流程, update
    # 事件在此场景下无需处理(避免重复建单/打标签)。若创建事件确实丢失,
    # 后续用户编辑触发的 update 也会重试; 这里不再主动补建。
    # 2026-08-19 方案A: 无 DB 记录但有 need_info marker ->
    # 用户编辑补充信息后重新评估(与 DB 记录场景的 re-eval 语义对齐)。
    if not exist and repo and number:
        if _issue_has_bot_comment(repo, number, MARKERS["need_info"]):
            logger.info(f"[issue-update] issue #{number} 存在 need_info 追问, 重走创建流程重新评估(方案A)")
            return handle_issue(payload)
    if repo and number:
        _done_markers = (
            "已识别SKILL开发任务", "需求信息不完整",
            "未识别到对应的华为云产品", "标题未体现对应的华为云产品",
            "该 Issue 与仓库项目范围无关", "面向其他云厂商",
            # 2026-08-07 #185: codeowner 分配也产生 bot 评论, 补入 marker
            # 使跳过原因真实(此前落到兜底文案误报"无 bot 评论"; 行为同为 skip)
            "已按 codeowner 映射分配",
        )
        _has_waiting = _has_waiting_prompt_comment(repo, number)
        _has_done = any(_issue_has_bot_comment(repo, number, mk) for mk in _done_markers)
        if _has_waiting and not _has_done:
            # 2026-08-09 #241 修复: 2.2/2.1.3 "等待用户补充"提示不是流程完成
            # —— create 事件在该分支评论提示用户修改 issue 后**不入库**返回,
            # 用户按提示编辑(如补充 skill 名称)触发的 update 事件若跳过, 需求
            # 永远卡死(#241 实测: 补充 skill 名后无任何响应)。重走 handle_issue
            # (其 bug 2.2 分流在质量检查之前, 与 create 路径语义一致), 各提示
            # 分支评论有 marker 幂等, 不会重复刷屏。
            logger.info(f"[issue-update] issue #{number} 存在等待用户补充类提示, 重走创建路径重新评估")
            return handle_issue(payload)
        if _has_done:
            return {"action": "skip", "reason": "GitCode 已有 bot 处理评论(创建流程进行中/已完成), 跳过重复处理"}
    return {"action": "skip", "reason": "DB 无记录但 GitCode 无 bot 评论; 创建事件负责完整流程, update 不重走创建(防重复建单/打标签)"}


# 评论触发"转机器人处理"意图词(2026-08-11 用户要求):
# 非 bot 开发的 skill 的 issue(bug 修复 / 优化类需求)已按 codeowner 分配
# 人工后, 用户评论要求机器人处理(bug 修复 / 优化开发) -> 转自动流水线
# (建 Multica 任务)。触发分支不区分 label —— bug 与优化类均适用。
BOT_FIX_REQUEST_RE = re.compile(
    r"让机器人(?:来)?(?:修(?:复|改)?|处?理|开发|改一下|优?化)"
    r"|转(?:给|交|入)?(?:给)?机器人(?:修(?:复|改)?|处?理|开发|优?化)?"
    r"|机器人(?:来|帮|帮忙|协助)?(?:修(?:复|改)?|处?理|开发|改一下|优?化)"
    # 2026-08-12 修复: 原 (?:请|帮|帮我|麻烦)? 是单个可选组, "请你帮我修复"
    # ("请"+"你"+"帮我"三 token) 匹配失败 -> issue #104 实测艾特无效落
    # "未找到 Multica 任务"。拆成 (?:请|麻烦)?(?:你)?(?:帮我|帮|帮忙)? 组合,
    # 覆盖 请修复/帮我修复/请你帮我修复/麻烦你帮我修复 等句式。
    r"|@?skills_bot\s*(?:请|麻烦)?(?:你)?(?:帮我|帮|帮忙)?\s*(?:修(?:复|改)?|处?理|开发|优?化)"
    r"|由机器人(?:修(?:复|改)?|处?理|开发|优?化)"
    r"|(?:让|请|叫)机器人",
    re.IGNORECASE,
)


def is_bot_fix_request(note_body):
    """判断评论是否含"转机器人处理"意图(2026-08-11 用户要求)。

    命中 -> codeowner 已分配的 issue(bug 修复 / 优化类)转自动流水线
    (建 Multica 任务)。空/异常 fail-open 返回 False(不误触发)。
    2026-08-11 修复(#101 实测): 评论常用 markdown mention 链接格式
    [@skills_bot](https://gitcode.com/skills_bot), 其中 `]` 紧跟
    skills_bot 后, 阻断正则 `@?skills_bot\\s*` 的空白匹配 —— 用户
    "帮我处理一下这些BUG" 意图未触发转机器人, 落为"未找到 Multica 任务"。
    先剥离链接外壳还原裸 @名(与 is_irrelevant_comment 的剥离一致,
    但保留 @名供正则匹配), 再匹配意图词。
    """
    try:
        if not note_body:
            return False
        b = re.sub(r"\[@([^\]]+)\]\([^)]+\)", r"@\1", note_body)
        return bool(BOT_FIX_REQUEST_RE.search(b))
    except Exception:
        return False


# 评论触发"重新处理 issue"意图词(2026-08-18 方案A, 用户要求): @机器人 +
# 重试/重新处理类词 -> 强制重新评估整个 issue(对齐 PR 评论"重新扫描"机制,
# 解决"建任务失败/判重关闭后用户干等, 无法主动触发重新处理"的痛点)。
# 与 is_bot_fix_request(转机器人修复)区分: 重试强调"重新走一遍流程",
# 转机器人强调"让机器人执行修复/开发"。词表不含"修复/开发/优化"(那归转
# 机器人), 避免与转机器人意图互相吞并。
ISSUE_RETRY_RE = re.compile(
    r"(?:@?skills_bot|@?check_bot|机器人)"
    r"[\s\S]{0,12}?"
    r"(?:重试|重新处理|重新评估|重新触发|重新检查|重新走|再试|重跑|"
    r"重新审|重新看|再处理一次|重来)",
    re.IGNORECASE,
)


def is_issue_retry_request(note_body):
    """判断评论是否为"@机器人 + 重新处理"触发(2026-08-18 方案A)。

    命中 -> handle_issue_comment 走重新评估分支(强制重跑完整流程)。
    必须同时满足: ① 艾特机器人(@skills_bot/@check_bot 或"机器人"字样);
    ② 重试/重新处理类关键词(间隔 ≤12 字符)。两者缺一不可, 空/异常
    fail-open 返回 False(不误触发)。
    """
    try:
        if not note_body:
            return False
        b = re.sub(r"\[@([^\]]+)\]\([^)]+\)", r"@\1", note_body)
        return bool(ISSUE_RETRY_RE.search(b))
    except Exception:
        return False


def _mentions_bot(note_body):
    """检测评论是否提到了机器人(但未匹配到具体操作指令)。
    用于在静默跳过前回复用户正确的触发词。"""
    try:
        if not note_body:
            return False
        b = re.sub(r"\[@([^\]]+)\]\([^)]+\)", r"@\1", note_body)
        return bool(re.search(r"@?(?:skills_bot|check_bot|机器人)", b, re.IGNORECASE))
    except Exception:
        return False


def handle_issue_comment(payload):
    """逻辑2: Issue 评论事件 -> 评论意图处理(关闭请求/信息补充) + 转发到对应 Multica issue"""
    note = payload.get("note") or {}
    issue = payload.get("issue") or {}
    repo = get_repo_full_name(payload)
    issue_number = issue.get("number")
    issue_id = issue.get("id")
    note_id = note.get("id")
    note_body = note.get("body") or ""
    note_author = ((note.get("author") or {}).get("login")) or ""

    # 程序自身评论直接跳过(防死循环)
    if note_author.strip().lower() in BOT_AUTHOR_LOGINS:
        return {"action": "skip", "reason": "程序自身评论"}

    req = db_find_requirement_by_issue_id(issue_id)

    # 2026-08-12 幂等加固(issue #102 复盘): gitcode_issue_id 依赖 payload 顶层
    # issue.id, GitCode 格式变化/字段缺失时解析为 None -> 补建 requirement 落
    # gitcode_issue_id=NULL, MySQL 唯一索引对 NULL 不生效 -> 每次评论都重复
    # 补建 + force_new 新建 Multica 任务(雪崩), 且 NULL 孤儿记录按 id 永远
    # 查不回。按 (issue_number + repo) 兜底查询(带仓库校验防跨仓编号冲突),
    # 与 gitcode_issue_id 互备: 历史 NULL 记录也能查回, 走已有任务拦截。
    if not req and issue_number and repo:
        _req_by_num = db_find_requirement_by_issue_number(issue_number, repo)
        if _req_by_num:
            req = _req_by_num
            logger.info(
                f"[comment] issue #{issue_number} 按 gitcode_issue_id 未命中, "
                f"按 number+repo 兜底命中 requirement id={_req_by_num.get('id')} "
                f"(gitcode_issue_id 可能为 NULL)")

    # ---- skill-issue 集成: 评论意图处理 ----
    if ENABLE_COMMENT_INTENT and repo and issue_number:
        # 意图1: 用户请求关闭 Issue(如"请关闭" / "close") -> 关闭并确认
        if re.search(r"请?关闭|关掉|取消(需求|任务|这个|该)?|close\b|不用(做|处理|开发)", note_body, re.IGNORECASE):
            safe_notify(close_gitcode_issue, repo, issue_number)
            if req:
                db_update_requirement(req["id"], {"state": "closed"})
            safe_notify(gitcode_issue_comment, repo, issue_number,
                        bot_comment("✅ 已按您的要求关闭该 Issue。若后续需要重新开启, 请告知。"))
            return {"action": "closed_by_request", "requirement_id": req["id"] if req else None}

        # 无关评论过滤(2026-08-05 用户要求): 灌水短文本/纯 emoji/与需求无关
        # 的闲聊评论静默过滤 —— 不转发 Multica、不触发意图2 的"信息仍不完整"
        # 追问。意图1(关闭请求)已在上方处理, 天然豁免; 判定异常 fail-open。
        # 2026-08-20 用户要求: 含 @机器人 的无关评论 -> 回复提示词汇总表,
        # 而非静默跳过; 纯路人灌水(无 @机器人)保持静默。
        if ENABLE_COMMENT_FILTER:
            try:
                if is_irrelevant_comment(
                        note_body,
                        issue.get("title") or "",
                        issue.get("body") or ""):
                    # 检查是否含 @机器人 mention: 艾特了机器人但提示词不对,
                    # 应回复提示而非静默跳过
                    _stripped_body = re.sub(
                        r"\[@([^\]]+)\]\([^)]+\)", r"@\1", note_body or "")
                    if re.search(
                            r"(?:@?(?:skills_bot|check_bot)|机器人)",
                            _stripped_body, re.IGNORECASE):
                        _guide_marker = "如果您需要机器人协助处理"
                        if not _issue_has_bot_comment(
                                repo, issue_number, _guide_marker):
                            _guide_msg = (
                                "您好, 暂无法识别您的指令。如果您需要机器人协助处理, "
                                "请使用以下格式：\n\n"
                                "- @skills_bot 重新处理 — 强制重新评估整个 issue\n"
                                "- @skills_bot 转机器人处理 — 转为机器人开发/修复流水线\n"
                                "- @skills_bot 请关闭 — 关闭本 issue\n"
                                "- 直接补充 issue 描述信息 — 信息补充完整后自动继续处理\n\n"
                                "非以上指令时机器人将保持静默, 感谢理解。"
                            )
                            safe_notify(gitcode_issue_comment, repo, issue_number,
                                        bot_comment(_guide_msg))
                        return {"action": "skip_with_guide",
                                "reason": "无关评论含@机器人, 已回复引导提示",
                                "requirement_id": req["id"] if req else None}
                    logger.info(
                        f"[comment-filter] issue #{issue_number} 评论 {note_id} "
                        f"无关(灌水/闲聊), 静默跳过: {(note_body or '')[:50]}")
                    return {"action": "skip", "reason": "无关评论(灌水/闲聊), 静默过滤",
                            "requirement_id": req["id"] if req else None}
            except Exception as e:
                logger.warning(f"[comment-filter] 判定异常, fail-open 继续: {e}")

        # ---- 评论触发"重新处理 issue"(2026-08-18 方案A, 用户要求) ----
        # @机器人 + 重试/重新处理类词 -> 强制重新评估(对齐 PR 评论"重新扫描")。
        # 场景: 建任务失败静默(#45)/判重关闭后用户想重试(#47 重开无响应)时,
        # 用户评论 "@skills_bot 重新处理" 即可重新走完整流程。与转机器人区分:
        # 重试 = 重新走流程(重新判重/分流/建任务), 转机器人 = 强制进修复流水线。
        # 放在意图2(need_info 补充)之前 —— 否则 need_info issue 的"重新处理"
        # 评论会被当成补充信息做质量检查, 重试永远不生效。
        if is_issue_retry_request(note_body):
            # 幂等守卫: 已有"已识别开发任务"的 bot 评论时跳过(防 GitCode webhook 重复投递)
            # 避免第一次 webhook 已成功建任务后, 第二次到达时发"已在开发中"多余回复
            if _issue_has_bot_comment(repo, issue_number, MARKERS["dev_identified"]):
                return {"action": "skip",
                        "reason": "retry 已处理(存在已识别marker), 跳过重复 webhook"}
            # 已有任务: 告知状态不重跑(避免重复建任务, 对齐 bot_fix_in_progress)
            if req and req.get("multica_issue_id"):
                _msg = (
                    f"🤖 该 issue 已有开发任务在处理中, 处理完成后会自动同步结果。\n"
                    f"任务状态可在此跟踪: {_devflow_url(req['id'])}\n"
                    f"若任务已结束仍需要重新处理, 请回复\"转机器人处理\"。"
                )
                if repo and issue_number:
                    safe_notify(gitcode_issue_comment, repo, issue_number,
                                bot_comment(_msg))
                return {"action": "retry_already_processing",
                        "reason": "该 issue 已有开发任务在处理中, 不重复评估",
                        "requirement_id": req["id"]}
            # need_info: 提示补充信息(重跑也会被 exist 幂等拦截)
            # 2026-08-19 方案A: 无 DB 记录时改用 marker 检测
            if (req and req.get("status") == "need_info") or (
                    not req and repo and issue_number
                    and _issue_has_bot_comment(repo, issue_number,
                                               MARKERS["need_info"])):
                _msg = (
                    f"ℹ️ 该 issue 当前为待补充信息状态, 请按之前的提示补充"
                    f"具体内容后保存, 将自动重新处理。"
                )
                if repo and issue_number:
                    safe_notify(gitcode_issue_comment, repo, issue_number,
                                bot_comment(_msg))
                return {"action": "retry_need_info",
                        "reason": "issue 待补充信息, 补充后自动重新处理",
                        "requirement_id": (req or {}).get("id")}
            # 其余(无记录/已关闭等): 强制重新评估完整流程(force_retry 放行
            # bot 评论守卫, 判重关闭/静默失败的 issue 也能重跑)
            logger.info(f"[issue-retry] issue #{issue_number} 评论触发重新处理")
            _retry_result = handle_issue(payload, force_retry=True)
            # 2026-08-18 用户要求: issue 已有记录时给出可见反馈而非静默跳过
            # (issue #93: 用户 @机器人 无反应, 因 exist 幂等守卫直接拦且无评论)
            if _retry_result and _retry_result.get("action") == "skip":
                _msg = "ℹ️ 该 issue 已处理，无需重试/请查看原评论。"
                if repo and issue_number:
                    safe_notify(gitcode_issue_comment, repo, issue_number,
                                bot_comment(_msg))
                return {"event": "issue_retried", "action": "retry_skip_notified",
                        "reason": "issue 已有处理记录, 已通知用户"}
            return {"event": "issue_retried", **(_retry_result or {})}


        # 意图2: 该需求之前信息不足(need_info), 用户补充信息 -> 重新质量检查, 通过则升级创建任务
        # 2026-08-19 方案A: DB 记录或评论 marker 均可触发(无 DB 记录时用 marker 检测)
        _is_need_info = False
        _src_title = ""
        _src_body = ""
        _req_id = None
        if req and req.get("status") == "need_info":
            _is_need_info = True
            _src_title = req.get("title") or ""
            _src_body = req.get("body") or ""
            _req_id = req["id"]
        elif not req and repo and issue_number:
            if _issue_has_bot_comment(repo, issue_number, MARKERS["need_info"]):
                _is_need_info = True
                _src_title = issue.get("title") or ""
                _src_body = issue.get("body") or ""
        if _is_need_info:
            label, _ = classify_issue(_src_title, combined_body)
            # 以 原正文 + 补充评论 合并评估
            combined_body = f"{_src_body}\n\n【用户补充】{note_body}"
            ok, qreason = issue_quality_ok(_src_title, combined_body, label)
            if ok:
                # 2026-08-19 方案A: 无 DB 记录时在此创建 requirement
                if not _req_id:
                    _author_login = (issue.get("user") or {}).get("login") or ""
                    _author_name = (issue.get("user") or {}).get("login") or ""
                    _created = issue.get("created_at") or None
                    _updated = issue.get("updated_at") or None
                    _state = issue.get("state") or "opened"
                    _issue_url = issue.get("html_url") or f"https://gitcode.com/{repo}/issues/{issue_number}"
                    _req_id = db_insert_requirement({
                        "gitcode_issue_id": issue_id,
                        "gitcode_issue_number": str(issue_number),
                        "gitcode_issue_url": _issue_url,
                        "title": _src_title,
                        "body": combined_body,
                        "state": _state,
                        "author": _author_login,
                        "author_name": _author_name,
                        "created_at_gitcode": _created,
                        "updated_at_gitcode": _updated,
                        "multica_issue_id": None,
                        "multica_issue_url": None,
                        "multica_issue_number": None,
                        "status": "pending",
                        "requirement_type": "requirement",
                        "gitcode_pr_id": None,
                    })
                    if not _req_id:
                        return {"action": "error", "reason": "创建 requirement 记录失败"}
                # 华为云产品相关性校验(2026-08-04): 竞品云明确 -> invalid 关闭;
                # none/weak(仅正文命中, #113 复制残留) -> need-info 追问(评论幂等)。
                # 与 handle_issue_update 一致。
                if ENABLE_HUAWEI_RELATED_CHECK:
                    _level = huawei_related_level(_src_title or "", combined_body)
                    if is_competitor_cloud(_src_title or "", combined_body):
                        if repo and issue_number:
                            # 标签幂等(2026-08-06 修复 #164 双标签): 与评论共用幂等检查
                            if not _issue_has_bot_comment(repo, issue_number, MARKERS["competitor_cloud"]):
                                safe_notify(set_issue_labels, repo, issue_number,
                                            ["invalid"])
                            if not _issue_has_bot_comment(repo, issue_number, MARKERS["competitor_cloud"]):
                                msg = (
                                    "⚠️ 检测到该需求面向其他云厂商(阿里云/腾讯云/AWS 等), "
                                    "本仓库仅开发华为云产品相关的 skill, 已按无关关闭。"
                                    "若确属华为云需求, 请重新创建 issue 并注明对应产品"
                                    "(如 ECS / RDS / OBS 等)。"
                                )
                                safe_notify(gitcode_issue_comment, repo, issue_number,
                                            bot_comment(msg))
                            safe_notify(close_gitcode_issue, repo, issue_number)
                        db_update_requirement(_req_id, {
                            "status": "invalid", "state": "closed",
                        })
                        return {"action": "invalid_competitor_cloud",
                                "reason": "需求面向竞品云(非华为云)",
                                "requirement_id": _req_id}
                    if _level == "none":
                        # 2.1.4 无关内容(2026-08-08 同步): 无华为云特征 -> 直接提示并关闭
                        if repo and issue_number:
                            if not _issue_has_bot_comment(repo, issue_number, MARKERS["unrelated_repo"]):
                                safe_notify(set_issue_labels, repo, issue_number, ["invalid"])
                            msg = (
                                "⚠️ 该 issue 内容与华为云产品 skill 开发无关"
                                "(未识别到对应华为云产品/场景), 本仓库仅开发华为云相关的 skill, "
                                "已按无关内容关闭。如确属华为云需求, 请补充说明对应的"
                                "华为云产品(如 ECS / RDS / OBS 等)后重新提交。"
                            )
                            if not _issue_has_bot_comment(repo, issue_number, MARKERS["unrelated_repo"]):
                                safe_notify(gitcode_issue_comment, repo, issue_number,
                                            bot_comment(msg))
                            safe_notify(close_gitcode_issue, repo, issue_number)
                        db_update_requirement(_req_id, {
                            "status": "invalid", "state": "closed",
                            "body": combined_body,
                        })
                        return {"action": "invalid_unrelated",
                                "reason": "无华为云特征, 按无关内容关闭",
                                "requirement_id": _req_id}
                    # weak(仅正文命中) -> 默认按华为云处理(2.1.2), 继续向下
                # 先判重: 仓库已有同标题 open issue 时按重复处理, 不创建新 Multica 任务
                # (2026-08-04 #103 升级分支撞 Multica duplicate 校验崩溃, 此处同源加固)
                # 2026-08-09 #241: bug 类跳过技能名匹配(只按标题归一化判重)
                if ENABLE_DUP_CHECK:
                    dup, dup_kind = find_duplicate_issue(
                        repo, _src_title or "", combined_body,
                        exclude_number=str(issue_number) if issue_number else None,
                        skip_skill_match=(label == "bug"))
                    if dup:
                        dup_num = dup.get("number") or dup.get("iid")
                        dup_url = dup.get("html_url") or f"https://gitcode.com/{repo}/issues/{dup_num}"
                        # 2026-08-07 讨论: closed_other(非重复原因关闭)只提示不关闭
                        if dup_kind == "closed_other":
                            if repo and issue_number:
                                msg = (
                                    f"ℹ️ 该需求与已关闭的 [#{dup_num}]({dup_url}) 相似"
                                    f"(状态: {dup.get('state')}), 如确需重新开发/迭代, "
                                    f"请补充说明与原需求的差异。"
                                )
                                if not _issue_has_bot_comment(repo, issue_number, MARKERS["dup_closed_similar"]):
                                    safe_notify(gitcode_issue_comment, repo, issue_number,
                                                bot_comment(msg))
                            return {"action": "dup_remind",
                                    "reason": f"与已关闭 #{dup_num} 相似(不自动关闭)",
                                    "requirement_id": _req_id}
                        if repo and issue_number:
                            msg = (f"🔁 该需求与 [#{dup_num}]({dup_url}) 重复, "
                                   f"已在原 issue 中跟进, 为避免重复开发本 issue 将被关闭。")
                            if not _issue_has_bot_comment(repo, issue_number, MARKERS["dup_followup"]):
                                safe_notify(gitcode_issue_comment, repo, issue_number,
                                            bot_comment(msg))
                            safe_notify(close_gitcode_issue, repo, issue_number)
                        db_update_requirement(_req_id, {"status": "duplicate"})
                        return {"action": "duplicate", "reason": f"与 #{dup_num} 重复",
                                "requirement_id": _req_id}
                # 已存在检查(2026-08-04 #118 修复): 评论补充升级路径同样补齐
                # 修改/迭代类需求(提到已有 skill 名称 + 修改意图词, #142)跳过。
                if ENABLE_EXISTING_CHECK and not is_skill_modification(
                        _src_title or "", combined_body):
                    _ct = _src_title or ""
                    found_skill, skill_path = find_skill_in_repo(repo, _ct, combined_body)
                    if found_skill:
                        if repo and issue_number:
                            safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(
                                f"📦 该需求对应的 skill 已在仓库中开发过(`{skill_path}`), "
                                f"无需重复开发, 本 issue 将关闭。如需新功能请在原 skill 上迭代。"))
                            safe_notify(close_gitcode_issue, repo, issue_number)
                        db_update_requirement(_req_id, {
                            "status": "duplicate", "state": "closed", "body": combined_body,
                        })
                        return {"action": "already_existing", "reason": f"仓库已存在: {skill_path}",
                                "requirement_id": _req_id}
                    found_pr, pr_num, pr_title = find_skill_in_open_prs(
                        repo, _ct, combined_body, strict_skill_only=True)
                    if found_pr and pr_num:
                        if repo and issue_number:
                            safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(
                                f"📦 该 skill 已有 PR 提交(PR #{pr_num}: {pr_title[:60]}), "
                                f"正在开发/审查中, 本 issue 将关闭, 请到该 PR 跟进。"))
                            safe_notify(close_gitcode_issue, repo, issue_number)
                        db_update_requirement(_req_id, {
                            "status": "duplicate", "state": "closed", "body": combined_body,
                        })
                        return {"action": "already_existing", "reason": f"PR #{pr_num} 已包含该 skill",
                                "requirement_id": _req_id}
                m_issue_id, m_identifier = multica_issue_create(
                    _src_title or "", combined_body, SQUAD_DEV)
                m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"
                db_update_requirement(_req_id, {
                    "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
                    "multica_issue_number": m_identifier, "status": "dispatched",
                    "body": combined_body,
                })
                db_update_flow(_req_id, "node_preprocess", "done", m_issue_url)
                if ENABLE_LABELS:
                    safe_notify(set_issue_labels, repo, issue_number,
                                [label or "feature", "triaged"])
                safe_notify(gitcode_issue_comment, repo, issue_number,
                            bot_comment(
                                f"已识别SKILL开发任务, 任务正在处理中, 任务详细状态参见链接: "
                                f"{_devflow_url(_req_id)}"))
                return {"action": "upgraded_from_need_info", "multica_issue_id": m_issue_id,
                        "multica_issue_number": m_identifier, "requirement_id": _req_id}
            else:
                safe_notify(gitcode_issue_comment, repo, issue_number,
                            bot_comment(f"⚠️ 信息仍不完整: {qreason}。请继续补充(如复现步骤/日志/具体需求描述)。"))
                return {"action": "still_need_info", "reason": qreason, "requirement_id": _req_id}

    # 2026-08-08 用户要求: 开发中(已有 multica 任务)issue 的评论,
    # 命中需求变更意图 -> 拒绝并提示新建 issue(与 title/body 修改的拒绝
    # 语义一致); 普通反馈/补充信息 -> 继续向下正常转发给开发小队。
    # 变更意图词: 改/换/增删功能/不要 等需求变更表述(不含 修改/优化/增强
    # —— 这些可能是开发过程中的普通反馈, 应转发)。
    if req and req.get("multica_issue_id"):
        _change_match = re.search(
            r"改成|改为|变更为|换成|更换|换掉|新增|增加|添加|加个|加一下|"
            r"删除|去掉|移除|不要|不需要|别做|追加|取消|撤掉",
            note_body)
        if _change_match:
            _change_word = _change_match.group(0)
            if repo and issue_number:
                _change_msg = (
                    f"⚠️ 该需求已在开发中, "
                    f"评论中的变更要求(**{_change_word}**)**不生效**, 开发仍按原需求进行。\n"
                    f"如需变更需求, 请**新建 issue** 描述变更内容, 或等当前任务"
                    f"开发完成后提交迭代需求。"
                )
                if not _issue_has_bot_comment(repo, issue_number, MARKERS["comment_change_rej"]):
                    safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(_change_msg))
            return {"action": "reject_change_comment",
                    "reason": f"开发中 issue 评论含变更意图({_change_word}), 拒绝并提示新建 issue",
                    "requirement_id": req["id"]}

    # ---- 评论触发"转机器人处理"(2026-08-11 用户要求) ----
    # 非 bot 开发的 skill 的 issue(bug 修复 / 优化类需求, 已按 codeowner 分配
    # 人工/其他无 Multica 任务状态), 用户评论要求"让机器人处理/优化" ->
    # 转自动流水线: 建 Multica 任务(@skills_dev) + 入库(dispatched) + flow 更新
    # + 评论确认。已有 Multica 任务的 issue 不需要转(已在自动流水线), 跳过。
    # 2026-08-11 修复(#101 实测): codeowner/提交历史分配的 issue 按用户要求
    # **不入库**(#182)导致 req 恒为 None, 原条件 `req and ...` 使该分支成为
    # 死代码 —— 用户"@skills_bot 帮我处理一下这些BUG"永远触发不了。改为
    # `not req or ...`: 无记录时先补建 requirement(status=dispatched) + flow,
    # 再建 Multica 任务(与 handle_issue 建任务路径一致)。
    # 2026-08-11 用户要求: 已有任务**正在执行**时, 艾特机器人应回复"正在执行中"
    # 而非重复建任务 —— 避免一个 issue 产生多个并行开发任务(实测 #102 重复
    # 评论建出多个任务)。判定: req 有 multica_issue_id 且任务状态非终态
    # (查 Multica 实际状态; 查询失败保守视为执行中)。
    _bot_fix_in_progress = False
    if req and req.get("multica_issue_id") and is_bot_fix_request(note_body):
        _done_statuses = ("done", "closed", "cancelled", "failed", "completed",
                          "canceled", "archived")
        _tsk_status = None
        try:
            _c, _o, _e = _multica_cli(
                ["issue", "get", req["multica_issue_id"], "--output", "json"],
                timeout=20)
            if _c == 0 and _o.strip():
                _d = json.loads(_o)
                _tsk = _d.get("issue", _d) if isinstance(_d, dict) else _d
                _tsk_status = str((_tsk or {}).get("status") or "").lower()
        except Exception:
            _tsk_status = None  # 查不到保守视为执行中
        if _tsk_status and _tsk_status in _done_statuses:
            pass  # 任务已结束, 允许重新触发(走下方新建)
        else:
            _bot_fix_in_progress = True
        if _bot_fix_in_progress:
            _msg = (
                f"🤖 该问题已有开发任务 "
                f"正在执行中, 处理完成后会自动同步结果。\n"
                f"请勿重复触发, 避免产生重复任务。\n"
                f"任务状态可在此跟踪: {_devflow_url(req['id'])}"
            )
            if repo and issue_number:
                safe_notify(gitcode_issue_comment, repo, issue_number,
                            bot_comment(_msg))
            return {"action": "bot_fix_in_progress",
                    "reason": "该 issue 已有开发任务在执行中, 不重复建任务",
                    "multica_issue_id": req.get("multica_issue_id"),
                    "requirement_id": req["id"]}
    if (not req or not req.get("multica_issue_id")) and is_bot_fix_request(note_body):
        _req = req
        if _req is None:
            # 2026-08-12 幂等加固: 补建前必须能定位 issue(id 或 number 至少
            # 一个)。两者皆无时拒绝补建 —— 否则 gitcode_issue_id=NULL 插入,
            # 唯一索引失效, 每次评论都新建任务(雪崩), 且 NULL 孤儿记录永远
            # 无法按 issue 查回。宁可提示失败, 不可静默重复建任务。
            if not issue_id and not issue_number:
                logger.error(
                    f"[bot-fix] payload 无法识别 issue id/number(repo={repo}), "
                    f"拒绝补建 requirement 防任务雪崩")
                if repo and issue_number:
                    safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(
                        "⚠️ 无法识别该 issue 的标识信息, 已拒绝转机器人处理, "
                        "请稍后重试或联系管理员。"))
                return {"action": "bot_fix_skip",
                        "reason": "payload 无法识别 issue id/number, 拒绝补建防任务雪崩"}
            _title = issue.get("title") or ""
            _body = issue.get("body") or ""
            _issue_url = (issue.get("html_url")
                          or f"https://gitcode.com/{repo}/issues/{issue_number}")
            _created = parse_datetime(issue.get("created_at"))
            _updated = parse_datetime(issue.get("updated_at"))
            if repo and issue_number:
                try:
                    _d = gitcode_get(f"/repos/{repo}/issues/{issue_number}")
                    _title = _title or (_d.get("title") or "")
                    _body = _body or (_d.get("body") or "")
                    _issue_url = _d.get("html_url") or _issue_url
                    _created = parse_datetime(_d.get("created_at")) or _created
                    _updated = parse_datetime(_d.get("updated_at")) or _updated
                except Exception as _e:
                    logger.warning(f"[bot-fix] 拉取 issue #{issue_number} 详情失败: {_e}")
            _title = _title or "未命名需求"
            _body = _body or ""
        else:
            _title = req.get("title") or ""
            _body = req.get("body") or ""
            _issue_url = (issue.get("html_url")
                          or f"https://gitcode.com/{repo}/issues/{issue_number}")
            _created = parse_datetime(issue.get("created_at"))
            _updated = parse_datetime(issue.get("updated_at"))
        # 2026-08-11 对齐 handle_issue 主路径(4962-4979): **先建 Multica 任务**,
        # 再一次性写 requirement(带 multica_issue_id, status=dispatched) + flow
        # (node_source done + node_preprocess done)。旧实现先建 requirement
        # (multica=None) 再 db_update + db_update_flow, 依赖 UPDATE 命中已存在
        # 行 —— flow 行缺失/时序错位时 UPDATE 静默 0 行, node_preprocess 永远
        # pending(#101 实测, 用户要求复用新增需求流程)。
        m_issue_id, m_identifier = multica_issue_create(
            _title, _body, SQUAD_DEV, force_new=True)
        m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"
        if _req is None:
            _req_id = db_insert_requirement({
                "gitcode_issue_id": issue_id, "gitcode_issue_number": str(issue_number),
                "gitcode_issue_url": _issue_url, "title": _title, "body": _body,
                "state": "opened", "author": note_author, "author_name": note_author,
                "created_at_gitcode": _created, "updated_at_gitcode": _updated,
                "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
                "multica_issue_number": m_identifier, "status": "dispatched",
                "requirement_type": "requirement", "gitcode_pr_id": None,
            })
            if not _req_id:
                return {"action": "bot_fix_skip",
                        "reason": "补建 requirement 记录失败, 无法转机器人处理"}
            _req = {"id": _req_id, "title": _title, "body": _body}
        else:
            db_update_requirement(_req["id"], {
                "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
                "multica_issue_number": m_identifier, "status": "dispatched",
            })
        db_insert_flow(_req["id"], issue_id, m_issue_id, {
            "node_source": ("done", _issue_url),
            "node_preprocess": ("done", m_issue_url),
        })
        if repo and issue_number:
            _msg = (
                f"🤖 已转为机器人修复, 任务 {m_identifier} 已派发给开发小队。\n"
                f"任务状态可在此跟踪: {_devflow_url(_req['id'])}"
            )
            # 2026-08-11 用户要求: 去掉幂等 —— 每次评论触发都回复确认,
            # 链接为新拼接的 requirementid(devflow?requirementid={id}),
            # 让用户能确认本次触发已生效(此前幂等拦截导致"评论了没回复"困惑)。
            safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(_msg))
        if ENABLE_LABELS and repo and issue_number:
            _lbl, _ = classify_issue(_title, _body)
            safe_notify(set_issue_labels, repo, issue_number,
                        [_lbl or "bug", "triaged"])
        return {"action": "bot_fix_requested", "multica_issue_id": m_issue_id,
                "multica_issue_number": m_identifier, "requirement_id": _req["id"]}

    if not req or not req.get("multica_issue_id"):
        # 2026-08-12 用户要求: 普通评论(非转机器人意图)静默跳过, 不再发
        # "未找到 Multica 任务"提示 —— bug 类型 issue 已不入库(codeowner
        # 分配只评论不建记录), 无 multica 任务时评论无需转发; 有需要转
        # 机器人处理的用户会在评论里艾特机器人(走上方转机器人分支)。
        # 2026-08-19 增强: 艾特了机器人但触发词不匹配时, 回复正确触发词
        if repo and issue_number and _mentions_bot(note_body):
                    _hint = (
                        "🤖 我收到了您的消息, 但未识别到有效的操作指令。请使用以下格式：\n\n"
                        "**重新处理** - 重新走完整流程（建任务失败/判重误关时使用）\n"
                        "  `@skills_bot 重新处理`\n\n"
                        "**转机器人修复** - 让机器人自动修复/开发\n"
                        "  `@skills_bot 帮我修复这个bug`\n"
                        "  `转机器人处理`\n\n"
                        "**关闭 issue**\n"
                        "  `请关闭`\n"
                    )
                    safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(_hint))
        return {"action": "skip", "reason": "无 Multica 任务, 评论不转发(静默)"}

    m_issue_id = req["multica_issue_id"]
    # 去重: 该评论已转发过则跳过
    if db_comment_forwarded(note_id, m_issue_id):
        return {"action": "skip", "reason": "评论已转发"}

    content = (
        f"**GitCode Issue #{issue_number} 用户评论（{note_author}）：**\n\n"
        f"{note_body}\n\n---\n> GitCode 评论 ID: {note_id}\n"
        f"{mention_squad(SQUAD_DEV, SQUAD_DEV_NAME)} 请处理上述用户反馈。"
    )
    multica_issue_comment(m_issue_id, content)

    notify = f"✅ 已将您的评论转发给开发小队处理。\n任务链接: {_devflow_url(req['id'])}"
    notify_err = None
    if repo and issue_number:
        notify_err = safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(notify))

    return {"action": "forwarded", "multica_issue_id": m_issue_id, "notify_error": notify_err}


def _test_squad_notified(multica_issue_id, pr_number):
    """该 PR 是否已向测试小队下发过测试任务(防重复 @, 供 PR 重复事件幂等)"""
    marker_prefix = f"**GitCode PR #{pr_number} 已创建**"
    try:
        for c in multica_issue_comments(multica_issue_id):
            content = c.get("content") or ""
            if marker_prefix in content and f"mention://squad/{SQUAD_TEST}" in content:
                return True
    except Exception:
        pass
    return False


# PR 处理版本 sha 记录/读取(2026-08-16 方案A, reopen 重审用)
# 处理时在 PR 评论里追加"处理版本: <sha>"标记; reopen 时对比当前 head sha,
# 变化说明文件内容有变动 -> 强制重新审查+测试, 不变则幂等跳过。
PR_SHA_MARKER = "处理版本:"


def _pr_head_sha(repo, pr_number):
    """获取 PR 当前 head 分支 commit sha; 失败返回 None(fail-open 不阻断)"""
    try:
        d = gitcode_get(f"/repos/{repo}/pulls/{pr_number}")
        if isinstance(d, dict):
            head = d.get("head") or {}
            sha = head.get("sha") or (head.get("commit_id")) or ""
            return sha or None
    except Exception as e:
        logger.warning(f"[pr-sha] 获取 PR #{pr_number} head sha 失败: {e}")
    return None


def _pr_recorded_sha(repo, pr_number):
    """读取 PR 评论中记录的处理版本 sha; 无记录返回 None。

    comments API 返回正序(旧->新), 需取**最后一个**匹配(最新处理版本)。
    """
    try:
        d = gitcode_get(f"/repos/{repo}/pulls/{pr_number}/comments?per_page=20")
        comments = d if isinstance(d, list) else []
        last = None
        for c in comments:
            cb = c.get("body") or ""
            if cb.startswith(BOT_PREFIX) and PR_SHA_MARKER in cb:
                m = re.search(rf"{re.escape(PR_SHA_MARKER)}\s*([0-9a-fA-F]+)", cb)
                if m:
                    last = m.group(1)  # 覆盖式取最后一个 = 最新
        return last
    except Exception as e:
        logger.warning(f"[pr-sha] 读取 PR #{pr_number} 记录 sha 失败: {e}")
    return None


def _pr_sha_changed(repo, pr_number):
    """reopen 事件判断文件内容是否有变动: 当前 head sha != 上次处理记录的 sha。

    无当前 sha(查询失败)或上次无记录 -> 视为有变动(保守重审, 宁重不漏)。
    """
    cur = _pr_head_sha(repo, pr_number)
    if not cur:
        return True  # 查询失败 fail-open: 保守重审
    prev = _pr_recorded_sha(repo, pr_number)
    if not prev:
        return True  # 上次未记录(可能从未完整处理) -> 重审
    # 记录时截断为 12 位(_pr_review 的 _sha_note), 对比必须同长度,
    # 否则完整 40 位 cur 恒 != 12 位 prev, 每次 reopen 都被误判"有变动"
    # (2026-08-17 PR #14 实测: 未改文件重开仍触发审查+测试)。
    return cur.lower()[:12] != prev.lower()[:12]


def _notify_test_squad(m_issue_id, req, pr_number, title, body, author_name, repo, notify_text):
    """在 Multica issue 下 @skills测试小队 处理 PR; 返回 (comment_err, notify_err)"""
    content = (
        f"**GitCode PR #{pr_number} 已创建**（{author_name}）：\n\n"
        f"{title}\n\n{body}\n\n---\n"
        f"{mention_squad(SQUAD_TEST, SQUAD_TEST_NAME)} 请对本次 PR 进行测试处理; "
        f"测试完成后系统将自动通知验证小队。"
    )
    comment_err = safe_notify(multica_issue_comment, m_issue_id, content)
    notify_err = None
    if repo and pr_number:
        notify_err = safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(notify_text))
    return comment_err, notify_err


def handle_pr(payload):
    """逻辑3/4/7: PR 创建事件"""
    pr = payload.get("pull_request") or payload.get("merge_request") or payload.get("pr") or payload
    repo = get_repo_full_name(payload)
    pr_number = pr.get("number")
    pr_id = pr.get("id")
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    pr_url = pr.get("html_url") or f"https://gitcode.com/{repo}/pull/{pr_number}"
    author_login = ((pr.get("user") or {}).get("login")) or ""
    author_name = ((pr.get("user") or {}).get("name")) or author_login

    # 逻辑7: 合入 distribute_for_github 分支的 PR(cherry-pick) -> 分发小队
    if is_distribute_pr(pr):
        return handle_distribute_pr(payload, pr, repo, pr_number, pr_id, title, body,
                                    pr_url, author_login, author_name)

    # ---- 元数据 update 直接跳过(2026-08-18 用户拍板) ----
    # add reviewer/approver/change merger/mr update 等元数据事件**不改变任何
    # 代码内容**(head sha 不变), 审查结果不会变 —— 直接跳过, 不进预校验/
    # 审查/评论(替代窗口合并方案: 窗口只是限频缓解, 根治是这类事件根本不
    # 该触发审查; PR #21/#23 实测重复评论的根因)。source update(新 commit)
    # /open/reopen 不参与(新代码必须重审)。
    _attrs0 = payload.get("object_attributes") or {}
    _is_meta_update = (
        str(_attrs0.get("action") or "").lower() in ("update", "updated")
        and str(_attrs0.get("act_desc") or "") != "source_branch_changed"
        and str(_attrs0.get("update_reason") or "") != "source update"
    )
    if _is_meta_update:
        return {"action": "skipped_meta_update",
                "reason": "元数据 update(评审人/合并人/标签等)不改变代码内容, 不触发审查"}

    # ---- PR 预校验(skill-pr 阶段0 迁移): 配置检查发现问题仅评论提醒, 不阻断 ----
    # 对齐 skill-pr 阶段0 "评论提醒...不阻塞后续流程": 标题规范(自动修复)/描述/
    # 关联Issue/标签/Assignee 等 warn 项只发提醒评论(幂等), 继续走审查报告;
    # 进测试小队的关卡是下方 PR 审查报告(fail 才阻断)。
    # 2026-08-04 修复: 此前 not ok 直接 return precheck_failed, 导致 PR #131
    # 只有预校验评论、审查报告从未生成(与 PR #124 的完整流程不一致)。
    if ENABLE_PR_PRECHECK and repo and pr_number:
        ok, issues, fixed_title = pr_precheck_and_notify(repo, pr_number, title, body)
        if fixed_title:
            title = fixed_title  # 后续流程使用修正后的标题

    # ---- PR 审查报告(skill-pr 阶段1-5 迁移): 预校验通过后输出审查报告 ----
    # 变更摘要 + Skill 审查 30 项 + 安全扫描 + 合并检查; 报告 fail 时阻断进测试,
    # pass/warn 放行(与 devops-bot 一致: 审查通过才进入测试小队)。
    # 新 commit 事件(act_desc=source_branch_changed)强制重新审查(#142 事故:
    # 第二次 commit 推送后旧报告幂等跳过, 未反映新代码; 修复后重发报告)
    # 2026-08-08 用户要求 PR 处理逻辑调整(1.1/1.2):
    #   - 机器人(skills_bot/check_bot)提交的 PR: 预检查不通过 -> 审查报告
    #     写入 multica 评论并 @skills_dev 处理; 每次 commit 二次预检查;
    #     其他情况不做任何处理(不 @skills_test)。
    #   - 开发者提交的 PR: 预检查不通过 -> 仅 @开发者 人工修改; 通过 ->
    #     @skills_test 处理 + 贴任务链接。
    _attrs = payload.get("object_attributes") or {}
    _is_new_commit = (
        str(_attrs.get("act_desc") or "") == "source_branch_changed"
        or str(_attrs.get("update_reason") or "") == "source update"
    )
    # 2026-08-16 方案A: reopen(重开)事件 —— 若 PR 文件内容有变动(head sha
    # 与上次处理时不同), 强制重新审查+重新测试; 未变动则保持幂等跳过。
    # reopen 事件本身已进入 handle_pr(handle_event 白名单), 这里只需在
    # 重开且内容变动时把 _is_new_commit 置 True, 复用新 commit 的强制路径。
    _is_reopen = str(_attrs.get("action") or "").lower() in ("reopen", "reopened")
    if _is_reopen and ENABLE_PR_REVIEW and repo and pr_number:
        try:
            if _pr_sha_changed(repo, pr_number):
                logger.info(f"[pr-reopen] PR #{pr_number} 重开且文件内容有变动(head sha 变化), 强制重新审查+测试")
                _is_new_commit = True
            else:
                logger.info(f"[pr-reopen] PR #{pr_number} 重开但文件内容未变动(head sha 相同), 幂等跳过")
        except Exception as _e:
            logger.warning(f"[pr-reopen] PR #{pr_number} sha 对比失败, 按未变动处理: {_e}")
    _pr_author_is_bot = author_login.strip().lower() in BOT_AUTHOR_LOGINS
    review_conclusion = "pass"
    if ENABLE_PR_REVIEW and repo and pr_number:
        issue_no = extract_issue_number_from_pr(pr, repo, pr_number)
        review_conclusion, _report = pr_review_and_notify(
            repo, pr_number, title, body, issue_no, force=_is_new_commit)
        if review_conclusion == "fail":
            if _pr_author_is_bot:
                # 1.1 机器人 PR 预检查不通过: 审查报告写入 multica + @skills_dev
                # 先匹配关联需求拿到 multica issue(若匹配不到则跳过 ——
                # 机器人 PR 必然关联其开发任务)
                _req_bot = None
                try:
                    _req_bot = db_find_requirement_by_pr_id(pr_number, repo)
                    if not _req_bot:
                        _ino = extract_issue_number_from_pr(pr, repo, pr_number)
                        if _ino:
                            _req_bot = db_find_requirement_by_issue_number(_ino, repo)
                except Exception as _e:
                    logger.warning(f"[pr-bot-review] 匹配需求失败: {_e}")
                if _req_bot and _req_bot.get("multica_issue_id"):
                    # 2026-08-09 #176 修复: ① 写**完整审查报告**(此前只写摘要
                    # "见 PR 评论", 用户期望完整报告在 multica); ② **幂等**:
                    # multica 评论无幂等检查, 每个 fail 事件(创建/update/新
                    # commit)都发一条, SKI-178 实测重复 5 条。发前检查该
                    # multica issue 是否已有同 PR 审查通知评论。
                    _mid = _req_bot["multica_issue_id"]
                    _marker = f"PR #{pr_number} 审查未通过"
                    _already = False
                    try:
                        _code, _out, _err = _multica_cli(
                            ["issue", "comment", "list", _mid, "--output", "json"], timeout=30)
                        if _code == 0 and _out.strip():
                            import json as _json
                            _items = _json.loads(_out)
                            _items = _items if isinstance(_items, list) else \
                                (_items.get("data") or _items.get("items") or [])
                            for _c in _items:
                                _cb = ((_c.get("body") or _c.get("content") or ""))
                                if _marker in _cb:
                                    _already = True
                                    break
                    except Exception as _e:
                        logger.warning(f"[pr-bot-review] 查 multica 评论失败: {_e}")
                    if not _already:
                        _bot_review_content = (
                            f"**GitCode PR #{pr_number} 审查未通过(机器人提交)**（{author_name}）：\n\n"
                            f"**PR 标题：** {title}\n"
                            f"**PR 链接：** {pr_url}\n\n"
                            f"---\n审查结论：{review_conclusion}（Skill 审查存在 ❌ 项）\n\n"
                            f"**审查报告全文：**\n\n{_report}\n\n"
                            f"{mention_squad(SQUAD_DEV, SQUAD_DEV_NAME)} 请处理该 PR 的审查问题。"
                        )
                        safe_notify(multica_issue_comment, _mid, _bot_review_content)
                    # 2026-08-09 用户要求: PR 下也简单回复, 让 PR 页面可见
                    # 已通知开发小队(幂等: 已回复过则不重复)
                    if not _pr_has_bot_comment(repo, pr_number, MARKERS["pr_review_notified"]):
                        safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(
                            f"PR 审查未通过, 已通知开发小队进行处理"))
                    return {"action": "review_failed_bot", "reason": "机器人 PR 审查不通过, 已写入 multica 并 @skills_dev", "conclusion": "fail"}
                return {"action": "review_failed", "reason": "机器人 PR 审查不通过但未匹配到 multica 任务", "conclusion": review_conclusion}
            # 1.2 开发者 PR 预检查不通过: 仅 @开发者 人工修改(评论已发, 追加 @作者)
            if repo and pr_number:
                try:
                    # 幂等 marker 必须与评论正文真实关键词一致(#170 实测:
                    # 旧 marker"请根据审查报告修改"不在正文中(正文是"请根据
                    # 上方 PR 审查报告修改后"), 幂等失效导致每个 update 事件
                    # 都发一条重复 @开发者 评论 —— PR #170 出现两条)
                    if not _pr_has_bot_comment(repo, pr_number, MARKERS["pr_review_fix_req"]):
                        safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(
                            f"@{author_login} 请根据上方 PR 审查报告修改后重新提交。"))
                except Exception as _e:
                    logger.warning(f"[pr-review-fail] @开发者提醒失败: {_e}")
            return {"action": "review_failed", "reason": "PR 审查报告存在 ❌ 项, 已@开发者修改(见 PR 评论), 修复后由 update 事件重新审查", "conclusion": review_conclusion}

    # 1.1 机器人 PR: 审查 pass/warn -> 评论审查通过 + @skills_dev 进行后续处理
    if _pr_author_is_bot and review_conclusion in ("pass", "warn"):
        # 匹配关联需求拿到 multica issue(同 fail 路径 L7737-7743 的模式)
        _req_pass = None
        try:
            _req_pass = db_find_requirement_by_pr_id(pr_number, repo)
            if not _req_pass:
                _ino_pass = extract_issue_number_from_pr(pr, repo, pr_number)
                if _ino_pass:
                    _req_pass = db_find_requirement_by_issue_number(_ino_pass, repo)
        except Exception as _e:
            logger.warning(f"[pr-bot-pass] 匹配需求失败: {_e}")
        if _req_pass and _req_pass.get("multica_issue_id"):
            _mid_pass = _req_pass["multica_issue_id"]
            _marker_pass = f"PR #{pr_number} 审查通过"
            _already_pass = False
            try:
                _code_pass, _out_pass, _err_pass = _multica_cli(
                    ["issue", "comment", "list", _mid_pass, "--output", "json"], timeout=30)
                if _code_pass == 0 and _out_pass.strip():
                    import json as _json_pass
                    _items_pass = _json_pass.loads(_out_pass)
                    _items_pass = _items_pass if isinstance(_items_pass, list) else \
                        (_items_pass.get("data") or _items_pass.get("items") or [])
                    for _c_pass in _items_pass:
                        _cb_pass = ((_c_pass.get("body") or _c_pass.get("content") or ""))
                        if _marker_pass in _cb_pass:
                            _already_pass = True
                            break
            except Exception as _e:
                logger.warning(f"[pr-bot-pass] 查 multica 评论失败: {_e}")
            if not _already_pass:
                _pass_content = (
                    f"## PR 审查通过\n\n"
                    f"**GitCode PR #{pr_number} 审查通过（{author_name}）**：\n\n"
                    f"**PR 标题：** {title}\n"
                    f"**PR 链接：** {pr_url}\n\n"
                    f"{mention_squad(SQUAD_DEV, SQUAD_DEV_NAME)} 请进行后续处理。"
                )
                safe_notify(multica_issue_comment, _mid_pass, _pass_content)
            # PR 下简单回复(幂等)
            _marker_pass_pr = "PR 审查通过, 已通知开发小队进行处理"
            if not _pr_has_bot_comment(repo, pr_number, _marker_pass_pr):
                safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(_marker_pass_pr))
        return {"action": "bot_pr_reviewed", "reason": "机器人 PR 审查通过, 已评论 multica 并 @skills_dev", "conclusion": review_conclusion}

    # 逻辑3: 用 PR id 匹配开发需求表(带仓库校验)
    req = db_find_requirement_by_pr_id(pr_number, repo)
    if req and req.get("multica_issue_id"):
        m_issue_id = req["multica_issue_id"]
        # 更新状态(node_repo 由 PR 合入事件点亮, 创建时不置 done; status=repoing 入库中)
        db_update_requirement(req["id"], {"status": "repoing", "gitcode_pr_id": str(pr_number)})
        # PR 创建即完成预处理, 开发阶段跳过
        db_update_flow(req["id"], "node_preprocess", "done", req.get("multica_issue_url") or f"{MULTICA_ISSUE_URL_PREFIX}/{req['multica_issue_id']}")
        db_update_flow(req["id"], "node_dev", "skipped", req.get("multica_issue_url") or f"{MULTICA_ISSUE_URL_PREFIX}/{req['multica_issue_id']}")
        # 幂等: 该 PR 已向测试小队下发过任务则不再重复 @
        # 例外: 新 commit 事件(act_desc=source_branch_changed / update_reason=
        # "source update")必须重新触发测试 —— 测试失败修复后提交的新 commit
        # 需要重新验证(2026-08-05 #140 实测: status=failed 后新 commit 被
        # 幂等拦截, @skills_test 未触发)。_is_new_commit 已在审查段计算
        if _test_squad_notified(m_issue_id, pr_number) and not _is_new_commit:
            return {"action": "matched_dup", "reason": "该 PR 已向测试小队下发过测试任务",
                    "multica_issue_id": m_issue_id, "requirement_id": req["id"]}
        notify_text = (
            f"✅ 已关联到开发任务, "
            f"已通知 {SQUAD_TEST_NAME} 进行测试处理, 测试完成后将通知 {SQUAD_VERIFY_NAME} 验证。\n"
            f"任务链接: {_devflow_url(req['id'])}"
        )
        comment_err, notify_err = _notify_test_squad(
            m_issue_id, req, pr_number, title, body, author_name, repo, notify_text)
        return {"action": "matched", "multica_issue_id": m_issue_id,
                "requirement_id": req["id"], "comment_error": comment_err,
                "notify_error": notify_err}

    # 兜底: 从 PR 内容提取关联 issue 编号再匹配(带仓库校验)
    issue_no = extract_issue_number_from_pr(pr, repo, pr_number)
    req = None
    if issue_no:
        req = db_find_requirement_by_issue_number(issue_no, repo)
        if req and req.get("multica_issue_id"):
            db_update_requirement(req["id"], {"status": "repoing", "gitcode_pr_id": str(pr_number)})
            # PR 创建即完成预处理, 开发阶段跳过
            _murl = req.get("multica_issue_url") or f"{MULTICA_ISSUE_URL_PREFIX}/{req['multica_issue_id']}"
            db_update_flow(req["id"], "node_preprocess", "done", _murl)
            db_update_flow(req["id"], "node_dev", "skipped", _murl)
            m_issue_id = req["multica_issue_id"]
            # 幂等: 已下发过测试任务则不再重复 @(新 commit 事件除外, 同逻辑3)
            if _test_squad_notified(m_issue_id, pr_number) and not _is_new_commit:
                return {"action": "matched_dup", "reason": "该 PR 已向测试小队下发过测试任务",
                        "multica_issue_id": m_issue_id, "requirement_id": req["id"]}
            notify_text = (
                f"✅ 已通过 Issue #{issue_no} 关联到开发任务, "
                f"已通知 {SQUAD_TEST_NAME} 进行测试处理, 测试完成后将通知 {SQUAD_VERIFY_NAME} 验证。\n"
                f"任务链接: {_devflow_url(req['id'])}"
            )
            comment_err, notify_err = _notify_test_squad(
                m_issue_id, req, pr_number, title, body, author_name, repo, notify_text)
            return {"action": "matched_by_issue", "multica_issue_id": m_issue_id,
                    "requirement_id": req["id"], "comment_error": comment_err,
                    "notify_error": notify_err}

    # 逻辑4: 未匹配到 -> 新建 Multica issue, 分配给 skills测试小队
    # 2026-08-16 用户要求: **不再按标题复用** —— 即使 Multica 已有同标题
    # 活跃 issue(PR 重开/重复事件/不同 PR 同标题等), 每个 PR 也必须建独立
    # 任务。旧逻辑 _find_existing_multica_issue(m_title) 按标题全等复用,
    # 导致同标题的重复 PR(如 #5/#13 同分支同 commit)共享同一个 SKI-67,
    # 两个 PR 的处理互相干扰。force_new=True 时 multica_issue_create 会给
    # CLI 加 --allow-duplicate, 撞 "Active duplicate issue exists" 校验不会
    # 崩溃(2026-08-11 issue #102 实测, 两层放行)。
    created = parse_datetime(pr.get("created_at"))
    m_title = f"[PR] {title}" if title else f"GitCode PR #{pr_number} 处理"
    m_body = (
        f"GitCode PR #{pr_number} 未关联到已有开发需求, 自动创建任务。\n\n"
        f"PR 链接: {pr_url}\n\n{body}"
    )
    m_issue_id, m_identifier = multica_issue_create(
        m_title, m_body, SQUAD_TEST, force_new=True)
    m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"

    # 入库: PR 对应 issue 数据 + PR 数据 (gitcode_issue_id 无则用 PR id 占位)
    req_id = db_insert_requirement({
        "gitcode_issue_id": pr_id or pr_number, "gitcode_issue_number": str(issue_no or pr_number),
        "gitcode_issue_url": pr_url, "title": m_title, "body": m_body,
        "state": "opened", "author": author_login, "author_name": author_name,
        "created_at_gitcode": created, "updated_at_gitcode": created,
        "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
        "multica_issue_number": m_identifier, "status": "dispatched",
        "requirement_type": "requirement", "gitcode_pr_id": str(pr_number),
    })
    db_insert_flow(req_id, pr_id or pr_number, m_issue_id, {
        "node_source": ("done", pr_url),
        "node_preprocess": ("done", m_issue_url),
        "node_dev": ("skipped", m_issue_url),
    })

    # 在 Multica issue 下发标记评论(带 "**GitCode PR #n 已创建**" + @测试小队),
    # 保证 PR update 事件重复触发时 _test_squad_notified 幂等命中
    comment_err = safe_notify(multica_issue_comment, m_issue_id,
                              f"**GitCode PR #{pr_number} 已创建**（{author_name}）：\n\n"
                              f"{title}\n\n{body}\n\n---\n"
                              f"{mention_squad(SQUAD_TEST, SQUAD_TEST_NAME)} 请对本次 PR 进行测试处理; "
                              f"测试完成后系统将自动通知验证小队。")

    notify = (
        f"✅ 未关联到已有任务, 已创建任务。\n"
        f"任务链接: {_devflow_url(req_id)}"
    )
    notify_err = None
    if repo and pr_number:
        notify_err = safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(notify))

    return {"action": "created_new", "multica_issue_id": m_issue_id,
            "multica_issue_number": m_identifier, "requirement_id": req_id,
            "comment_error": comment_err,
            "notify_error": notify_err}


def handle_pr_merged(payload):
    """逻辑3/4 补充: 普通 PR 合入事件 -> 点亮共建仓库节点(node_repo=done)

    PR 创建时不再点亮 node_repo, 合入后才视为共建仓库入库完成;
    不重复下发测试任务(测试/验证流程已完成)。
    2026-08-08 用户要求 1.4: PR 合入后按提交者区分处理:
    - 机器人(skills_bot/check_bot)提交的 PR 合入 -> 直接对关联 issue
      评论"已合入"并关闭(自动闭环);
    - 开发者提交的 PR 合入 -> PR 评论提醒创建者关注 + 关联 issue 评论
      通知作者自行关闭 issue。
    """
    pr = payload.get("pull_request") or payload.get("merge_request") or payload.get("pr") or payload
    repo = get_repo_full_name(payload)
    pr_number = pr.get("number")
    if not (repo and pr_number):
        return {"action": "ignored", "reason": "缺少 repo/pr_number"}
    author_login = ((pr.get("user") or {}).get("login")) or ""
    _is_bot = author_login.strip().lower() in BOT_AUTHOR_LOGINS
    req = db_find_requirement_by_pr_id(pr_number, repo)
    if not req or not req.get("multica_issue_id"):
        return {"action": "ignored", "reason": "PR 合入事件未匹配到开发需求",
                "pr_number": pr_number, "repo": repo}
    db_update_flow(req["id"], "node_repo", "done", repo and f"https://gitcode.com/{repo}")
    # ---- 1.4 合入后处理(2026-08-18 用户要求修订) ----
    # 无论机器人/开发者提交的 PR, 合入后都关闭关联 issue; 但 issue 关联
    # 多个 PR 且仍有 open 状态的 PR(开发还在继续) -> 不关闭, 评论说明
    # 待全部 PR 合入后关闭(开发者 PR 保留 PR 页评论提醒创建者关注)。
    issue_number = (req or {}).get("gitcode_issue_number")
    if issue_number and repo:
        try:
            if _issue_has_open_pr(repo, issue_number, pr_number):
                if not _issue_has_bot_comment(repo, issue_number,
                                              MARKERS["pr_merged_issue_open"]):
                    safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(
                        f"📌 关联的 PR #{pr_number} 已合入, 但该 issue 还有其他 PR "
                        f"在处理中, 本 issue 暂不关闭, 待全部 PR 合入后关闭。"))
            else:
                _marker = "该需求已开发完成并合入主仓"
                if not _issue_has_bot_comment(repo, issue_number, _marker):
                    close_msg = (
                        f"✅ 该需求已开发完成并合入主仓 (PR #{pr_number} 已合并)。\n"
                        f"本 issue 已关闭。"
                    )
                    safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(close_msg))
                    safe_notify(close_gitcode_issue, repo, issue_number)
                    logger.info(f"PR #{pr_number} 合入: 已评论并关闭关联 GitCode issue #{issue_number}")
        except Exception as e:
            logger.warning(f"[pr-merged] 处理关联 issue 失败: {e}")
    # 开发者 PR: 保留 PR 页评论提醒创建者关注后续
    if not _is_bot and repo and pr_number:
        try:
            if not _pr_has_bot_comment(repo, pr_number, MARKERS["pr_merged_dev_notice"]):
                safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(
                    f"✅ PR #{pr_number} 已合入, @{author_login} 请关注后续处理。"))
        except Exception as e:
            logger.warning(f"[pr-merged-dev] PR 评论提醒失败: {e}")
    return {"action": "node_repo_done", "requirement_id": req["id"],
            "multica_issue_id": req["multica_issue_id"], "pr_number": pr_number,
            "repo": repo, "author_is_bot": _is_bot}


# ==================== master/main 合并分发(devops-bot 迁移, 对齐 _dispatch_merged_pr) ====================
_merge_created_guard = {}  # pr_id -> unix ts(内存守卫, 防重复 webhook 重复创建)
_MERGE_CREATE_GUARD_WINDOW = 600


def _issue_has_open_pr(repo, issue_number, exclude_pr_number):
        """issue 是否还有其他 open 状态的关联 PR(2026-08-18 用户要求)。

        合入一个 PR 时, 若 issue 关联多个 PR 且仍有 open 的, 说明开发还在
        继续, **不关闭 issue**。查 DB 该 issue 关联的所有 PR id(排除当前),
        逐个查 PR 状态; 任一 open -> True; 无其他 PR / 查询失败 -> False
        (不阻断关闭, 保守)。
        """
        if not (repo and issue_number):
            return False
        try:
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT gitcode_pr_id FROM `{REQUIREMENT_TABLE}` "
                        f"WHERE gitcode_issue_number = %s AND gitcode_pr_id IS NOT NULL "
                        f"AND gitcode_pr_id != '' AND gitcode_pr_id != %s",
                        (str(issue_number), str(exclude_pr_number or 0)))
                    rows = cur.fetchall()
            for r in rows:
                pid = r[0]
                try:
                    d = gitcode_get(f"/repos/{repo}/pulls/{pid}")
                    if isinstance(d, dict) and (d.get("state") or "").lower() \
                            in ("open", "opened"):
                        return True
                except Exception:
                    continue  # 单个 PR 查询失败跳过, 不阻断
            return False
        except Exception as e:
            logger.warning(f"[pr-merged] 查 issue #{issue_number} 其他 PR 状态失败: {e}")
            return False


def dispatch_merged_pr(payload, pr, repo, pr_number, pr_id, title, body, pr_url):
    """逻辑8: 合入 MERGE_TARGET_BRANCH(main/master)的 PR -> 分发到分发小队。

    对齐 devops-bot skills_dev_handler._dispatch_merged_pr:
    1. 查 DB 找 multica issue(先按 pr_id, 再按 title/body 中的 #N 匹配 issue 号)
    2. 命中 -> 点亮 flow node_repo=done; 验证 multica issue 存在 ->
       已指派分发小队则跳过, 否则指派 + 评论 @分发小队
    3. 未命中/issue 不存在 -> 新建 Multica issue 分配给分发小队(守卫窗口防重复)
    4. 2026-08-07 需求: 命中关联 multica issue 时, 评论并关闭对应 GitCode issue
    5. 2026-08-08 1.4: 合入后按提交者处理关联 issue —— 机器人 PR 自动关闭,
       开发者 PR 通知作者自行关闭
    """
    _pr_author = ((pr.get("user") or {}).get("login")) or ""
    _author_is_bot = _pr_author.strip().lower() in BOT_AUTHOR_LOGINS

    def _close_related_issue(author_is_bot=False):
        """PR 合入后处理关联 GitCode issue(幂等, 2026-08-18 用户要求修订)。

        在已指派/新指派分发小队两个分支都执行:
        - **无论机器人/开发者提交的 PR, 合入后都关闭关联 issue**(原开发者
          PR 不自动关分流移除, 用户拍板)。
        - 例外: issue 关联多个 PR 且仍有 open 状态的 PR(开发还在继续) ->
          **不关闭**, 评论说明待全部 PR 合入后关闭。
        幂等靠评论 marker("该需求已开发完成并合入主仓"/"还有其他 PR 在处理中")。
        """
        issue_number = (req or {}).get("gitcode_issue_number")
        if not (repo and issue_number):
            return
        try:
            if _issue_has_open_pr(repo, issue_number, pr_number):
                # 还有其他 open 关联 PR -> 不关闭
                if not _issue_has_bot_comment(repo, issue_number,
                                              MARKERS["pr_merged_issue_open"]):
                    notify_msg = (
                        f"📌 关联的 PR #{pr_number} 已合入, 但该 issue 还有其他 PR "
                        f"在处理中, 本 issue 暂不关闭, 待全部 PR 合入后关闭。"
                    )
                    safe_notify(gitcode_issue_comment, repo, issue_number,
                                bot_comment(notify_msg))
                    logger.info(f"PR #{pr_number} 合入: issue #{issue_number} 还有其他 "
                                f"open PR, 暂不关闭")
                return
            # 无其他 open PR -> 评论 + 关闭(无论机器人/开发者 PR)
            issue_marker = "该需求已开发完成并合入主仓"
            if _issue_has_bot_comment(repo, issue_number, issue_marker):
                return  # 已评论过(重复 webhook), 跳过
            close_msg = (
                f"✅ 该需求已开发完成并合入主仓 (PR #{pr_number} 已合并)。\n"
                f"本 issue 已关闭。"
            )
            safe_notify(gitcode_issue_comment, repo, issue_number, bot_comment(close_msg))
            safe_notify(close_gitcode_issue, repo, issue_number)
            logger.info(f"PR #{pr_number} 合入: 已评论并关闭关联 GitCode issue #{issue_number}")
        except Exception as e:
            logger.warning(f"评论/关闭关联 GitCode issue #{issue_number} 失败: {e}")
    # 查 DB 拿 multica_issue_id
    req = None
    try:
        req = db_find_requirement_by_pr_id(pr_number, repo)
        if not req:
            for num in re.findall(r"#(\d+)", f"{title} {body}"):
                r = db_find_requirement_by_issue_number(num, repo)
                if r and r.get("multica_issue_id"):
                    req = r
                    logger.info(f"合并分发: PR #{pr_number} 关联 issue #{num}")
                    break
    except Exception as e:
        logger.warning(f"合并分发查DB失败: {e}")

    # 点亮共建仓库节点(有记录时)
    if req and req.get("id"):
        try:
            db_update_flow(req["id"], "node_repo", "done", f"https://gitcode.com/{repo}")
            logger.info(f"已更新 flow 共建仓库节点: requirement_id={req['id']} status=done")
        except Exception as e:
            logger.warning(f"flow 共建仓库节点更新失败: {e}")
        # 主分支合入 -> requirement.status=repoed(已入库, 2026-08-04 需求)
        # 已进入后期阶段(测试/部署等)的记录不倒退降级
        try:
            cur_status = str(req.get("status") or "")
            if cur_status not in ("repoed", "developed", "tested", "verifying",
                                  "deployed", "deploying"):
                db_update_requirement(req["id"], {"status": "repoed"})
                logger.info(f"已更新 requirement status=repoed: id={req['id']}")
        except Exception as e:
            logger.warning(f"requirement status 更新失败: {e}")
        # 主分支合入 -> 写版本表 t_skill_pr_version_history
        try:
            record_version_history(payload, pr, repo, pr_number, req)
        except Exception as e:
            logger.warning(f"版本表写入失败: {e}")

    m_issue_id = str((req or {}).get("multica_issue_id") or "")
    mention = mention_squad(SQUAD_DISTRIBUTE, SQUAD_DISTRIBUTE_NAME)
    comment_content = (
        f"**GitCode PR #{pr_number} 已合并到 {MERGE_TARGET_BRANCH}**\n\n"
        f"**PR 标题：** {title}\n"
        f"**PR 链接：** {pr_url}\n\n"
        f"---\n{mention} 请进行分发处理。"
    )

    # 有记录 -> 验证并指派
    if m_issue_id:
        found, data = multica_issue_get(m_issue_id)
        if not found:
            logger.warning(f"multica issue {m_issue_id} 不存在, 走创建兜底")
        else:
            assigned = (data.get("assignee_type") == "squad"
                        and str(data.get("assignee_id") or "") == SQUAD_DISTRIBUTE)
            if assigned:
                logger.info(f"multica issue {m_issue_id} 已指派给 {SQUAD_DISTRIBUTE_NAME}, 跳过")
                _close_related_issue(_author_is_bot)
                return {"action": "already_distributed", "multica_issue_id": m_issue_id}
            try:
                multica_issue_assign(m_issue_id, SQUAD_DISTRIBUTE)
                multica_issue_comment(m_issue_id, comment_content)
                logger.info(f"multica issue {m_issue_id} 已指派给 {SQUAD_DISTRIBUTE_NAME} 并评论")
                # 2026-08-05: 已通知分发小队 -> 标记分发进行中
                # 2026-08-13 用户要求: node_distribute_url 统一指向 Multica
                # 任务 URL(原为仓库主页, PR 链接语义由 node_source/node_repo 承担)
                if req and req.get("id"):
                    try:
                        db_update_flow(req["id"], "node_distribute", "processing",
                                       f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}")
                        db_update_requirement(req["id"], {"status": "dispatching"})
                        logger.info(
                            f"已标记分发中: requirement_id={req['id']} "
                            f"node_distribute=processing status=dispatching"
                        )
                    except Exception as e:
                        logger.warning(f"分发状态更新失败: {e}")
                _close_related_issue(_author_is_bot)
                return {"action": "distributed", "multica_issue_id": m_issue_id,
                        "requirement_id": (req or {}).get("id")}
            except Exception as e:
                logger.warning(f"指派到 {SQUAD_DISTRIBUTE_NAME} 失败: {e}")
                return {"action": "failed", "reason": str(e)[:300]}

    # 创建兜底: 记录未找到或 multica issue 不存在 -> 新建(守卫窗口防重复)
    now = int(time.time())
    if _merge_created_guard.get(str(pr_id), 0) + _MERGE_CREATE_GUARD_WINDOW > now:
        logger.info(f"PR #{pr_number} 已在守卫窗口内创建过 multica issue, 跳过")
        return {"action": "skip", "reason": "merge guard window"}
    desc = "\n".join([
        "# PR 合并分发",
        "",
        f"**来源仓库：** {repo}",
        f"**PR 编号：** #{pr_number}",
        f"**PR 标题：** {title}",
        f"**PR 链接：** {pr_url}",
        "",
        "---",
        "",
        (body.strip() or "（PR 无描述）"),
    ])
    try:
        m_issue_id, m_identifier = multica_issue_create(
            f"PR #{pr_number} 合并分发", desc, SQUAD_DISTRIBUTE)
        _merge_created_guard[pr_id] = now
        multica_issue_comment(m_issue_id, comment_content)
        logger.info(f"已新建 multica issue {m_issue_id} 并指派给 {SQUAD_DISTRIBUTE_NAME}")
        # 入库(2026-08-04 修复: 此前创建兜底分支只建 Multica issue 不写 DB,
        # 重复事件再次触发时 multica issue create 报 duplicate 且无幂等记录可查)
        created = parse_datetime(pr.get("created_at"))
        m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"
        req_id = db_insert_requirement({
            "gitcode_issue_id": pr_id or pr_number,
            "gitcode_issue_number": str(pr_number),
            "gitcode_issue_url": pr_url,
            "title": f"PR #{pr_number} 合并分发",
            "body": desc, "state": "opened", "author": "", "author_name": "",
            "created_at_gitcode": created, "updated_at_gitcode": created,
            "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
            "multica_issue_number": m_identifier, "status": "dispatching",
            "requirement_type": "distribute", "gitcode_pr_id": str(pr_number),
        })
        db_insert_flow(req_id, pr_id or pr_number, m_issue_id, {
            "node_source": ("done", pr_url),
            "node_repo": ("done", f"https://gitcode.com/{repo}"),
            "node_distribute": ("processing", m_issue_url),
        })
        logger.info(f"已入库 requirement id={req_id} (requirement_type=distribute)")
        return {"action": "created_distribute", "multica_issue_id": m_issue_id,
                "multica_issue_number": m_identifier, "requirement_id": req_id}
    except Exception as e:
        logger.warning(f"新建 multica issue 失败: {e}")
        return {"action": "failed", "reason": str(e)[:300]}


# ==================== Push-sync(devops-bot 迁移, 对齐 handle_push_event) ====================
def handle_push(payload):
    """push 事件: 仓库命中 PUSH_SYNC_CONFIG 且分支匹配时, 创建 Multica issue 分发。

    对齐 devops-bot skills_dev_handler.handle_push_event + event-routing.json push_sync。
    """
    repo = get_repo_full_name(payload)
    branch = ""
    ref = payload.get("ref", "")
    if isinstance(ref, str) and ref.startswith("refs/heads/"):
        branch = ref[11:]
    cfg = PUSH_SYNC_CONFIG.get(repo) if isinstance(PUSH_SYNC_CONFIG, dict) else None
    if not cfg:
        return {"action": "skip", "reason": f"仓库 {repo} 未配置 push_sync"}
    if branch != (cfg.get("branch") or ""):
        return {"action": "skip", "reason": f"分支 {branch!r} != 配置 {cfg.get('branch')!r}"}
    commits = payload.get("commits") or []
    head = commits[-1] if commits else {}
    sha = str(head.get("id") or "")[:8]
    msg = (str(head.get("message") or "").strip().split("\n")[0])
    actor = payload.get("user_name") or ""
    if not actor:
        user = payload.get("user") or {}
        actor = user.get("username") or user.get("login") or ""
    sha_line = f"[{sha}]({head.get('url','')})" if sha and head.get("url") else sha
    desc = "\n".join([
        "# 仓库同步通知",
        "",
        f"**来源仓库：** {repo}",
        f"**分支：** {branch}",
        f"**推送人：** {actor}",
        f"**最新 Commit：** {sha_line}",
        f"**提交信息：** {msg}",
        "",
        "---",
        "",
        "> 本任务由 Push Webhook 自动创建, 请处理。",
    ])
    try:
        m_issue_id, m_identifier = multica_issue_create(
            f"[sync] {repo} ({branch})", desc, cfg.get("squad_id"))
        return {"action": "created", "multica_issue_id": m_issue_id,
                "multica_issue_number": m_identifier, "repo": repo, "branch": branch}
    except Exception as e:
        return {"action": "failed", "reason": str(e)[:300]}


def extract_skill_name_from_pr(pr, title, body):
    """从 PR 信息提取 skills 技能名称(优先路径, 其次 huawei-cloud 命名/xxx-skill)"""
    text = f"{title or ''} {body or ''}"
    # 1. 文件路径 skills/<category>/<name>
    m = re.search(r"skills/[\w-]+/[\w-]+/([\w-]+)", text)
    if m:
        return m.group(1)
    # 2. huawei-cloud-xxx / huaweicloud-xxx 命名
    # 2026-08-07 修复 #182: [\w-]+ 的 \w 匹配中文导致贪婪吞掉后续文字
    # ("huawei-cloud-evs-list在实际使用中..." 被提取成整句)。改为仅 ASCII。
    m = re.search(r"(huawei[-_]?cloud-[a-z0-9-]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    # 3. "add skill: <name>" / "skill: <name>" 后跟的名称(必须带冒号, 避免误匹配 "skill here")
    m = re.search(r"skill[:：]\s+([a-z][\w-]*)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    # 4. xxx-skill 后缀
    m = re.search(r"([\w-]+-skill)", text)
    if m:
        return m.group(1)
    return None


def extract_skill_names(title, body):
    """提取文本中出现的**所有** skill 名称(去重, 保持出现顺序)。

    2026-08-07 判重 2.1.3 需求: 优化类需求必须给出**唯一具体** skill 名称,
    无名称或给出多个名称时需提示用户修改 issue。返回去重后的 skill 名列表:
    - []  -> 未提取到任何 skill 名
    - [a] -> 恰好一个
    - [a, b, ...] -> 多个
    """
    text = f"{title or ''} {body or ''}"
    names = []
    # 1. huawei-cloud-xxx / huaweicloud-xxx(ASCII 限定, #182 教训)
    for m in re.finditer(r"(huawei[-_]?cloud-[a-z0-9-]+)", text, re.IGNORECASE):
        n = m.group(1).lower().replace("_", "-")
        if n not in names:
            names.append(n)
    # 2. "add skill: <name>" / "skill: <name>" 后跟的名称(必须带冒号)
    for m in re.finditer(r"skill[:：]\s+([a-z][\w-]*)", text, re.IGNORECASE):
        n = m.group(1).lower()
        if n not in names:
            names.append(n)
    # 3. xxx-skill 后缀(跳过 huawei-cloud- 前缀已匹配的)
    for m in re.finditer(r"([a-z0-9][a-z0-9-]+-skill)", text, re.IGNORECASE):
        n = m.group(1).lower()
        if n not in names:
            names.append(n)
    # 2026-08-13 #113 实测: 评测问题单的**问题编号**(huawei-cloud-monitoring-
    # query-ISSUE-001)会被规则 1 误提取为 skill 名 —— 编号不是 skill, 统一
    # 过滤 -issue-\d+ 结尾的匹配, 否则 2.1.3 优化类"多 skill 名"误拦截。
    return [n for n in names if not re.search(r"-issue-\d+$", n, re.IGNORECASE)]


def find_requirement_by_skill(skill_name, req_type="requirement", prefer_skill_name=False):
    """按技能名称在 requirement 各字段(skill_name/title/body)匹配最近的开发需求。

    req_type: 默认 "requirement"(普通需求, 逻辑3c/逻辑7 用); 传 None 匹配
    所有类型 —— 2026-08-07 #187 修复: _is_bot_developed_skill 需要能查到
    distribute 类型的开发记录(huawei-cloud-evs-list 的开发记录是 PR #148
    合入产生的 distribute 记录, 旧逻辑只查 requirement 查不到 -> 误判
    非机器人开发, bug 分流失效)。
    prefer_skill_name: True 时 skill_name 精确匹配的记录排序优先(2026-08-07
    #189 修复: 同标题 issue 的 need_info 噪音记录(skill_name=NULL, 仅
    title/body 命中)按 updated_at 更新会插队到真正的开发记录之前,
    _is_bot_developed_skill 取到噪音记录 -> multica_issue_id 为空 ->
    误判非机器人开发, bug 分流错误走 codeowner 人工分配)。
    """
    if not skill_name:
        return None
    like = f"%{skill_name}%"
    type_clause = "requirement_type='requirement' AND " if req_type == "requirement" else ""
    if prefer_skill_name:
        # MySQL 布尔表达式(skill_name = %s)为 True 的记录排最前
        order = "(skill_name = %s) DESC, updated_at DESC"
    else:
        order = "updated_at DESC"
    sql = (
        f"SELECT * FROM `{REQUIREMENT_TABLE}` "
        f"WHERE {type_clause}"
        "(skill_name = %s OR skill_name LIKE %s OR title LIKE %s OR body LIKE %s) "
        f"ORDER BY {order} LIMIT 1"
    )
    params = [skill_name, like, like, like]
    if prefer_skill_name:
        params.append(skill_name)
    with db_connect() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()


# 机器人自动开发产出后视为"已上线"的终态(2026-08-07 需求): 只有这些状态
# 的 skill 才认定是机器人开发并已交付 —— bug 修复类 issue 才应走修复开发
# 流程(复用自动流水线); 开发中/未上线的 skill 出问题按 codeowner 人工分配。
BOT_DEPLOYED_STATUSES = ("repoed", "tested", "deployed", "deploying", "dispatching",
                         "dispatched", "in_progress")


def _is_bot_developed_skill(skill_ref):
    """判断 skill 是否为机器人自动开发且已上线(2026-08-07 新增)。

    bug 类 issue 分流判据: requirement 表有该 skill 的开发记录 +
    有 multica_issue_id(走过自动流水线) + status 为已上线终态
    (repoed/tested/deployed 等)。返回 (is_bot, dev_req)。
    """
    if not skill_ref:
        return False, None
    try:
        # #188 修复(2026-08-07): 不能只取"最近一条"记录 —— 同标题重复
        # issue 的 need_info 记录(无 multica, updated_at 更新)会按
        # updated_at DESC 排在机器人开发记录(id=439 distribute/deployed)
        # 之前, 导致误判 bot_developed=False, 重复提交的 bug 报告被错误
        # 走 codeowner 人工分配(评论"非 Multica 自动开发"与事实不符)。
        # 改为遍历该 skill 的全部匹配记录: 任一满足「有 multica 任务 +
        # status 为已上线终态」即认定该 skill 是机器人开发并已交付。
        like = f"%{skill_ref}%"
        sql = (
            f"SELECT * FROM `{REQUIREMENT_TABLE}` "
            "WHERE skill_name = %s OR skill_name LIKE %s "
            "OR title LIKE %s OR body LIKE %s "
            "ORDER BY updated_at DESC"
        )
        with db_connect() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, (skill_ref, like, like, like))
                rows = cur.fetchall()
        if not rows:
            return False, None
        for r in rows:
            if (r.get("multica_issue_id")
                    and str(r.get("status") or "").lower() in BOT_DEPLOYED_STATUSES):
                return True, r
        # 无机器人开发记录: 返回最近一条(可能是 need_info 等)供调用方参考
        return False, rows[0]
    except Exception as e:
        logger.warning(f"[bot-skill] 判定机器人开发 skill 失败: {e}")
        return False, None


def _skill_has_bot_contributor(repo, skill_name):
    """判断仓库中该 skill 的贡献者是否包含机器人账号(2026-08-07 判重 2.1.3)。

    用户要求: 优化类需求识别到具体 skill 名称后, 判断相关 skill 的贡献者
    是否包含 skills_bot —— 含则走优化 skill 的逻辑(机器人自动开发, 可自动
    优化), 否则走分配责任人逻辑(开发者维护, 需人工分配)。
    实测: 机器人开发的 skill 由 **check_bot**(git 提交账号)提交, skills_bot
    是 API 评论账号 —— 两者都在 BOT_AUTHOR_LOGINS 机器人账号集, 统一按
    BOT_AUTHOR_LOGINS 判断。
    判断路径(任一命中即 True):
      1. 从 _get_repo_products 拿 skill 路径, 查该路径提交历史
         (GET /commits?path=...), **任一提交者 login 命中 BOT_AUTHOR_LOGINS
         即判定**(2026-08-11 用户要求, 撤销 #243 占比规则 —— 只要有机器人
         开发过/提交过代码就走自动修复流水线);
      2. 路径拿不到/提交历史为空时, 退化查 PR 历史(pulls state=all 最近
         30 条): 标题/body 含该 skill 名且作者 login 命中 BOT_AUTHOR_LOGINS
         (覆盖 products 限流/不完整场景, 2026-08-07 实测)。
    失败/查不到返回 False(保守: 非机器人开发走人工分配)。
    返回 bool。
    """
    if not repo or not skill_name:
        return False
    skill_name = skill_name.lower().replace("_", "-")
    path = None
    commit_history_unavailable = False
    has_empty_author = False
    try:
        products = _get_repo_products(repo)
        path = products.get(skill_name)
        if not path:
            # 宽松: 名字包含关系(如传入 huawei-cloud-vpc-list 而目录名一致)
            for n, p in products.items():
                if n == skill_name or skill_name in n or n in skill_name:
                    path = p
                    break
    except Exception as e:
        logger.warning(f"[bot-contrib] 获取 skill 路径失败: {e}")
    if not path:
        commit_history_unavailable = True
    if path:
        try:
            _authors = _mirror_commit_authors(repo, path)
            if _authors is not None:
                # 2026-08-18 本地化: git log 替代 commits API(无 429)。
                # 本地只有 author name/email(无 login), 用 _mirror_author_is_bot
                # 匹配(name/email 命中 BOT_AUTHOR_LOGINS 或含 bot)。
                bot_cnt = 0
                empty_author = False
                for n, e in _authors:
                    if not (n or e):
                        empty_author = True
                        has_empty_author = True
                    elif _mirror_author_is_bot(n, e):
                        bot_cnt += 1
                if bot_cnt >= 1:
                    return True
                if not empty_author:
                    return False
            else:
                commits = gitcode_get(f"/repos/{repo}/commits?path={path}&per_page=100")
                commits = commits if isinstance(commits, list) else []
                if commits:
                    # 2026-08-11 用户要求: **撤销 #243 占比规则, 任一 bot 提交即判定**。
                    # 旧规则"bot 提交占比 >=50% 才算 bot 开发"(2026-08-09 #243)会把
                    # "bot 开发为主、人类仅少量协助"的 skill 因占比被拉低而误判人工
                    # 分配。回归保守语义: 只要有机器人开发过/提交过代码就走自动修复
                    # 流水线(宁走自动修复, 不误判人工分配)。
                    bot_cnt = 0
                    empty_author = False
                    for c in commits:
                        author = ((c.get("author") or {}).get("login")) or ""
                        if not author:
                            empty_author = True  # API 对部分 bot 提交不返回 login
                            has_empty_author = True
                        elif author.lower() in BOT_AUTHOR_LOGINS:
                            bot_cnt += 1
                    if bot_cnt >= 1:
                        return True
                    # 无 bot 提交: 若存在"作者为空"的提交(疑似 bot 但 API 不返回
                    # login), 继续走 PR 历史退化确认; 否则直接判非 bot。
                    # (2026-08-09 #243 实测: maas-tokens-usage 首个提交作者为空,
                    # 实际由 PR #164 skills_bot 创建)
                    if not empty_author:
                        return False
        except Exception as e:
            logger.warning(f"[bot-contrib] 查询提交历史失败: {e}")
            commit_history_unavailable = True
    # 退化: 查 PR 历史(该 skill 的 PR 作者是否机器人)
    # 2026-08-11(新规则): 仅在提交历史**不可用/未明确判定**(路径拿不到、查询
    # 失败、无 bot 提交且存在空作者提交需 PR 确认)时退化; 提交历史已明确判定
    # (无 bot 提交且无空作者)时直接判非 bot, 不再退化。
    if path and not commit_history_unavailable and not has_empty_author:
        return False
    try:
        prs = gitcode_get(f"/repos/{repo}/pulls?state=all&per_page=30&sort=updated&direction=desc")
        prs = prs if isinstance(prs, list) else []
        for p in prs:
            p_title = (p.get("title") or "").lower()
            p_body = (p.get("body") or "").lower()
            if skill_name in p_title or skill_name in p_body:
                author = ((p.get("user") or {}).get("login")) or ""
                if author and author.lower() in BOT_AUTHOR_LOGINS:
                    return True
    except Exception as e:
        logger.warning(f"[bot-contrib] 查询 PR 历史失败: {e}")
    return False


def _skill_primary_contributor(repo, skill_name):
    """从提交历史推断该 skill 的主要人工开发者(2026-08-11 用户要求)。

    背景: codeowner 分配依赖仓库配置的映射 issue(skillspackage 未配置 ->
    "已跳过", issue #101 实测卡死)。而提交历史是现成事实 —— 谁写了这个
    skill 一目了然(huawei-cloud-computing-query 提交者 Jason7602×3)。
    策略: 复用 _skill_has_bot_contributor 同源路径(products 定位 + commits
    历史), 统计**非机器人**提交者按提交次数排序, 返回提交最多的 login。
    返回 None 表示推断不出(查不到路径/无提交/全是 bot) -> 调用方回退
    codeowner 映射。
    """
    if not repo or not skill_name:
        return None
    skill_name = skill_name.lower().replace("_", "-")
    path = None
    try:
        products = _get_repo_products(repo)
        path = products.get(skill_name)
        if not path:
            for n, p in products.items():
                if n == skill_name or skill_name in n or n in skill_name:
                    path = p
                    break
    except Exception as e:
        logger.warning(f"[primary-contrib] 获取 skill 路径失败: {e}")
    if not path:
        # 2026-08-11 优化: products 拿不到路径(限流/无缓存)时, **按 skill 名
        # 直接探测**常见路径结构 —— 全量扫描 12 类目在 429 时耗 60s+。
        # 探测顺序: 优先 skill 名自带的产品类目(computing/compute 等, 大概率
        # 命中, 1-2 次调用), 再试通用类目 + ops 子目录。
        _first_cats = []
        for _kw in ("computing", "compute", "ecs", "bms", "as", "ims", "iam"):
            if _kw in skill_name:
                _first_cats.append(_kw)
        _cats = list(dict.fromkeys(_first_cats + [
            "ai", "bigdata", "bss", "cdi", "compute", "computing",
            "container", "database", "devtools", "monitoring",
            "network", "security", "solution", "storage"]))
        for _cat in _cats:
            for _sub in ("", "ops"):
                _probe = f"skills/{_cat}/{_sub}/{skill_name}" if _sub \
                    else f"skills/{_cat}/{skill_name}"
                # 2026-08-18 本地化: git log 探测(无 429), 失败回退 commits API
                _pa = _mirror_commit_authors(repo, _probe, limit=5)
                if _pa is not None:
                    if _pa:
                        path = _probe
                        logger.info(f"[primary-contrib] 探测到路径: {_probe}")
                        break
                else:
                    try:
                        _d = gitcode_get(
                            f"/repos/{repo}/commits?path={_probe}&per_page=5")
                        if isinstance(_d, list) and _d:
                            path = _probe
                            logger.info(f"[primary-contrib] 探测到路径: {_probe}")
                            break
                    except Exception:
                        pass
            if path:
                break
    if not path:
        return None
    # 2026-08-18 本地化: git log 统计提交者(无 429); 本地只有 author name
    # (无 login), 返回 name 作为开发者标识 —— 调用方用其匹配 GitCode 账号,
    # 本地 name 通常是提交者 git config user.name(与账号名一致); 镜像不可用
    # 回退 commits API(login 语义)。
    _authors = _mirror_commit_authors(repo, path)
    if _authors is not None:
        counts = {}
        for n, e in _authors:
            if not n or _mirror_author_is_bot(n, e):
                continue  # 跳过空作者与机器人账号
            counts[n] = counts.get(n, 0) + 1
        if not counts:
            return None
        primary = max(sorted(counts), key=counts.get)
        logger.info(f"[primary-contrib] skill {skill_name} 主要开发者: {primary} "
                    f"({counts[primary]} 次提交, 本地 git log)")
        return primary
    try:
        commits = gitcode_get(f"/repos/{repo}/commits?path={path}&per_page=100")
        commits = commits if isinstance(commits, list) else []
    except Exception as e:
        logger.warning(f"[primary-contrib] 查询提交历史失败: {e}")
        return None
    counts = {}
    for c in commits:
        author = ((c.get("author") or {}).get("login")) or ""
        if not author or author.lower() in BOT_AUTHOR_LOGINS:
            continue  # 跳过空作者与机器人账号
        counts[author] = counts.get(author, 0) + 1
    if not counts:
        return None
    # 提交次数最多者为主要开发者; 并列时按登录名稳定排序
    # 2026-08-12 用户要求: **不做跨仓兜底**(测试环境, 现网无跨仓) ——
    # 只统计当前仓库提交历史, 不查源仓库(developer-skill/huaweicloud-skills)。
    primary = max(sorted(counts), key=counts.get)
    logger.info(f"[primary-contrib] skill {skill_name} 主要开发者: {primary} "
                f"({counts[primary]} 次提交)")
    return primary


def _repo_has_skill(repo, skill_name, exact=False):
    """判断仓库中是否存在该 skill 目录(2026-08-08 判重 2.2.2 新增)。

    两层判断:
    1. _get_repo_products(缓存 10 分钟)查 skill 名是否在仓库已开发清单中
       (精确匹配 + 宽松包含匹配);
    2. 清单未命中时, 直接查 contents API 确认该 skill 目录确实不存在
       (2026-08-08 限流健壮性: products 清单可能因 429 不完整, 直接查
       目录路径兜底, 避免误判"仓库无此 skill")。
    2026-08-09 #235 修复: exact=True 时**仅精确匹配**(目录名 == skill_name,
    不做包含匹配) —— 2.2 问题类分支用精确匹配判定
    "用户给出的 skill 名是否完整有效"。宽松匹配会把用户随意写的前缀
    (如 "huawei-cloud-ascend" 想表达 ascend 系列)误当成完整 skill 名,
    匹配到仓库的 huawei-cloud-ascend-command, 导致本应 2.2.1 提示"提供
    完整 skill 名称"的 issue 错误走贡献者判断/codeowner。
    返回 bool。
    """
    if not repo or not skill_name:
        return False
    skill_name = skill_name.lower().replace("_", "-")

    def _exact_hit(name):
        name = (name or "").lower().replace("_", "-")
        return name == skill_name

    # 1. 缓存清单查
    try:
        products = _get_repo_products(repo)
        if skill_name in products:
            return True
        if not exact:
            for n in products:
                if skill_name in n or n in skill_name:
                    return True
    except Exception as e:
        logger.warning(f"[repo-has-skill] 查询仓库 skill 清单失败: {e}")
    # 2. 直接查 contents 兜底(遍历类目, 找 skill 名目录)
    # 2026-08-11 对齐 _get_repo_products 类目探测修复(issue #101): 旧固定列表
    # 只有 computing(复数), 而 skillspackage 实际用 compute(单数), 导致兜底
    # 永远扫不到 compute/ 下 skill, 误判"仓库无此 skill"。必须同时含两个。
    # 2026-08-18 本地化: 优先本地镜像全量映射匹配(products 已本地完整构建,
    # 直接查 key 等价于全类目遍历), 本地不可用再回退 API 遍历。
    _local_products = _mirror_products(repo)
    if _local_products is not None:
        for n in _local_products:
            if _exact_hit(n) if exact else (n == skill_name or skill_name in n or n in skill_name):
                return True
        return False  # 本地全量清单未命中 -> 确定不存在(无限流不完整问题)
    _all_failed = True
    for cat in ("ai", "bigdata", "bss", "cdi", "compute", "computing",
                "container", "devtools", "monitoring", "network", "security",
                "solution", "storage"):
        try:
            data = gitcode_get(f"/repos/{repo}/contents/skills/{cat}?per_page=100")
            items = data if isinstance(data, list) else []
            _all_failed = False
            for it in items:
                if it.get("type") != "dir":
                    continue
                n = it["name"].lower().replace("_", "-")
                if _exact_hit(n) if exact else (n == skill_name or skill_name in n or n in skill_name):
                    return True
                # 二级子目录(如 ops/huawei-cloud-vpc-list)
                try:
                    data2 = gitcode_get(f"/repos/{repo}/contents/skills/{cat}/{it['name']}?per_page=100")
                    items2 = data2 if isinstance(data2, list) else []
                    for it2 in items2:
                        if it2.get("type") == "dir":
                            n2 = it2["name"].lower().replace("_", "-")
                            if _exact_hit(n2) if exact else (n2 == skill_name or skill_name in n2 or n2 in skill_name):
                                return True
                except Exception:
                    continue
        except Exception:
            continue
    # 2026-08-08 限流保护: 12 类目全部请求失败(全局限流)时, 不能确认
    # "仓库无此 skill"(可能只是查不到)。返回 None 表示"无法确认"。
    if _all_failed:
        return None
    return False


def _distribute_notified(multica_issue_id, pr_number):
    """该分发 PR 是否已在 multica issue 上 @过分发小队(防重复)"""
    marker = f"**{DISTRIBUTE_BRANCH} 分支分发任务**（GitCode PR #{pr_number}"
    try:
        for c in multica_issue_comments(multica_issue_id):
            if marker in (c.get("content") or ""):
                return True
    except Exception:
        pass
    return False


def handle_distribute_pr(payload, pr, repo, pr_number, pr_id, title, body,
                         pr_url, author_login, author_name):
    """逻辑7: 合入 distribute_for_github 分支的 PR(cherry-pick)
    优先按技能名称匹配原开发需求 -> 在现有 multica issue 上评论 @分发小队(持续处理);
    匹配不到才新建 multica issue 分配给分发小队"""
    # 幂等: 该分发 PR 已处理过则跳过
    exist = db_find_requirement_by_pr_id(pr_number, repo)
    if exist and exist.get("multica_issue_id"):
        return {"action": "skip", "reason": "分发 PR 已处理", "requirement_id": exist["id"]}

    created = parse_datetime(pr.get("created_at"))

    # 1. 提取技能名称, 2. 到 requirement 各字段匹配原开发需求
    skill_name = extract_skill_name_from_pr(pr, title, body)
    matched = find_requirement_by_skill(skill_name) if skill_name else None

    if matched and matched.get("multica_issue_id"):
        # 3. 匹配到: 在现有 multica issue 上评论 @分发小队(持续处理, 不新建任务)
        m_issue_id = matched["multica_issue_id"]
        if _distribute_notified(m_issue_id, pr_number):
            return {"action": "skip", "reason": "该分发 PR 已在原任务上 @过分发小队",
                    "multica_issue_id": m_issue_id, "requirement_id": matched["id"]}
        content = (
            f"**{DISTRIBUTE_BRANCH} 分支分发任务**（GitCode PR #{pr_number}, cherry-pick, "
            f"skill: {skill_name}）：\n\n{title}\n\n{body}\n\n---\n"
            f"{mention_squad(SQUAD_DISTRIBUTE, SQUAD_DISTRIBUTE_NAME)} "
            f"请将 skill `{skill_name}` 分发到 GitHub 平台。"
        )
        comment_err = safe_notify(multica_issue_comment, m_issue_id, content)
        # 记录分发进展到 flow(node_distribute 节点)
        # 2026-08-13 用户要求: node_distribute_url 统一指向 Multica 任务 URL
        # (原为 PR URL, PR 链接语义由 node_source 承担)
        db_update_flow(matched["id"], "node_distribute", "in_progress",
                       f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}")
        notify = (
            f"✅ 已在原任务上 @{SQUAD_DISTRIBUTE_NAME} "
            f"处理 {DISTRIBUTE_BRANCH} 分支分发(skill: {skill_name})。\n"
            f"任务链接: {_devflow_url(matched['id'])}"
        )
        notify_err = None
        if repo and pr_number:
            notify_err = safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(notify))
        return {"action": "distribute_matched", "multica_issue_id": m_issue_id,
                "requirement_id": matched["id"], "skill_name": skill_name,
                "comment_error": comment_err, "notify_error": notify_err}

    # 4. 匹配不到 -> 新建 Multica issue, 分配给 skills分发小队
    m_title = f"[DISTRIBUTE] {title}" if title else f"GitCode PR #{pr_number} 分发处理"
    m_body = (
        f"GitCode PR #{pr_number} 已合入 {DISTRIBUTE_BRANCH} 分支(cherry-pick), "
        f"需要分发到 GitHub 平台。\n\nPR 链接: {pr_url}\n\n{body}"
    )
    m_issue_id, m_identifier = multica_issue_create(m_title, m_body, SQUAD_DISTRIBUTE)
    m_issue_url = f"{MULTICA_ISSUE_URL_PREFIX}/{m_issue_id}"

    req_id = db_insert_requirement({
        "gitcode_issue_id": pr_id or pr_number, "gitcode_issue_number": str(pr_number),
        "gitcode_issue_url": pr_url, "title": m_title, "body": m_body,
        "skill_name": skill_name, "state": "opened", "author": author_login,
        "author_name": author_name,
        "created_at_gitcode": created, "updated_at_gitcode": created,
        "multica_issue_id": m_issue_id, "multica_issue_url": m_issue_url,
        "multica_issue_number": m_identifier, "status": "dispatched",
        "requirement_type": "distribute", "gitcode_pr_id": str(pr_number),
    })
    db_insert_flow(req_id, pr_id or pr_number, m_issue_id, {
        "node_source": ("done", pr_url),
        "node_repo": ("done", repo and f"https://gitcode.com/{repo}"),
    })

    notify = (
        f"✅ 未匹配到原开发任务, 已创建分发任务 "
        f"(skill: {skill_name or '未知'}), 分配给 {SQUAD_DISTRIBUTE_NAME} 处理。\n"
        f"状态链接: {_devflow_url(req_id)}"
    )
    notify_err = None
    if repo and pr_number:
        notify_err = safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(notify))

    return {"action": "distribute_created", "multica_issue_id": m_issue_id,
            "multica_issue_number": m_identifier, "requirement_id": req_id,
            "skill_name": skill_name, "notify_error": notify_err}


# 评论触发"重新扫描 PR"意图(2026-08-11 用户要求):
# **必须同时**满足: ① 艾特机器人(@skills_bot/@check_bot 或"机器人"字样);
# ② 含重新扫描关键字(重新扫描/重新审查/重新检查/重扫)。两者缺一不可,
# 其他评论一律拒绝(维持"仅处理测试报告"现状)。
PR_RESCAN_AT_RE = re.compile(
    r"@?skills_bot|@?check_bot|机器人", re.IGNORECASE)
# 2026-08-18 扩展: 并入 issue 侧重试类词(重试/重新处理/重新评估等, 对齐
# ISSUE_RETRY_RE) —— PR #22 实测: 用户评论"重新处理"未触发(词表只有重扫类),
# 需再评论"重扫"才生效, 语义不一致。
PR_RESCAN_KEY_RE = re.compile(
    r"重新扫描|重新审查|重新检查|重扫|再扫|重新评审|重新评估|重新生成报告|"
    r"重试|重新处理|重新触发|重新走|再试|重跑|重新看|再处理一次|重来", re.IGNORECASE)
# 重扫限频(2026-08-11 用户要求): 1 分钟内同一 PR 只能触发一次重扫。
# 用内存字典记录最近重扫时间; 超过窗口允许再次触发(旧的永久幂等改为限频,
# 否则确认评论挂 PR 上后永远无法再触发 —— 17:37 实测被永久幂等拦截)。
PR_RESCAN_WINDOW = 60  # 秒
_pr_rescan_last = {}  # {(repo, pr_number): last_ts}


def _pr_rescan_throttled(repo, pr_number):
    """判断该 PR 是否在限频窗口内(1 分钟内已重扫过)。窗口外返回 False(可触发)。"""
    key = (repo or "", str(pr_number or ""))
    now = time.time()
    last = _pr_rescan_last.get(key)
    _pr_rescan_last[key] = now
    return last is not None and (now - last) < PR_RESCAN_WINDOW


def is_rescan_request(note_body):
    """判断评论是否为"艾特机器人 + 重新扫描关键字"的触发(2026-08-11)。

    命中 -> handle_pr_comment 强制重新审查(force=True), 按创建者分流。
    空/异常 fail-open 返回 False(不误触发)。
    """
    try:
        return bool(note_body) and bool(PR_RESCAN_AT_RE.search(note_body)) \
            and bool(PR_RESCAN_KEY_RE.search(note_body))
    except Exception:
        return False


def handle_pr_comment(payload):
    """逻辑5(2026-08-04 调整): PR 评论事件 -> 仅处理测试报告评论。

    用户要求: ① 判断评论是否是该 PR 相关的测试报告; ② 若不是 -> 不做任何
    处理(不再转发 Multica, 避免打扰测试小队); ③ 若是测试报告 -> 回复确认
    并提醒 PR 创建者关注测试结果(不转发回 Multica, 测试报告本就是测试小队
    的产出, #126 实测: 测试报告被旧逻辑转发回 Multica 是多余的)。
    """
    note = payload.get("note") or {}
    pr = payload.get("pull_request") or payload.get("merge_request") or {}
    repo = get_repo_full_name(payload)
    pr_number = pr.get("number") or payload.get("pr_number")
    note_id = note.get("id")
    note_body = note.get("body") or ""
    note_author = ((note.get("author") or {}).get("login")) or ""

    if note_body.startswith(BOT_PREFIX):
        return {"action": "skip", "reason": "程序自身评论"}

    # ②(2026-08-11 新增) 评论触发"重新扫描"：艾特机器人 + 重新扫描关键字
    # 两者缺一不可; 命中 -> force 重扫 + 按创建者分流(fail: 机器人 PR @skills_dev
    # / 开发者 PR @开发者; pass/warn: 机器人 PR 不处理 / 开发者 PR 走测试)。
    # 幂等: 评论 marker 防刷屏。
    if is_rescan_request(note_body):
        if not (repo and pr_number):
            return {"action": "skip", "reason": "缺少 repo/pr_number, 无法重扫"}
        if not ENABLE_PR_REVIEW:
            return {"action": "skip", "reason": "PR 审查功能未启用(ENABLE_PR_REVIEW=0)"}
        pr_title = pr.get("title") or ""
        pr_body = pr.get("body") or ""
        pr_author = (pr.get("author") or pr.get("user") or {}).get("login") \
            or pr.get("author_login") or ""
        _is_bot_author = pr_author.strip().lower() in BOT_AUTHOR_LOGINS
        # 限频(2026-08-11 用户要求): 1 分钟内同一 PR 只能触发一次重扫
        if _pr_rescan_throttled(repo, pr_number):
            return {"action": "skip",
                    "reason": f"该 PR 在 {PR_RESCAN_WINDOW}s 限频窗口内已重扫过, 请稍后再试"}
        _issue_no = extract_issue_number_from_pr(pr, repo, pr_number)
        # 2026-08-18 用户要求: 重扫路径补齐预校验(对齐 handle_pr 主流程
        # 顺序"预校验 -> 审查")—— PR #22 实测: 重扫只重发审查报告, 标题
        # 自动修正/配置检查评论缺失, 配置检查只能由后续事件补发(时间落在
        # 审查报告后, 顺序颠倒)。pr_precheck_and_notify 内部幂等: 已有
        # "PR 配置检查"评论则跳过提醒; 标题修正照常执行(chore/deps 等
        # PR_TITLE_SKIP_PREFIX 豁免前缀不改)。
        if ENABLE_PR_PRECHECK:
            try:
                _ok0, _issues0, _fixed0 = pr_precheck_and_notify(
                    repo, pr_number, pr_title, pr_body)
                if _fixed0:
                    pr_title = _fixed0  # 审查报告用修正后的标题
            except Exception as _e:
                logger.warning(f"[pr-comment-rescan] 预校验执行异常(不阻断重扫): {_e}")
        _conclusion, _report = pr_review_and_notify(
            repo, pr_number, pr_title, pr_body, _issue_no, force=True)
        if _conclusion == "fail":
            # 按创建者分流(与 handle_pr fail 分支一致)
            if _is_bot_author:
                _req_bot = None
                try:
                    _req_bot = db_find_requirement_by_pr_id(pr_number, repo)
                    if not _req_bot and _issue_no:
                        _req_bot = db_find_requirement_by_issue_number(_issue_no, repo)
                except Exception as _e:
                    logger.warning(f"[pr-comment-rescan] 匹配需求失败: {_e}")
                if _req_bot and _req_bot.get("multica_issue_id"):
                    _mid2 = _req_bot["multica_issue_id"]
                    _marker2 = f"PR #{pr_number} 审查未通过"
                    _already2 = False
                    try:
                        _code2, _out2, _err2 = _multica_cli(
                            ["issue", "comment", "list", _mid2, "--output", "json"], timeout=30)
                        if _code2 == 0 and _out2.strip():
                            import json as _json2
                            _items2 = _json2.loads(_out2)
                            _items2 = _items2 if isinstance(_items2, list) else \
                                (_items2.get("data") or _items2.get("items") or [])
                            for _c2 in _items2:
                                if _marker2 in ((_c2.get("body") or _c2.get("content") or "")):
                                    _already2 = True
                                    break
                    except Exception:
                        pass
                    if not _already2:
                        _bot_review_content2 = (
                            f"**GitCode PR #{pr_number} 审查未通过(机器人提交, 评论触发重扫)**"
                            f"（{pr_author}）：\n\n"
                            f"**PR 标题：** {pr_title}\n"
                            f"**PR 链接：** {pr.get('html_url') or f'https://gitcode.com/{repo}/pull/{pr_number}'}\n\n"
                            f"---\n审查结论：fail\n\n"
                            f"**审查报告全文：**\n\n{_report}\n\n"
                            f"{mention_squad(SQUAD_DEV, SQUAD_DEV_NAME)} 请处理该 PR 的审查问题。"
                        )
                        safe_notify(multica_issue_comment, _mid2, _bot_review_content2)
                if not _pr_has_bot_comment(repo, pr_number, MARKERS["pr_review_notified"]):
                    safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(
                        "PR 审查未通过, 已通知开发小队进行处理"))
            else:
                if not _pr_has_bot_comment(repo, pr_number, MARKERS["pr_review_fix_req"]):
                    safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(
                        f"@{pr_author} 请根据上方 PR 审查报告修改后重新提交。"))
        # 确认评论(含幂等 marker)
        _confirm = f"🔄 已按您的要求重新扫描(PR #{pr_number})。审查结论: {_conclusion}。"
        if _conclusion == "fail":
            _confirm += " 已按创建者分流通知处理。"
        elif _conclusion == "pass":
            _confirm += " 审查通过。"
        else:
            _confirm += " 需修改(⚠️), 详见上方报告。"
        safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(_confirm))
        return {"action": "rescan_requested", "conclusion": _conclusion,
                "pr_number": pr_number, "note_id": note_id}

    # ① 判断是否为该 PR 相关的测试报告
    if not is_test_report(note_body):
        return {"action": "skip", "reason": "非测试报告评论, 不做处理"}

    # ② 测试报告 -> 回复 + 提醒 PR 创建者关注(幂等)
    pr_author = (pr.get("author") or pr.get("user") or {}).get("login") \
        or pr.get("author_login") or ""
    mention = f"@{pr_author}" if pr_author else "PR 创建者"
    marker = "已收到该 PR 的测试报告"
    if repo and pr_number:
        if _pr_has_bot_comment(repo, pr_number, marker):
            return {"action": "skip", "reason": "测试报告确认已回复过"}
        msg = (
            f"📋 {marker}, {mention} 请关注测试结果。\n"
            f"> 若测试未通过, 请根据失败用例修复后重新提交。"
        )
        safe_notify(gitcode_pr_comment, repo, pr_number, bot_comment(msg))

    return {"action": "test_report_ack", "pr_number": pr_number,
            "note_id": note_id, "note_author": note_author}


# ==================== 测试→验证 交接轮询 ====================
HANDOFF_MARKER = "测试小队已完成处理"


def _handoff_done(multica_issue_id):
    """该 Multica issue 是否已执行过测试→验证交接(防重复)"""
    try:
        for c in multica_issue_comments(multica_issue_id):
            if HANDOFF_MARKER in (c.get("content") or ""):
                return True
    except Exception:
        pass
    return False


def check_handoff(limit=50):
    """扫描分配给测试小队、状态为已完成(done/in_review/closed)的 Multica issue,
    在对应 issue 下 @验证小队 并转移分配; 供 cron 定时调用。
    返回处理结果列表。"""
    results = []
    code, out, err = _multica_cli(
        ["issue", "list", "--assignee-id", SQUAD_TEST, "--output", "json", "--limit", str(limit)]
    )
    if code != 0:
        raise RuntimeError(f"multica issue list 失败: {out[:300]} {err[:300]}")
    data = json.loads(out)
    issues = data.get("issues", data) if isinstance(data, dict) else data
    if not isinstance(issues, list):
        issues = []
    for iss in issues:
        iid = iss.get("id")
        status = iss.get("status") or ""
        if status not in ("done", "in_review", "closed", "blocked"):
            continue
        if _handoff_done(iid):
            continue
        try:
            content = (
                f"**{HANDOFF_MARKER}**（状态: {status}）。\n\n"
                f"{mention_squad(SQUAD_VERIFY, SQUAD_VERIFY_NAME)} 请对本次交付进行验证。"
            )
            multica_issue_comment(iid, content)
            # 转移分配给验证小队, 由验证小队接手
            multica_issue_assign(iid, SQUAD_VERIFY)
            # 更新数据库
            req = db_find_requirement_by_multica_issue(iid)
            if req:
                db_update_requirement(req["id"], {"status": "verifying"})
                db_update_flow(req["id"], "node_verify", "in_progress",
                               f"{MULTICA_ISSUE_URL_PREFIX}/{iid}")
            results.append({"multica_issue_id": iid, "title": iss.get("title"),
                            "action": "handoff_to_verify", "status": status})
        except Exception as e:
            results.append({"multica_issue_id": iid, "title": iss.get("title"),
                            "action": "error", "error": str(e)[:200]})
    return results


# ==================== 事件分发 ====================
def normalize_payload(payload):
    """归一化 payload 为统一的 Gitee 风格内部结构。

    GitCode webhook 实际发送的是 GitLab 风格(object_attributes),
    而脚本内部按 Gitee 风格(issue/pull_request/note 顶层对象)处理。
    此函数在解析前做转换, 保证两种格式都能处理。
    """
    attrs = payload.get("object_attributes")
    if not attrs or not isinstance(attrs, dict):
        return payload  # 已是 Gitee 风格或无法识别

    kind = payload.get("object_kind") or payload.get("event_type") or ""
    p = dict(payload)
    user = payload.get("user") or {}
    attrs_author = attrs.get("author") or {}

    def gitlab_user(author):
        if not author:
            return {"login": user.get("username") or "", "name": user.get("name") or ""}
        return {
            "login": author.get("username") or author.get("login") or "",
            "name": author.get("name") or author.get("nick_name") or "",
        }

    if kind == "issue":
        p["issue"] = {
            "id": attrs.get("id"),
            "number": attrs.get("iid"),
            "title": attrs.get("title") or "",
            "body": attrs.get("description") or "",
            "state": attrs.get("state"),
            "html_url": attrs.get("url"),
            "user": gitlab_user(attrs_author),
            "created_at": attrs.get("created_at"),
            "updated_at": attrs.get("updated_at"),
        }
    elif kind == "note":
        p["note"] = {
            "id": attrs.get("id"),
            "body": attrs.get("note") or attrs.get("body") or "",
            "author": gitlab_user(attrs_author),
            "created_at": attrs.get("created_at"),
        }
        # GitLab 风格 note 事件顶层直接带 issue / merge_request 对象
        if not p.get("noteable_type"):
            p["noteable_type"] = attrs.get("noteable_type")
        # 归一化顶层关联对象(GitLab 风格: id + iid)
        # 2026-08-12 幂等加固: 原硬性要求 "iid" in obj, GitCode 未来调整 note
        # payload(缺 iid/缺顶层对象)时 issue 对象整体不构造 -> issue_id=None
        # -> 补建 requirement 落 gitcode_issue_id=NULL(唯一索引对 NULL 失效)
        # -> 评论转机器人重复建任务雪崩。放宽为 id 或 iid 任一存在即构造,
        # 保住幂等锚点(gitcode_issue_id)。
        for src_key, dst_key in (("issue", "issue"), ("merge_request", "pull_request")):
            obj = p.get(src_key)
            if isinstance(obj, dict) and ("iid" in obj or "id" in obj):
                norm = {
                    "id": obj.get("id"),
                    "number": obj.get("iid") or obj.get("number"),
                    "title": obj.get("title") or "",
                    "body": obj.get("description") or "",
                    "state": obj.get("state"),
                    "html_url": obj.get("url"),
                    "user": gitlab_user(obj.get("author")),
                    "target_branch": obj.get("target_branch"),
                    "source_branch": obj.get("source_branch"),
                }
                if dst_key == "issue":
                    p["issue"] = norm
                else:
                    p["pull_request"] = norm
                    p.pop("merge_request", None)
    elif kind in ("merge_request", "pull_request"):
        p["pull_request"] = {
            "id": attrs.get("id"),
            "number": attrs.get("iid"),
            "title": attrs.get("title") or "",
            "body": attrs.get("description") or "",
            "state": attrs.get("state"),
            "html_url": attrs.get("url"),
            "user": gitlab_user(attrs_author),
            "created_at": attrs.get("created_at"),
            "close_related_issue": attrs.get("close_related_issue"),
            "target_branch": attrs.get("target_branch"),
            "source_branch": attrs.get("source_branch"),
        }
    return p


def parse_event(payload):
    """识别事件类型, 返回 (kind, action)"""
    kind = payload.get("object_kind") or ""
    # GitLab 风格 payload 的 action 在 object_attributes 中, Gitee 风格在顶层
    attrs = payload.get("object_attributes") or {}
    action = str(
        payload.get("action") or attrs.get("action") or payload.get("event_name") or ""
    ).lower()

    if kind == "issue":
        return ("issue", action)
    if kind == "note":
        noteable = (payload.get("noteable_type") or "").lower()
        if "merge" in noteable or "pull" in noteable:
            return ("pr_comment", action)
        return ("issue_comment", action)
    if kind in ("merge_request", "pull_request"):
        return ("pr", action)
    return ("other", action)


def handle_event(payload):
    """按事件类型分发处理, 返回处理摘要 dict"""
    payload = normalize_payload(payload)  # 兼容 GitLab/Gitee 两种 payload 格式

    # push 事件: push-sync 按仓库配置分发(devops-bot 迁移; parse_event 不识别 push)
    if str(payload.get("object_kind") or "").lower() == "push":
        return {"event": "push", **handle_push(payload)}

    kind, action = parse_event(payload)

    # 忽略标签检查: 命中全局忽略标签(codeowner/评测参考)的 issue 事件整体忽略
    if kind in ("issue", "issue_comment"):
        if issue_has_ignore_label(payload):
            return {"event": kind, "action": "ignored", "reason": "issue 携带全局忽略标签"}

    # 机器人账号提交的 issue 整体忽略(2026-08-05 用户要求): skills_bot /
    # check_bot 等自动化账号创建的 issue(评测系统自动建单等)不做任何处理,
    # 不评论/不打标签/不分配/不建 Multica 任务, 避免机器人自处理噪音。
    if kind == "issue":
        _issue_obj = payload.get("issue") or {}
        _author = str(((_issue_obj.get("user") or {}).get("login")) or "").strip().lower()
        if _author in BOT_AUTHOR_LOGINS:
            return {"event": "issue_created", "action": "ignored",
                    "reason": f"机器人账号({_author})提交的 issue, 不做处理"}

    if kind == "issue":
        if action in ("open", "create", "new", "", "opened"):
            return {"event": "issue_created", **handle_issue(payload)}
        if action in ("update", "updated"):
            # GitCode 创建 issue 后连推多个 update; 用户编辑补充信息也走这里
            return {"event": "issue_updated", **handle_issue_update(payload)}
        if action in ("reopen", "reopened"):
            # 2026-08-18 用户要求: issue 重开也重新评估(对齐 PR reopen 支持)。
            # force_retry=True 放行"已有 bot 处理评论"守卫 —— 判重关闭过的
            # issue(#47 实测: 关闭后被重开)能重新走完整流程(重新判重/分流);
            # exist 幂等守卫保留, 已有任务的 issue 重开不会重复建任务。
            return {"event": "issue_reopened",
                    **handle_issue(payload, force_retry=True)}
        return {"event": "issue_" + (action or "unknown"), "action": "ignored",
                "reason": f"仅处理创建/更新事件, 当前 action={action or 'unknown'}"}
    if kind == "issue_comment":
        return {"event": "issue_commented", **handle_issue_comment(payload)}
    if kind == "pr":
        pr = payload.get("pull_request") or payload.get("merge_request") or payload.get("pr") or payload
        # GitCode 合入后推送的事件 action 常为 update(而非 merge), state=merged。
        # 旧逻辑只认 action=="merge", 导致已合入 PR 被误判为"创建/更新"走预校验,
        # 合入分发(逻辑8/点亮 node_repo)漏执行(2026-08-04 实测 PR #128)。
        # 修复: state=merged 且非创建类 action 的事件一律按合入处理。
        pr_state = str(pr.get("state") or "").lower()
        if pr_state == "merged" and action not in ("open", "create", "new", "opened"):
            action = "merge"
        if action in ("open", "create", "new", "", "opened", "update", "updated",
                      "reopen", "reopened"):
            # 2026-08-16 用户要求: 同 skill 多 PR 检查上移为入口硬门槛
            # (原审查检测点32 🔴)。在文件数检查之前执行: 命中重复直接阻断
            # (评论提示 PR 创建者 + 跳过整个 PR), 不进预校验/审查/测试。
            repo = get_repo_full_name(payload)
            if check_pr_dup_skill(repo, pr.get("number"), pr=pr):
                return {"event": "pr_created", "action": "skipped_dup_skill",
                        "reason": "该 Skill 已有其他 open PR 在处理中"}
            # PR 文件数上限检查: 超过上限发评论提示并跳过(devops-bot 迁移)
            if check_pr_file_limit(repo, pr.get("number")):
                # 2026-08-12: 评审人与审查解耦 —— 超限跳过审查时仍设置评审人
                # (PR #41 219 文件超限, 审查报告未生成, 评审人跟着空)。
                try:
                    _ino = extract_issue_number_from_pr(
                        pr, repo, pr.get("number"))
                    _assign_pr_reviewer(
                        repo, pr.get("number"), "",
                        issue_no=_ino,
                        title=pr.get("title") or "",
                        body=pr.get("body") or "")
                except Exception as _e:
                    logger.warning(f"[pr-reviewer] 超限 PR 设置评审人失败: {_e}")
                return {"event": "pr_created", "action": "skipped",
                        "reason": f"PR 变更文件数超过上限({PR_FILE_LIMIT})"}
            return {"event": "pr_created", **handle_pr(payload)}
        if action == "merge":
            # 合入事件三分支:
            #  - distribute_for_github 分支 -> 逻辑7(cherry-pick 分发)
            #  - MERGE_TARGET_BRANCH(main/master) -> 逻辑8(点亮 node_repo + 分发到分发小队)
            #  - 普通分支合入 -> 点亮共建仓库节点(node_repo=done), 不再下发测试任务
            pr = (payload.get("pull_request") or payload.get("merge_request")
                  or payload.get("pr") or payload)
            if is_distribute_pr(pr):
                return {"event": "pr_merged_distribute", **handle_pr(payload)}
            target = pr.get("target_branch") or ((pr.get("base") or {}).get("ref")) or ""
            if target == MERGE_TARGET_BRANCH:
                return {"event": "pr_merged_main", **dispatch_merged_pr(
                    payload, pr, get_repo_full_name(payload),
                    pr.get("number"), pr.get("id"),
                    pr.get("title") or "", pr.get("body") or "",
                    pr.get("html_url") or f"https://gitcode.com/{get_repo_full_name(payload)}/pull/{pr.get('number')}")}
            return {"event": "pr_merged", **handle_pr_merged(payload)}
        return {"event": "pr_" + (action or "unknown"), "action": "ignored",
                "reason": f"仅处理创建/合入事件, 当前 action={action or 'unknown'}"}
    if kind == "pr_comment":
        return {"event": "pr_commented", **handle_pr_comment(payload)}
    return {"event": kind, "action": "ignored", "reason": f"不支持的事件类型: object_kind={kind or 'unknown'}"}


# ==================== 自检 ====================
def self_test():
    """检查配置与各系统连通性"""
    results = []
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM `{REQUIREMENT_TABLE}`")
                c = cur.fetchone()[0]
        results.append(("数据库", "OK", f"requirement 表 {c} 条"))
    except Exception as e:
        results.append(("数据库", "FAIL", str(e)))
    try:
        d = gitcode_get("/user")
        results.append(("GitCode", "OK", f"登录账号: {d.get('login')}"))
    except Exception as e:
        results.append(("GitCode", "FAIL", str(e)))
    try:
        code, out, err = _multica_cli(["workspace", "list", "--output", "json"])
        ws = json.loads(out) if code == 0 else []
        name = next((w["name"] for w in ws if w.get("id") == MULTICA_WORKSPACE_ID), "未知")
        results.append(("Multica", "OK" if code == 0 else "FAIL", f"工作区: {name}"))
    except Exception as e:
        results.append(("Multica", "FAIL", str(e)))
    try:
        code, out, err = _multica_cli(["squad", "list", "--output", "json"])
        squads = json.loads(out) if code == 0 else []
        ids = {s["id"]: s["name"] for s in squads}
        for key, sid in (("SQUAD_DEV", SQUAD_DEV), ("SQUAD_TEST", SQUAD_TEST),
                         ("SQUAD_VERIFY", SQUAD_VERIFY), ("SQUAD_DISTRIBUTE", SQUAD_DISTRIBUTE)):
            results.append((key, "OK" if sid in ids else "FAIL", ids.get(sid, "未找到!")))
    except Exception as e:
        results.append(("小队配置", "FAIL", str(e)))
    return results


def main():
    args = sys.argv[1:]
    if args and args[0] == "--test":
        print("=== gitcode-event-handler 自检 ===")
        results = self_test()
        for name, status, detail in results:
            print(f"[{status}] {name}: {detail}")
        return 0 if all(s == "OK" for _, s, _ in results) else 1

    if args and args[0] == "--check-handoff":
        # 测试→验证 交接轮询(供 cron 调用); 无交接任务时静默(空输出), 便于 watchdog 模式
        result = check_handoff()
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # 读取 payload
    if args and args[0] != "-":
        with open(args[0], "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    result = handle_event(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(json.dumps({"error": str(e), "trace": traceback.format_exc()[-2000:]},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
