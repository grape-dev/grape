"""`db-update` command: write back platform status by multica issue id.

Flow:
  1. query t_skills_dev_requirement by multica_issue_id -> skill_name + id
     - 0 rows -> error; >1 rows -> refuse (dirty data)
  2. read product JSON, find the skill by name -> url/version
     - not found -> error
  3. UPDATE t_skills_dev_flow SET platform_<target>_status/url/time
     WHERE requirement_id = (flow.requirement_id == requirement.id)
"""
from __future__ import annotations

import sys

from ..config import Config
from ..exitcodes import EXIT_DIFF, EXIT_GUARD, EXIT_OK
from ..product import read_product


class DbUpdateError(Exception):
    """Raised on DB write-back failures."""


def db_update_command(config: Config, args) -> int:
    db = config.db
    if not (db.host and db.database):
        print("db-update: no db config (host/database missing)", file=sys.stderr)
        return EXIT_GUARD

    multica = args.multica_issue.strip()
    target = getattr(args, "target", None)  # github|gitcode|clawhub or None (all)

    # whitelist target before it is interpolated into SQL column names
    valid_targets = {"github", "gitcode", "clawhub"}
    targets = [target] if target else ["github", "gitcode", "clawhub"]
    for tg in targets:
        if tg not in valid_targets:
            print(f"db-update: invalid target {tg!r} (must be one of "
                  f"{sorted(valid_targets)})", file=sys.stderr)
            return EXIT_GUARD

    import pymysql

    try:
        conn = pymysql.connect(
            host=db.host, user=db.user, password=db.password,
            database=db.database, connect_timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        print(f"db-update: cannot connect: {e}", file=sys.stderr)
        return EXIT_GUARD

    try:
        with conn.cursor() as cur:
            # 1. resolve requirement by multica
            cur.execute(
                "SELECT id, skill_name FROM t_skills_dev_requirement "
                "WHERE multica_issue_id = %s",
                (multica,),
            )
            rows = cur.fetchall()
            if not rows:
                print(f"db-update: no requirement for multica {multica}", file=sys.stderr)
                return EXIT_DIFF
            if len(rows) > 1:
                print(
                    f"db-update: dirty data — multica {multica} matches {len(rows)} "
                    "requirements; fix data first",
                    file=sys.stderr,
                )
                return EXIT_GUARD
            requirement_id, skill_name = rows[0]

            # 2. find the skill in the product
            product = read_product(db.product_dir)
            if not product:
                print("db-update: no product file found", file=sys.stderr)
                return EXIT_DIFF
            skill_entry = None
            for s in product.get("skills", []):
                if s.get("name") == skill_name:
                    skill_entry = s
                    break
            if skill_entry is None:
                print(
                    f"db-update: skill {skill_name!r} not found in product output",
                    file=sys.stderr,
                )
                return EXIT_DIFF

            # 3. update platform fields: per-target url from the artifact; only
            #    platforms that succeeded this round (legacy artifact without a
            #    platforms list falls back to updating every target).
            # 需求4: 各 target 的 UPDATE 相互独立 -> 并行执行(线程池)。
            # 同一行并发冲突: MySQL UPDATE 天然行锁 + 幂等重放(值相同),
            # 后写者覆盖先写者但值一致, 无实际冲突; 失败 target 单独记。
            urls = skill_entry.get("urls") or {}
            legacy_url = skill_entry.get("url") or ""
            platforms = skill_entry.get("platforms")
            results = {}

            def _update_one(tg: str) -> tuple:
                if platforms is not None and tg not in platforms:
                    return tg, "skipped", None
                url = urls.get(tg) or legacy_url
                col_prefix = f"platform_{tg}"
                sql = (
                    f"UPDATE t_skills_dev_flow SET "
                    f"{col_prefix}_status='published', {col_prefix}_url=%s, "
                    f"{col_prefix}_time=NOW() WHERE requirement_id=%s"
                )
                # pymysql connection 非线程安全: 每线程独立连接
                local_conn = pymysql.connect(
                    host=db.host, user=db.user, password=db.password,
                    database=db.database, connect_timeout=10,
                )
                try:
                    with local_conn.cursor() as cur:
                        cur.execute(sql, (url, requirement_id))
                        if cur.rowcount == 0:
                            return tg, "missing_flow_row", (
                                f"no t_skills_dev_flow row for requirement_id "
                                f"{requirement_id} (platform {tg}) — nothing updated"
                            )
                        local_conn.commit()
                    return tg, "updated", None
                finally:
                    local_conn.close()

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(3, len(targets))) as pool:
                futures = {pool.submit(_update_one, tg): tg for tg in targets}
                for fut in concurrent.futures.as_completed(futures):
                    tg, st, err = fut.result()
                    results[tg] = (st, err)

            updated = sum(1 for st, _ in results.values() if st == "updated")
            skipped = sum(1 for st, _ in results.values() if st == "skipped")
            missing = [tg for tg, (st, _) in results.items() if st == "missing_flow_row"]
            if missing:
                for tg in missing:
                    print(f"db-update: {results[tg][1]}", file=sys.stderr)
                return EXIT_DIFF
            if updated == 0:
                print(
                    f"db-update: requirement {requirement_id} ({skill_name}) — "
                    f"none of the requested platforms {targets} is in product "
                    f"platforms {platforms or []}; nothing updated",
                    file=sys.stderr,
                )
                return EXIT_DIFF
            # 各 target 已在独立线程连接内各自 commit; 外层仅关闭连接
            print(
                f"db-update: requirement {requirement_id} ({skill_name}) "
                f"-> platforms {targets} published (updated={updated}, "
                f"skipped={skipped})"
            )
            return EXIT_OK
    except Exception as e:  # noqa: BLE001
        print(f"db-update: error: {e}", file=sys.stderr)
        return EXIT_GUARD
    finally:
        conn.close()
