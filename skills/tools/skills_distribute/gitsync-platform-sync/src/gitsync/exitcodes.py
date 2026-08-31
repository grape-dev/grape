"""Exit codes used across the whole toolchain."""
EXIT_OK = 0          # success / in sync
EXIT_DIFF = 1        # differences (check mode) or self-verify failed
EXIT_GUARD = 2       # foreign history / non-fast-forward without --force / env error
