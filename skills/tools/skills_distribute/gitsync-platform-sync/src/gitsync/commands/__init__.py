"""Command dispatch: maps each subcommand to its handler."""

from ..config import Config
from ..exitcodes import EXIT_GUARD


def cmd_run(config: Config, args):
    from .run import run_command

    return run_command(config, args)


def cmd_sync(config: Config, args):
    from .sync import sync_command

    return sync_command(config, args)


def cmd_check(config: Config, args):
    from .check import check_command

    return check_command(config, args)


def cmd_lookup(config: Config, args):
    from .lookup import lookup_command

    return lookup_command(config, args)


def cmd_bootstrap(config: Config, args):
    from .bootstrap import bootstrap_command

    return bootstrap_command(config, args)


def cmd_map(config: Config, args):
    from .maprebuild import map_rebuild_command

    if getattr(args, "map_action", None) == "rebuild":
        return map_rebuild_command(config, args)
    return EXIT_GUARD


def cmd_publish(config: Config, args):
    from .publish import publish_command

    return publish_command(config, args)


def cmd_db_update(config: Config, args):
    from .dbupdate import db_update_command

    return db_update_command(config, args)
