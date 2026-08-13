"""Hello Calculator —— 简单计算器，带单元测试"""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b


OPERATIONS = {"+": add, "-": subtract, "*": multiply, "/": divide}


def main():
    print("简单计算器 (输入如: 3 + 4, 或 q 退出)")
    while True:
        line = input("> ").strip()
        if line.lower() in ("q", "quit"):
            break
        parts = line.split()
        if len(parts) != 3 or parts[1] not in OPERATIONS:
            print("格式: <数字> <+|-|*|/> <数字>")
            continue
        try:
            a, op, b = float(parts[0]), parts[1], float(parts[2])
            result = OPERATIONS[op](a, b)
            print("= ", result)
        except ValueError as e:
            print("错误:", e)


if __name__ == "__main__":
    main()
