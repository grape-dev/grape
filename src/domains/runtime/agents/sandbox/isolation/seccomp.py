"""SeccompProgram——seccomp BPF 程序，deny io_uring 三 syscall。

生命周期：open() → args() → close()
fd 跨多次 wrap（幂等 open），作为 SandboxManager 全局依赖注入。
线程安全：open() 加锁（double-check locking）。

不 deny all ioctl——用户程序依赖 ioctl 做常规操作。
TIOCSTI P1 跳过（需要 arg 条件过滤），P2 补。
AF_UNIX 不由 seccomp 管——bwrap 文件系统白名单控制。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from typing import Protocol

log = logging.getLogger("devops-bot")

SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO_EPERM = 0x00050001  # SCMP_ACT_ERRNO(1) = 0x00050000 | 1

DENY_SYSCALLS = [
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
]


class SeccompProgram(Protocol):
    """seccomp BPF 程序。

    生命周期：open() → args() → close()
    fd 跨多次 wrap，作为 SandboxManager 全局依赖注入。
    """

    def open(self) -> int:
        """创建 BPF 程序 fd（幂等 + 线程安全）。返回 fd 或 -1。"""
        ...

    def args(self) -> list[str]:
        """基于已打开的 fd，返回 bwrap 参数。fd=-1 时返回 []。"""
        ...

    def close(self) -> None:
        """关闭 fd（幂等）。"""
        ...


class LibseccompProgram:
    """用 libseccomp 构建 BPF 程序。fd 幂等创建，跨多次 wrap。

    线程安全：open() 加锁。
    """

    def __init__(self):
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._lib = None
        try:
            self._lib = ctypes.CDLL("libseccomp.so.2")
        except OSError:
            pass

    def open(self) -> int:
        """创建 BPF 程序 fd（幂等 + 线程安全）。"""
        if self._fd is not None:
            return self._fd
        with self._lock:
            if self._fd is not None:
                return self._fd
            self._fd = self._build_once()
            # fd < 3 会和 stdin/stdout/stderr 冲突——pass_fds 不允许标准 fd
            while self._fd >= 0 and self._fd < 3:
                new_fd = os.dup(self._fd)
                os.close(self._fd)
                self._fd = new_fd
            return self._fd

    def _build_once(self) -> int:
        """实际构建 BPF 程序（不加锁，由 open() 持锁调用）。"""
        if self._lib is None:
            return -1

        lib = self._lib
        ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
        if not ctx:
            return -1

        try:
            lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
            lib.seccomp_syscall_resolve_name.restype = ctypes.c_int

            for name in DENY_SYSCALLS:
                num = lib.seccomp_syscall_resolve_name(name.encode())
                if num >= 0:
                    lib.seccomp_rule_add(ctx, SCMP_ACT_ERRNO_EPERM, num, 0)
                # 不支持的 syscall → 静默跳过

            # TIOCSTI P1 跳过——需要 arg 条件过滤，P2 补
            # P2: lib.seccomp_rule_add(ctx, SCMP_ACT_ERRNO_EPERM, ioctl_num, 1,
            #   SCMP_A0(SCMP_CMP_EQ, 0x5412))

            memfd = os.memfd_create("seccomp-bpf", os.MFD_CLOEXEC)
            if memfd < 0:
                return -1

            ret = lib.seccomp_export_fd(ctx, memfd)
            if ret != 0:
                os.close(memfd)
                return -1

            return memfd
        finally:
            lib.seccomp_release(ctx)

    def args(self) -> list[str]:
        if self._fd is None or self._fd < 0:
            return []
        return ["--seccomp", str(self._fd)]

    def close(self) -> None:
        if self._fd is not None and self._fd >= 0:
            try:
                os.close(self._fd)
            except Exception:
                pass
        self._fd = None


def socket_syscall_nr(arch: str) -> int:
    """__NR_socket per architecture（未知架构按 aarch64 兜底并告警）。

    Windows 的 platform.machine() 返回 "AMD64" / "ARM64"（大小写不一），
    必须先归一化；否则 AMD64 落到 aarch64 分支，用错系统调用号。
    """
    _a = (arch or "").lower()
    if _a in ("x86_64", "amd64", "x64", "i386", "i686", "x86"):
        return 41   # asm/unistd_64.h
    if _a in ("aarch64", "arm64"):
        return 198  # asm-generic/unistd.h
    log.warning(f"seccomp: unknown architecture {arch}, using aarch64 __NR_socket=198")
    return 198


class NullSeccompProgram:
    """空 seccomp——不做 seccomp 限制。

    语义：完全不限制 syscall。风险高，但 bwrap 本身有 namespace 隔离。
    seccomp 是补充不是唯一防线。
    """

    def open(self) -> int:
        return -1

    def args(self) -> list[str]:
        return []

    def close(self) -> None:
        pass


class BPFSocketFilter:
    """手动 BPF 过滤器——禁止 AF_INET/AF_INET6 socket 创建。

    不依赖 libseccomp，直接生成 BPF 字节码。
    aarch64 libseccomp 有 SIGSEGV 问题，用手动 BPF 绕过。

    BPF 逻辑：
      0: A = seccomp_data.nr
      1: jeq #198 (socket), jt=1, jf=0  → socket: go to 3, else: go to 2
      2: RET ALLOW (不是 socket syscall)
      3: A = seccomp_data.args[0] (socket domain)
      4: jeq #2 (AF_INET), jt=2, jf=0  → AF_INET: go to 7, else: go to 5
      5: jeq #10 (AF_INET6), jt=1, jf=0  → AF_INET6: go to 7, else: go to 6
      6: RET ALLOW (AF_UNIX 等)
      7: RET ERRNO EPERM

    只禁止创建 IPv4/IPv6 socket，允许 AF_UNIX（UDS 通信）。
    配合 --unshare-net 双保险：即使 namespace 被 bypass，seccomp 也禁止网络 socket。
    """

    # P0-13 修复：按架构选 __NR_socket（aarch64=198, x86_64=41）
    # 2026-11-20：Windows 的 platform.machine() 返回 "AMD64"，旧判定落到
    # aarch64 默认分支（AMD64 上用 aarch64 的 socket 号=198，而 x86_64 是 41）——
    # 语义完全错位。统一做大小写归一 + AMD64/x64 映射，并在非 POSIX 平台
    # 直接降级为空实现（seccomp 是 Linux 内核特性，Windows 上生成 BPF 无意义）。
    import platform as _platform
    _SECCOMP_SUPPORTED = os.name == "posix"
    _NR_SOCKET = socket_syscall_nr(_platform.machine())
    _AF_INET = 2
    _AF_INET6 = 10
    _SECCOMP_RET_ALLOW = 0x7FFF0000
    _SECCOMP_RET_ERRNO = 0x00050001  # EPERM

    def __init__(self):
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._tmpfile: str | None = None

    def open(self) -> int:
        """生成 BPF 字节码文件，返回 fd。幂等。"""
        if not self._SECCOMP_SUPPORTED:
            # Windows/macOS：没有 seccomp(2)，返回 -1 = 不启用（调用方按无过滤处理）
            if self._fd is None:
                log.debug("seccomp: skipped (not a POSIX platform)")
            return -1
        if self._fd is not None:
            return self._fd
        with self._lock:
            if self._fd is not None:
                return self._fd
            self._fd = self._build_bpf()
            return self._fd

    def _build_bpf(self) -> int:
        """生成 BPF 字节码，写入 memfd，返回 fd。"""
        import struct

        # sock_filter: __u16 code; __u8 jt; __u8 jf; __u32 k
        filters = [
            # 0: A = seccomp_data.nr (offset 0)
            struct.pack('<HBBI', 0x20, 0, 0, 0),  # BPF_LD|BPF_W|BPF_ABS
            # 1: jeq #198 (socket), jt=1, jf=0
            struct.pack('<HBBI', 0x15, 1, 0, self._NR_SOCKET),  # BPF_JMP|BPF_JEQ|BPF_K
            # 2: RET ALLOW (not socket)
            struct.pack('<HBBI', 0x06, 0, 0, self._SECCOMP_RET_ALLOW),  # BPF_RET|BPF_K
            # 3: A = seccomp_data.args[0] (offset 16)
            struct.pack('<HBBI', 0x20, 0, 0, 16),  # BPF_LD|BPF_W|BPF_ABS
            # 4: jeq #2 (AF_INET), jt=2, jf=0
            struct.pack('<HBBI', 0x15, 2, 0, self._AF_INET),  # BPF_JMP|BPF_JEQ|BPF_K
            # 5: jeq #10 (AF_INET6), jt=1, jf=0
            struct.pack('<HBBI', 0x15, 1, 0, self._AF_INET6),  # BPF_JMP|BPF_JEQ|BPF_K
            # 6: RET ALLOW (AF_UNIX etc)
            struct.pack('<HBBI', 0x06, 0, 0, self._SECCOMP_RET_ALLOW),  # BPF_RET|BPF_K
            # 7: RET ERRNO EPERM
            struct.pack('<HBBI', 0x06, 0, 0, self._SECCOMP_RET_ERRNO),  # BPF_RET|BPF_K
        ]

        prog = b''.join(filters)

        # bwrap --seccomp 期望：raw BPF filter 数组（8 的倍数）
        try:
            memfd = os.memfd_create("seccomp-bpf-socket", os.MFD_CLOEXEC)
            os.write(memfd, prog)
            os.lseek(memfd, 0, os.SEEK_SET)
            # fd < 3 会和 stdin/stdout/stderr 冲突
            while memfd < 3:
                new_fd = os.dup(memfd)
                os.close(memfd)
                memfd = new_fd
            log.info(f"BPF socket filter created: {len(filters)} instructions, {len(prog)} bytes, fd={memfd}")
            return memfd
        except Exception as e:
            log.error(f"BPF socket filter creation failed: {e}")
            return -1

    def args(self) -> list[str]:
        if self._fd is None or self._fd < 0:
            return []
        return ["--seccomp", str(self._fd)]

    def close(self) -> None:
        if self._fd is not None and self._fd >= 0:
            try:
                os.close(self._fd)
            except Exception:
                pass
        self._fd = None
