# 包体大小对比记录（Issue #499）

实测（2026-10-29，`pip download -r requirements-worker.txt --only-binary=:all: --python-version 3.12`，清华镜像）：

| 平台矩阵 | wheels 数 | 体积 |
|---|---|---|
| 旧：单平台（构建机 aarch64） | 17 | ~4.0 MB |
| 新：linux x86_64 | 17 | 4.0 MB |
| 新：linux aarch64 | 17 | 4.0 MB |
| 新：win_amd64 | 17 | 1.7 MB |
| **新：三平台平铺混装（纯 Python wheel 去重后）** | **29** | **8.1 MB（未压缩）** |

- 纯 Python（`py3-none-any`）wheel 11 个三平台共享；二进制 wheel（aiohttp/bcrypt/frozenlist/multidict/propcache/yarl）每平台独立。
- grape CLI wheel（`grape-*-py3-none-any.whl`）平台无关，仍单份随包（参考 0.1.5 版约 30 KB）。
- 结论：包内 wheels 部分由 ~4.0 MB 增至 ~8.1 MB（tar.gz 后增量更小，约 +2~3 MB），换取 linux x86_64 与 win_amd64 首装离线完成（不再依赖在线镜像）。
- 只预置 requirements-worker.txt 必要依赖（aiohttp/bcrypt/click/httpx 闭包），无额外依赖。
