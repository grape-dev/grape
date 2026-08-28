---
name: os-disk-list
version: 1.0.0
description: 枚举当前系统所有挂载硬盘列表，输出设备名/挂载点/文件系统类型/总容量/已用容量
tools:
  - bash
  - lsblk
  - mount
  - df
---

# 系统挂载硬盘列表

## 场景

需要快速查看当前系统所有挂载硬盘的列表信息，包括设备名、挂载点、文件系统类型、总容量和已用容量。
与 os-disk-info（等价 df -h）互补，本 skill 侧重挂载点列表（lsblk/mount 方式）。

## 知识

### 数据来源优先级

1. **lsblk**（优先）：现代 Linux 标准工具，输出结构化，支持 JSON，信息最完整
2. **mount + /proc/mounts + df -T**（降级）：当 lsblk 不可用时组合使用
   - `mount` 或 `/proc/mounts` 获取设备名、挂载点、文件系统类型
   - `df -T` 补充容量信息

### 输出字段

| 字段 | 说明 | 示例 |
|------|------|------|
| DEVICE | 设备名 | /dev/sda1 |
| MOUNTPOINT | 挂载点 | / |
| FSTYPE | 文件系统类型 | ext4 |
| SIZE | 总容量 | 50G |
| USED | 已用容量 | 20G |

### 注意事项

- 跳过伪文件系统（proc、sysfs、devtmpfs、cgroup、tmpfs 等虚拟文件系统）
- 跳过无挂载点的设备
- 容量使用人类可读格式（K/M/G/T）

## 步骤

1. 检测 lsblk 是否可用
2. 若 lsblk 可用，使用 lsblk 提取挂载设备信息
3. 若 lsblk 不可用，降级使用 mount/proc/mounts + df -T 组合
4. 过滤掉伪文件系统和无挂载点的设备
5. 格式化输出（纯文本或 JSON）

## 判断标准

- 输出包含设备名、挂载点、文件系统类型、总容量、已用容量五个字段
- 不包含伪文件系统条目
- lsblk 缺失时降级方案可正常运行
- --json 输出为合法 JSON

## 输出规范

### 纯文本模式（默认）

```
DEVICE       MOUNTPOINT  FSTYPE  SIZE  USED
/dev/sda1    /           ext4    50G   20G
/dev/sda2    /home       ext4    200G  80G
```

### JSON 模式（--json）

```json
[
  {
    "device": "/dev/sda1",
    "mountpoint": "/",
    "fstype": "ext4",
    "size": "50G",
    "used": "20G"
  }
]
```

## 参考

执行脚本位于 `scripts/os-disk-list.sh`。
