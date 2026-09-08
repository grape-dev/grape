"""python -m grape_cli 入口。"""
import sys

from grape_cli.cli import main

if __name__ == "__main__":
    sys.exit(main(prog_name="grape"))