#!/usr/bin/env python3
import os
import re
from typing import Optional

DEFAULT_REPO_CONF = os.path.expanduser("~/.fetch-pr-skill/obs-cred.conf")


def load_default_repo() -> Optional[str]:
    """
    从 obs-cred.conf 读 DEFAULT_REPO（如 'huaweiskilldev/huaweicloud-skillsdev'）。
    返回 'owner/repo' 字符串；未配置或读取失败返回 None。
    """
    if not os.path.exists(DEFAULT_REPO_CONF):
        return None
    try:
        with open(DEFAULT_REPO_CONF, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEFAULT_REPO="):
                    repo = line.split("=", 1)[1].strip()
                    return repo if repo else None
    except Exception:
        return None
    return None


def repo_cache_path(owner_repo: str) -> str:
    """把 'owner/repo' 形式仓库名转成检索库缓存目录路径（斜杠转下划线）。"""
    return f"/tmp/fetch-pr-skill/{owner_repo.replace('/', '_')}"


def extract_triggers_from_content(content: str) -> list:
    """
    从 SKILL.md 内容的 'Triggers:' 或 'Triggers include:' 段提取触发词列表。
    匹配到结尾（句号/换行列表/---）为止。提取失败返回 []。
    """
    m = re.search(r"Triggers(?:\s+include)?:\s*(.+?)(?:\.|\n\s*-|\n---)", content, re.S)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def extract_triggers(skill_path: str) -> list:
    """
    从 SKILL.md 文件的 'Triggers:' 或 'Triggers include:' 段提取触发词列表。
    匹配到结尾（句号/换行列表/---）为止。提取失败返回 []。
    """
    if os.path.isdir(skill_path):
        skill_md = os.path.join(skill_path, "SKILL.md")
    else:
        skill_md = skill_path
    if not os.path.exists(skill_md):
        return []
    try:
        with open(skill_md, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []
    return extract_triggers_from_content(content)


def build_skill_index(repo_root: str) -> dict:
    """
    遍历 repo_root/skills/ 下所有 SKILL.md，返回 {skill_name: content}。
    目录不存在或为空返回 {}。
    """
    skills_dir = os.path.join(repo_root, "skills")
    if not os.path.isdir(skills_dir):
        return {}
    index = {}
    try:
        for root, dirs, files in os.walk(skills_dir):
            if "SKILL.md" in files:
                name = os.path.basename(root)
                with open(os.path.join(root, "SKILL.md"), "r", encoding="utf-8", errors="ignore") as f:
                    index[name] = f.read()
    except Exception:
        return {}
    return index


def retrieval_hit_count(triggers: list, skill_name: str, index: dict) -> int:
    """
    对每个触发词在全仓库 index 中检索（content 包含触发词即命中该 skill），
    统计命中名单包含 skill_name 的触发词数。index 为空返回 0。
    """
    if not index or not triggers:
        return 0
    hit = 0
    for trigger in triggers:
        for name, content in index.items():
            if trigger in content and name == skill_name:
                hit += 1
                break  # 当前 skill 命中该触发词，计入并继续下一个触发词
    return hit


def score_retrieval(hit_count: int) -> float:
    """命中≥4→2.0；1-3→1.0；0→0.0"""
    if hit_count >= 4:
        return 2.0
    if hit_count >= 1:
        return 1.0
    return 0.0
