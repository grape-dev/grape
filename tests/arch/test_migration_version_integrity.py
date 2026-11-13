"""迁移版本号完整性守卫（静态，无需数据库/环境变量）。

背景（2026-11-12 dev 事故，详见 docs）：两条并行开发线各自占用 202611120001~120005，
合并后同一版本号出现两个不同名字。库表 `grape_t_schema_migrations` 只按"版本号"判是否执行过，
于是其中一条线的迁移被判定"已执行"而**永不落地**（UUID 主键、trace_context 列、autopilot 列
全都没建），且日志只有一条 ERROR、不阻断启动 → 静默故障。

本守卫守住三条规则（编译期即可发现，不依赖数据库）：
  R1 版本号唯一：同一版本号不得注册两条迁移（本 PR 内撞号）
  R2 已发布版本号稳定：基线快照里出现过的版本号，名字不得变化（跨线合并撞号的典型形态）
  R3 新号只能往后：基线之后新增的迁移号必须大于基线最大值（防止往历史区间塞号）

刷新基线：`python3 scripts/gen-migration-baseline.py --write`（仅发布评审时执行并单独提交）。
"""
import json
import pathlib
import re
import sys
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parents[2]
BASELINE = pathlib.Path(__file__).with_name("migration_versions_baseline.json")
VERSION_RE = re.compile(r"^\d{12}$")


def _registry() -> list[tuple[str, str]]:
    sys.path.insert(0, str(REPO))
    import src.api.db_setup as db_setup  # noqa: E402

    return [(m.version, m.name) for m in db_setup._MIGRATIONS]


def test_migration_versions_unique_and_well_formed():
    pairs = _registry()
    assert pairs, "迁移注册表为空，检查 db_setup._MIGRATIONS"

    bad_format = [v for v, _ in pairs if not VERSION_RE.match(v)]
    assert not bad_format, f"版本号格式必须为 12 位数字：{bad_format}"

    blank = [v for v, n in pairs if not (n or "").strip()]
    assert not blank, f"迁移名不得为空：{blank}"

    dups = {v: c for v, c in Counter(v for v, _ in pairs).items() if c > 1}
    assert not dups, (
        f"迁移版本号重复（同一版本号注册了多条迁移）：{dups}\n"
        "→ 两条并行线占用了同一段号。改号时只改**尚未发布**的那条，"
        "已发布的必须保持原号（否则各环境库里已盖章的行会把它判成已执行）。"
    )


def test_released_migration_versions_are_stable():
    """R2/R3：已发布版本不得改名/改号，新号必须更大。"""
    pairs = _registry()
    current = {v: n for v, n in pairs}

    assert BASELINE.exists(), (
        f"缺少基线快照 {BASELINE}；首次引入请执行 "
        "`python3 scripts/gen-migration-baseline.py --write` 并提交"
    )
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    released = baseline.get("versions") or {}
    max_released = baseline.get("max_version") or max(released)

    renamed = {v: (released[v], current[v]) for v in released
               if v in current and current[v] != released[v]}
    assert not renamed, (
        "已发布迁移被改名/改号（跨线占号撞车的典型形态）：\n"
        + "\n".join(f"  {v}: 基线={old!r} 现在={new!r}" for v, (old, new) in renamed.items())
        + "\n→ 库里按版本号判'已执行'，改名后对应 DDL 永远不会再跑。"
        "新迁移请用更大的号（>= 20261115xxxx 之类的新段）。"
    )

    vanished = sorted(v for v in released if v not in current)
    assert not vanished, (
        f"已发布迁移从注册表消失（删迁移是不可逆的，各环境已盖章）：{vanished}"
    )

    too_old = sorted(v for v in current if v not in released and v <= max_released)
    assert not too_old, (
        f"新增迁移使用了不大于已发布最大号 {max_released} 的版本号：{too_old}\n"
        "→ 与历史区间重叠，后续合并极易撞号；请改用更大的新号段。"
    )