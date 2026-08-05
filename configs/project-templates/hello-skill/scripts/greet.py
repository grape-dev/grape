#!/usr/bin/env python3
"""Hello Skill 示例脚本"""


def greet(name: str = "World") -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greet())
