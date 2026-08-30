from pathlib import Path
from typing import List, Optional, Generator


EXCLUDED_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".idea", ".vscode"}
SOURCE_EXTENSIONS = {".py", ".sh", ".yaml", ".yml", ".json", ".md", ".toml", ".cfg", ".ini"}


def find_files(
    root: Path,
    extensions: Optional[set] = None,
    exclude_dirs: Optional[set] = None,
) -> Generator[Path, None, None]:
    if extensions is None:
        extensions = SOURCE_EXTENSIONS
    if exclude_dirs is None:
        exclude_dirs = EXCLUDED_DIRS

    if not root.exists():
        return

    for item in root.rglob("*"):
        if item.is_file():
            if any(part in exclude_dirs for part in item.parts):
                continue
            if item.suffix.lower() in extensions:
                yield item


def read_file_content(file_path: Path, encoding: str = "utf-8") -> str:
    try:
        return file_path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        return file_path.read_text(encoding="gbk", errors="replace")
    except Exception:
        return ""


def read_skill_files(skill_path: Path) -> dict[str, str]:
    result = {}
    for file_path in find_files(skill_path):
        content = read_file_content(file_path)
        if content:
            rel_path = str(file_path.relative_to(skill_path))
            result[rel_path] = content
    return result


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_file(file_path: Path, content: str, encoding: str = "utf-8") -> Path:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding=encoding)
    return file_path


def list_subdirectories(root: Path, exclude_dirs: Optional[set] = None) -> List[Path]:
    if exclude_dirs is None:
        exclude_dirs = EXCLUDED_DIRS
    if not root.exists():
        return []
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and d.name not in exclude_dirs
    )