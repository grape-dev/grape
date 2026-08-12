#!/usr/bin/env bash
set -euo pipefail

PSEUDO_FSTYPES="proc sysfs devtmpfs cgroup cgroup2 tmpfs devpts mqueue hugetlbfs debugfs tracefs securityfs configfs pstore binfmt_misc efivarfs bpf fusectl ramfs overlay"

is_pseudo_fstype() {
    local fstype="$1"
    local p
    for p in $PSEUDO_FSTYPES; do
        if [ "$fstype" = "$p" ]; then
            return 0
        fi
    done
    return 1
}

format_size() {
    local bytes="$1"
    if [ "$bytes" -ge 1099511627776 ] 2>/dev/null; then
        echo "$((bytes / 1099511627776))T"
    elif [ "$bytes" -ge 1073741824 ] 2>/dev/null; then
        echo "$((bytes / 1073741824))G"
    elif [ "$bytes" -ge 1048576 ] 2>/dev/null; then
        echo "$((bytes / 1048576))M"
    elif [ "$bytes" -ge 1024 ] 2>/dev/null; then
        echo "$((bytes / 1024))K"
    else
        echo "${bytes}B"
    fi
}

get_df_info() {
    local mountpoint="$1"
    local size="N/A" used="N/A"
    if command -v df >/dev/null 2>&1; then
        local df_line
        df_line=$(df -B1 --output=size,used,target 2>/dev/null | tail -n +2 | while IFS= read -r dl; do
            mp=$(echo "$dl" | awk '{print $NF}')
            if [ "$mp" = "$mountpoint" ]; then
                echo "$dl"
                break
            fi
        done || true)
        if [ -n "$df_line" ]; then
            local size_bytes used_bytes
            size_bytes=$(echo "$df_line" | awk '{print $1}')
            used_bytes=$(echo "$df_line" | awk '{print $2}')
            [ -n "$size_bytes" ] && [ "$size_bytes" -gt 0 ] 2>/dev/null && size=$(format_size "$size_bytes")
            [ -n "$used_bytes" ] && [ "$used_bytes" -ge 0 ] 2>/dev/null && used=$(format_size "$used_bytes")
        fi
    fi
    echo "${size}|${used}"
}

collect_lsblk() {
    if ! command -v lsblk >/dev/null 2>&1; then
        return 1
    fi

    local has_used=0
    if lsblk -l -n -o NAME,MOUNTPOINT,FSTYPE,SIZE,USED 2>/dev/null 1>/dev/null; then
        has_used=1
    fi

    local lsblk_output
    if [ "$has_used" -eq 1 ]; then
        lsblk_output=$(lsblk -l -n -o NAME,MOUNTPOINT,FSTYPE,SIZE,USED 2>/dev/null)
    else
        lsblk_output=$(lsblk -l -n -o NAME,MOUNTPOINT,FSTYPE,SIZE 2>/dev/null)
    fi

    echo "$lsblk_output" | while IFS= read -r line; do
        [ -z "$line" ] && continue
        local name mountpoint fstype size used
        if [ "$has_used" -eq 1 ]; then
            name=$(echo "$line" | awk '{print $1}')
            mountpoint=$(echo "$line" | awk '{print $2}')
            fstype=$(echo "$line" | awk '{print $3}')
            size=$(echo "$line" | awk '{print $4}')
            used=$(echo "$line" | awk '{print $5}')
        else
            name=$(echo "$line" | awk '{print $1}')
            mountpoint=$(echo "$line" | awk '{print $2}')
            fstype=$(echo "$line" | awk '{print $3}')
            size=$(echo "$line" | awk '{print $4}')
            used=""
        fi
        [ -z "$mountpoint" ] && continue
        [ -z "$fstype" ] && continue
        if is_pseudo_fstype "$fstype"; then
            continue
        fi
        if [ -z "$used" ]; then
            local df_result
            df_result=$(get_df_info "$mountpoint")
            local df_size df_used
            df_size=$(echo "$df_result" | cut -d'|' -f1)
            df_used=$(echo "$df_result" | cut -d'|' -f2)
            if [ -z "$size" ]; then
                size="$df_size"
            fi
            if [ -n "$df_used" ] && [ "$df_used" != "N/A" ]; then
                used="$df_used"
            else
                used="N/A"
            fi
        fi
        local dev_name="/dev/$name"
        if echo "$name" | grep -q '^/dev/'; then
            dev_name="$name"
        fi
        echo "${dev_name}|${mountpoint}|${fstype}|${size}|${used}"
    done
}

collect_fallback() {
    local mounts_source
    if [ -r /proc/mounts ]; then
        mounts_source="/proc/mounts"
    elif command -v mount >/dev/null 2>&1; then
        mounts_source=""
    else
        echo "ERROR: no mount info available" >&2
        return 1
    fi

    if [ -n "$mounts_source" ]; then
        grep -v '^nodev' "$mounts_source" 2>/dev/null || true
    else
        mount 2>/dev/null || true
    fi | while IFS= read -r line; do
        [ -z "$line" ] && continue
        local device mountpoint fstype
        if [ -n "$mounts_source" ]; then
            device=$(echo "$line" | awk '{print $1}')
            mountpoint=$(echo "$line" | awk '{print $2}')
            fstype=$(echo "$line" | awk '{print $3}')
        else
            device=$(echo "$line" | awk '{print $1}')
            mountpoint=$(echo "$line" | awk '{print $3}')
            fstype=$(echo "$line" | sed -n 's/.*type \([^ ]*\).*/\1/p')
        fi
        [ -z "$mountpoint" ] && continue
        [ -z "$fstype" ] && continue
        if is_pseudo_fstype "$fstype"; then
            continue
        fi
        if ! echo "$device" | grep -q '^/dev/'; then
            continue
        fi
        local df_result
        df_result=$(get_df_info "$mountpoint")
        local size used
        size=$(echo "$df_result" | cut -d'|' -f1)
        used=$(echo "$df_result" | cut -d'|' -f2)
        echo "${device}|${mountpoint}|${fstype}|${size}|${used}"
    done
}

output_text() {
    printf "%-16s %-24s %-10s %-8s %-8s\n" "DEVICE" "MOUNTPOINT" "FSTYPE" "SIZE" "USED"
    printf "%-16s %-24s %-10s %-8s %-8s\n" "------" "----------" "------" "----" "----"
    while IFS='|' read -r device mountpoint fstype size used; do
        printf "%-16s %-24s %-10s %-8s %-8s\n" "$device" "$mountpoint" "$fstype" "$size" "$used"
    done
}

output_json() {
    local first=1
    echo "["
    while IFS='|' read -r device mountpoint fstype size used; do
        if [ "$first" -eq 1 ]; then
            first=0
        else
            echo ","
        fi
        printf '  {"device": "%s", "mountpoint": "%s", "fstype": "%s", "size": "%s", "used": "%s"}' \
            "$device" "$mountpoint" "$fstype" "$size" "$used"
    done
    echo ""
    echo "]"
}

main() {
    local json_mode=0
    if [ $# -gt 0 ]; then
        case "$1" in
            --json) json_mode=1 ;;
            -h|--help)
                echo "Usage: os-disk-list.sh [--json]"
                echo "  --json  Output in JSON format"
                exit 0
                ;;
            *)
                echo "Unknown option: $1" >&2
                echo "Usage: os-disk-list.sh [--json]" >&2
                exit 1
                ;;
        esac
    fi

    local entries
    entries=$(collect_lsblk 2>/dev/null || true)
    if [ -z "$entries" ]; then
        entries=$(collect_fallback 2>/dev/null || true)
    fi

    if [ -z "$entries" ]; then
        echo "No mounted disks found." >&2
        exit 1
    fi

    if [ "$json_mode" -eq 1 ]; then
        echo "$entries" | output_json
    else
        echo "$entries" | output_text
    fi
}

main "$@"
