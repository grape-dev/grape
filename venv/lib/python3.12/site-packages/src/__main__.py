"""python -m src 入口。"""
import sys

from src.cli import main

if __name__ == "__main__":
    sys.exit(main(prog_name="grape"))