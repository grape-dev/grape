# os-disk-list 参考文档

## 数据来源

### lsblk（优先）

`lsblk` 来自 `util-linux` 包，是现代 Linux 标准块设备列举工具。

常用列：
- `NAME` - 设备名
- `MOUNTPOINT` - 挂载点
- `FSTYPE` - 文件系统类型
- `SIZE` - 总容量（字节，使用 -b 标志）
- `USED` - 已用容量（字节，需 util-linux >= 2.27）

关键标志：
- `-l` - 列表模式，避免树形输出，便于解析
- `-n` - 不输出表头
- `-b` - 以字节为单位输出容量，便于统一格式化

检测 USED 列是否可用：
```bash
lsblk -l -n -b -o NAME,MOUNTPOINT,FSTYPE,SIZE,USED >/dev/null 2>&1
```

### df（补充 USED）

当 lsblk 不提供 USED 列时，使用 `df -B1 --output=size,used,target` 获取字节数再格式化。

### /proc/mounts + mount（降级）

当 lsblk 不可用时：
- `/proc/mounts` 格式：`device mountpoint fstype options dump pass`
- `mount` 命令格式：`device on mountpoint type fstype (options)`

## 伪文件系统过滤

以下文件系统类型视为伪文件系统，应跳过：
proc sysfs devtmpfs cgroup cgroup2 tmpfs devpts mqueue hugetlbfs debugfs tracefs securityfs configfs pstore binfmt_misc efivarfs bpf fusectl ramfs overlay autofs rpc_pipefs squashfs

## 容量格式化

字节数转人类可读格式（整数除法）：
- >= 1TiB → T
- >= 1GiB → G
- >= 1MiB → M
- >= 1KiB → K
- < 1KiB → B
