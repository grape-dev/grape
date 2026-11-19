"""旧入口兼容层（随 worker 安装包下发，不在仓库 ``src/`` 树内）。

为什么存在
  ``#699`` 把 ``src/runtime/`` 等顶层兼容包清掉后，代码包里只剩 canonical 入口
  ``src.domains.runtime.engine.agent``。但已经装到设备上的 ``start-worker.sh``、
  systemd unit、第三方 wrapper 是旧版 ``install-worker.sh`` 生成的，命令行里仍
  固化了 ``-m src.runtime.agent``（沙箱 worker 为 ``-m src.runtime.cloud_worker``）。
  这类设备「换了代码但没重跑安装脚本」时，进程一启动就
  ``ModuleNotFoundError: No module named 'src.runtime'``，现场表现为设备接入不上。

为什么不放在仓库 ``src/runtime/``
  两个 arch 守卫明确禁止在仓库树里恢复这个包：
  ``tests/arch/test_domain_boundaries.py::test_legacy_dir_shims_removed`` 要求
  ``import src.runtime`` 必须 ImportError；
  ``tests/arch/test_issue_2010_src_top_level_layout.py`` 禁止 MANIFEST 列 legacy
  顶层路径。所以 shim 源码放在 ``packaging/legacy-entry-src/``，
  由 ``src.kernel.worker_package.build_package()`` 在打包时映射成包内的
  ``src/runtime/*`` —— 只在设备侧生效，仓库侧依旧干净。

约束
  这里只能做转发，不允许放任何业务逻辑。真正的实现在
  ``src.domains.runtime.engine.*``；shim 一变，包版本指纹就变（见
  ``worker_package._manifest_version``），设备才会重新拉包。
"""