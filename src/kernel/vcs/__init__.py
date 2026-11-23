"""VCS 平台无关抽象（kernel 层，不依赖 domains）。

子模块：
  `repo_ref` —— 仓库身份值对象与「建仓响应 → RepoRef」对齐器。平台差异只存在于
  对齐器，调用方（路由/托管用例）只认 RepoRef。与 `token_resolver` 的 VCSProvider
  端口同层，保证 kernel 不 import domains（check-arch R1）。

注意：本包 `__init__` 刻意不 re-export 子模块符号——包 ↔ 子模块互相 import 会被
check-arch R5 判成导入环；调用方直接用 `from src.kernel.vcs.repo_ref import ...`。
"""