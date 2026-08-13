"""Hello Todo CLI —— 简单的命令行待办清单"""
import sys

todos = []


def add(text):
    todos.append({"text": text, "done": False})
    print(f"[+] 已添加: {text}")


def list_all():
    if not todos:
        print("待办清单为空，使用 add <事项> 添加。")
        return
    for i, t in enumerate(todos, 1):
        mark = "[x]" if t["done"] else "[ ]"
        print(f"{i}. {mark} {t['text']}")


def done(idx):
    if 1 <= idx <= len(todos):
        todos[idx - 1]["done"] = True
        print(f"[✓] 完成: {todos[idx - 1]['text']}")
    else:
        print("序号无效。")


def main():
    if len(sys.argv) < 2:
        list_all()
        return
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) >= 3:
        add(" ".join(sys.argv[2:]))
    elif cmd == "done" and len(sys.argv) >= 3:
        done(int(sys.argv[2]))
    else:
        print("用法: python todo.py | add <事项> | done <序号>")


if __name__ == "__main__":
    main()
