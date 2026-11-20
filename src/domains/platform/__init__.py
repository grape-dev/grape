"""D109 平台对象模型（平台是一等领域对象）。

分层位置：本包是**领域层**（`src/domains/`），不是内核层。平台对象模型要读凭据域、
登录渠道与平台卡（`src.domains.mgmt.*` / `src.domains.runtime.vcs.*`），放在
`src/kernel/` 会直接违反 R1（kernel 不得 import domains，check-arch 硬门禁）——
2026-11-14 按用户质询纠正到 domains（原路径 `src/kernel/platform_object/`）。

持久化（ORM / Repository）仍在 `src/kernel/persistence/models_platform_object.py`
与 `repositories_platform_object.py`：纯映射、无领域依赖，属内核。

本 `__init__` 刻意不做 re-export：`from src.domains.platform import hooks` 这种包级
取子模块的写法不依赖 re-export，而 re-export 会与 check-arch 的「模块→父包」隐式边
互指成伪 SCC（R5 误报，同 IM 各包白名单条目）。需要领域对象时直接从
`src.domains.platform.domain` / `src.domains.platform.canonical` 取。
"""